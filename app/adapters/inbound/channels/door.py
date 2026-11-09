"""How the CLI and the Telegram channel reach the agent: as HTTP clients of the chat API through the front door (specs/020), exactly like the
web page. They never touch the work queue: the door proves who is calling and meters it, the API publishes the turn and relays the worker's
events as SSE, and the worker's guarantees (the per-conversation lock, crash reclaim, the dead-letter archive) cover the turn as for any client
(GRAPH_PATTERNS.md 89). `tests/adapters/inbound/channels/test_channels_use_the_door.py` fails the build if a channel can reach the queue again.

`Door` yields the SAME event vocabulary the API streams (token, tool_start, tool_end, citations, followups, approval_required, error, done).
Anything that stops a request from being served (a refused sign-in, a rate limit, the door not answering) becomes ONE `error` event with words a
person can act on, so both channels render it through the path they already have. Nothing falls back to another route: when the door cannot be
reached nothing is sent, and the person is told so.

The caller is named in one of two ways, chosen by the API's mode and never inferred from a response: `identity` (the default) a bearer token the
door verifies; `headers` (development only, announced loudly by the API) the tenant and person as sent, like the web page's pickers.
"""
import json
import logging
import ssl
from collections.abc import AsyncGenerator
from typing import Any, Protocol

import httpx

from app.adapters.inbound.channels.oauth import SignInRefusedError
from app.core import metrics
from app.core.config import settings

logger = logging.getLogger(__name__)

SIGNIN_MESSAGES = {
    "forbidden": "You are not allowed to use this assistant.",
    "unknown_person": "You are not registered yet. Ask an administrator to add you.",
    "credentials": "Your sign-in was refused. Sign in again and retry.",
    "not_linked": "Your Telegram is not connected to an account yet. Send /login to connect it.",
    "unavailable": "The sign-in service is unavailable right now. Try again shortly.",
}


class DoorRefusedError(Exception):
    """A cancel the front door did not accept (cancel has no stream to carry the words in)."""


class Auth(Protocol):
    async def headers(self) -> dict[str, str]:
        """What identifies the caller on the next request. Raises `SignInRefusedError` when there is no way to produce it."""
        ...

    async def reauthenticate(self) -> bool:
        """After a 401: try once to get a fresh sign-in. True if the request is worth repeating."""
        ...


class HeaderAuth:
    """Header mode (`AUTH_MODE=headers`, development): the caller is named by two headers and nothing is proven."""

    def __init__(self, tenant: str, principal: str) -> None:
        self._headers = {"X-Tenant-Id": tenant, "X-Principal-Id": principal}

    async def headers(self) -> dict[str, str]:
        return dict(self._headers)

    async def reauthenticate(self) -> bool:
        return False


def make_http_client() -> httpx.AsyncClient:
    """One client for the door and the authorization server. `CHANNEL_CA_FILE` is the CA that signed the door's certificate when it is not in
    the system store (Caddy's local root); naming it REPLACES the system roots, so a deployment with a public certificate leaves it empty."""
    ca_file = settings.channel_ca_file.strip()
    verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=ca_file) if ca_file else True
    # `read` is the gap between two events of ONE turn (a slow tool, a long model call), so it is generous; the API bounds the wait for a first
    # event itself (CHAT_FIRST_RESPONSE_DEADLINE_SECONDS) and answers with an error event.
    return httpx.AsyncClient(verify=verify, timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0), follow_redirects=False)


def _wait_words(retry_after: str | None) -> str:
    try:
        seconds = max(1, int(float(retry_after or "")))
    except ValueError:
        return "a moment"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def _refusal(status: int, retry_after: str | None) -> tuple[str, str]:
    """(reason for the metric, words for the person) for a status the API or the door answered with."""
    if status == 401:
        return "unauthenticated", SIGNIN_MESSAGES["credentials"]
    if status == 403:
        return "forbidden", "You are not allowed to do that."
    if status == 404:
        return "not_found", "That conversation was not found."
    if status == 429:
        return "rate_limited", f"You are sending too much. Try again in {_wait_words(retry_after)}."
    if status in (502, 503, 504):
        return "unavailable", "Your sign-in can't be checked right now and nothing was sent. Try again shortly."
    if status == 422:
        return "invalid", "That request was refused as invalid."
    return "other", f"The request was refused ({status})."


class Door:
    def __init__(self, auth: Auth, *, channel: str, domain: str, http: httpx.AsyncClient, base_url: str | None = None) -> None:
        self._auth = auth
        self._channel = channel
        self._domain = domain
        self._http = http
        self._base = (base_url or settings.channel_api_url).rstrip("/")

    def _error(self, reason: str, content: str) -> dict[str, Any]:
        metrics.channel_door_refusal_total.labels(channel=self._channel, reason=reason).inc()
        return {"type": "error", "content": content}

    async def _stream(self, path: str, body: dict[str, Any], *, read_body: bool = True) -> AsyncGenerator[dict[str, Any], None]:
        for attempt in (0, 1):
            try:
                headers = {**await self._auth.headers(), "X-Domain": self._domain, "Accept": "text/event-stream"}
            except SignInRefusedError as exc:
                yield self._error(exc.reason, SIGNIN_MESSAGES.get(exc.reason, SIGNIN_MESSAGES["credentials"]))
                return
            try:
                async with self._http.stream("POST", f"{self._base}{path}", json=body, headers=headers) as reply:
                    if reply.status_code == 401 and attempt == 0 and await self._auth.reauthenticate():
                        continue  # a token that expired between two requests: one fresh sign-in, one repeat
                    if reply.status_code != 200:
                        reason, words = _refusal(reply.status_code, reply.headers.get("retry-after"))
                        yield self._error(reason, words)
                        return
                    if not read_body:
                        return  # the API acted before it began to stream (see `cancel_turn`)
                    async for line in reply.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[5:].strip())
                        except ValueError:
                            logger.warning("channel_bad_sse_frame")
                            continue
                        if isinstance(event, dict):
                            yield event
                    return
            except httpx.HTTPError as exc:
                logger.warning("channel_door_unreachable", extra={"error_class": type(exc).__name__})
                yield self._error("unreachable", "The front door is unreachable, so nothing was sent. Try again shortly.")
                return

    async def stream_turn(
        self,
        text: str,
        thread_id: str,
        *,
        require_approval: bool = False,
        unattended: bool = False,
        replay_key: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """One new turn. `unattended` and `require_approval` can only make it stricter; `replay_key` makes a repeat of this request the same
        attempt (the API streams the FIRST attempt's events and starts none)."""
        body: dict[str, Any] = {"message": text, "thread_id": thread_id}
        if require_approval:
            body["require_approval"] = True
        if unattended:
            body["unattended"] = True
        if replay_key is not None:
            body["replay_key"] = replay_key
        async for event in self._stream("/chat/stream/queued", body):
            yield event

    async def stream_resume(self, thread_id: str, *, approved: bool) -> AsyncGenerator[dict[str, Any], None]:
        async for event in self._stream("/chat/resume", {"thread_id": thread_id, "approved": approved}):
            yield event

    async def cancel_turn(self, thread_id: str, *, wait: bool = False) -> None:
        """Ask for the turn to stop, and return once the API has accepted the request without reading what it streams: it sets the cancel flag
        and publishes the cancel job BEFORE it begins to stream (app/adapters/inbound/api/routers/chat.py::chat_cancel), and the stream is only the paused-run
        job's outcome, which nobody here waits for. `wait` reads that outcome too, for a caller that sends its next request on the same
        conversation straight after (a stale pause cleared before a new message): without it the new turn can arrive before the cancel is done.
        Raises `DoorRefusedError` when the door or the API did not accept it, so a caller that logs can say why."""
        async for event in self._stream("/chat/cancel", {"thread_id": thread_id}, read_body=wait):
            if event.get("type") == "error" and not (wait and event.get("code") == "cancelled"):
                raise DoorRefusedError(str(event.get("content")))

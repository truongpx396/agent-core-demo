"""The CLI's sign-in (specs/020 FR-003): the person signs in with the same sign-in as the web page, once, and the terminal stays signed in until the
authorization server's own lifetime ends.

The first request (or a 401) opens the authorization server's page in the browser; a one-shot listener on a loopback port catches the redirect,
and the code is exchanged with PKCE. The port is fixed (`CHANNEL_CLI_CALLBACK_PORT`) because the authorization server compares a redirect URI
exactly with what the tenant's `api-<tenant>` client registered (scripts/mcp_gateway_bootstrap.py registers it). The session is kept in a file only
its owner can read and is renewed with its refresh token before it expires; a refresh that is refused (the person was disabled, the token was
revoked) falls back to signing in again, and one that could not be tried (the server did not answer) is reported, not turned into a new sign-in.

A token is never printed, logged or put in an error; the only thing shown is the address the person opens.
"""
import asyncio
import json
import logging
import os
import secrets
import urllib.parse
import webbrowser
from collections.abc import Callable
from pathlib import Path

import httpx

from app.adapters.inbound.channels import oauth
from app.adapters.inbound.channels.oauth import SignInRefused, Tokens
from app.core import metrics
from app.core.config import settings

logger = logging.getLogger(__name__)

SIGN_IN_TIMEOUT_SECONDS = 300.0
_DONE_PAGE = "<!doctype html><meta charset=utf-8><title>Signed in</title><p>Signed in. You can close this tab and go back to the terminal.</p>"
_FAILED_PAGE = "<!doctype html><meta charset=utf-8><title>Sign-in failed</title><p>The sign-in did not complete. Go back to the terminal.</p>"


def default_session_path() -> Path:
    configured = settings.channel_cli_session_file.strip()
    return Path(configured) if configured else Path.home() / ".config" / "graph-agent-core" / "channel-session.json"


class SessionStore:
    """One JSON file, mode 600, in a directory made mode 700. Written to a sibling and renamed, so a crash never leaves half a token."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, *, issuer: str, client_id: str) -> Tokens | None:
        """The saved session, or None when there is none, it is unreadable, or it belongs to another authorization server or client."""
        try:
            record = json.loads(self.path.read_text())
            if record["issuer"] != issuer or record["client_id"] != client_id:
                return None
            refresh = record.get("refresh_token")
            return Tokens(str(record["access_token"]), refresh if isinstance(refresh, str) and refresh else None, float(record["expires_at"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, tokens: Tokens, *, issuer: str, client_id: str) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        record = {
            "issuer": issuer,
            "client_id": client_id,
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at,
        }
        temporary = self.path.with_name(f"{self.path.name}.{secrets.token_hex(4)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(record, handle)
        os.replace(temporary, self.path)

    def clear(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True


class CliAuth:
    """`app.adapters.inbound.channels.door.Auth` for a person at a terminal."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        tenant: str,
        store: SessionStore | None = None,
        open_browser: Callable[[str], object] = webbrowser.open,
        say: Callable[[str], object] = print,
    ) -> None:
        self._http = http
        self._issuer = settings.web_auth_issuer.strip().rstrip("/")
        self._audience = settings.web_auth_audience.strip().rstrip("/")
        self._scope = settings.web_auth_scope.strip()
        self._port = settings.channel_cli_callback_port
        self._client_id = f"api-{tenant}"  # app/adapters/inbound/api/web_auth.py::CLIENT_ID_PREFIX, the rule the web page finds a tenant's client by
        self._store = store or SessionStore(default_session_path())
        self._open_browser = open_browser
        self._say = say
        self._tokens: Tokens | None = None
        self._lock = asyncio.Lock()

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self._port}/callback"

    async def headers(self) -> dict[str, str]:
        async with self._lock:
            if self._tokens is None:
                self._tokens = self._store.load(issuer=self._issuer, client_id=self._client_id)
            if self._tokens is None or not self._tokens.fresh():
                self._tokens = await self._renew(self._tokens)
            return {"Authorization": f"Bearer {self._tokens.access_token}"}

    async def reauthenticate(self) -> bool:
        """After a 401 the access token we hold is not accepted: renew it (or sign in again) and let the caller repeat the request once."""
        async with self._lock:
            try:
                self._tokens = await self._renew(self._tokens)
            except SignInRefused:
                return False
            return True

    def sign_out(self) -> bool:
        self._tokens = None
        return self._store.clear()

    async def _renew(self, current: Tokens | None) -> Tokens:
        if current is not None and current.refresh_token:
            try:
                tokens = await oauth.refresh_tokens(
                    self._http, self._issuer, client_id=self._client_id, refresh_token=current.refresh_token, scope=self._scope, resource=self._audience
                )
            except SignInRefused as exc:
                if exc.reason == "unavailable":
                    metrics.channel_signin_total.labels(channel="cli", outcome="unavailable").inc()
                    raise
                # refused: the person was disabled or the token revoked. Signing in again is the person's call, and the server will say no if so.
            else:
                self._store.save(tokens, issuer=self._issuer, client_id=self._client_id)
                metrics.channel_signin_total.labels(channel="cli", outcome="refreshed").inc()
                return tokens
        return await self._interactive()

    async def _interactive(self) -> Tokens:
        verifier, challenge = oauth.pkce_pair()
        state = secrets.token_urlsafe(16)
        url = oauth.authorize_url(
            self._issuer,
            client_id=self._client_id,
            redirect_uri=self.redirect_uri,
            scope=self._scope,
            resource=self._audience,
            state=state,
            challenge=challenge,
        )
        arrived: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                line = (await reader.readline()).decode("latin-1")
                target = line.split(" ")[1] if line.count(" ") >= 2 else ""
                parsed = urllib.parse.urlsplit(target)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                if parsed.path == "/callback" and not arrived.done():
                    arrived.set_result(query)
                page = _DONE_PAGE if parsed.path == "/callback" and "code" in query else _FAILED_PAGE
                body = page.encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
                await writer.drain()
            finally:
                writer.close()

        try:
            server = await asyncio.start_server(handle, "localhost", self._port)
        except OSError as exc:
            self._say(f"Sign-in needs the local port {self._port} (CHANNEL_CLI_CALLBACK_PORT) and it is in use: {type(exc).__name__}")
            metrics.channel_signin_total.labels(channel="cli", outcome="unavailable").inc()
            raise SignInRefused("unavailable") from exc
        async with server:
            self._say(f"Sign in to continue. Opening your browser; if it does not open, go to:\n  {url}")
            self._open_browser(url)
            try:
                query = await asyncio.wait_for(arrived, SIGN_IN_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                metrics.channel_signin_total.labels(channel="cli", outcome="credentials").inc()
                raise SignInRefused("credentials") from exc
        code = query.get("code")
        if not code or not secrets.compare_digest(query.get("state", ""), state):
            metrics.channel_signin_total.labels(channel="cli", outcome="credentials").inc()
            raise SignInRefused("credentials")
        try:
            tokens = await oauth.exchange_code(
                self._http, self._issuer, client_id=self._client_id, code=code, verifier=verifier, redirect_uri=self.redirect_uri, resource=self._audience
            )
        except SignInRefused as exc:
            metrics.channel_signin_total.labels(channel="cli", outcome=exc.reason).inc()
            raise
        self._store.save(tokens, issuer=self._issuer, client_id=self._client_id)
        metrics.channel_signin_total.labels(channel="cli", outcome="ok").inc()
        return tokens

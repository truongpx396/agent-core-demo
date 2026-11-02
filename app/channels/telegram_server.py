"""The bot's own small HTTP listener (specs/021 FR-007, FR-008): the webhook Telegram calls, and the page the browser returns to after a login.
Neither is a route of the chat API, and neither can sit behind the front door: Telegram holds no token and a browser coming back from the login page
has none yet, so each is authenticated by what it carries instead.

- `POST /telegram/webhook`: the request must carry `TELEGRAM_WEBHOOK_SECRET` in `X-Telegram-Bot-Api-Secret-Token` (what `setWebhook`'s `secret_token`
  makes Telegram send; compared in constant time). It does one thing, put the update on the inbox, and answers 200 only once that worked: any other
  answer makes Telegram redeliver (its docs: a non-2xx is retried), which is what we want when Redis is down.
- `GET /telegram/link/callback` and `POST /telegram/link/confirm`: account linking (telegram_link.py). Pages set no-store, no-referrer and a
  no-frame policy because the address carries a one-time code.

Bound to loopback by default; TLS and the public name are the reverse proxy's (the proxy must route `/telegram/` here and must NOT require the front
door's token for it). No docs or OpenAPI routes are served.
"""
import asyncio
import contextlib
import hmac
import html
import json
import logging
import re
import urllib.parse
from collections.abc import Iterator

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from app.channels.telegram_inbox import Inbox
from app.channels.telegram_link import CALLBACK_PATH, CONFIRM_PATH, LinkFlow
from app.core import metrics
from app.core.config import settings

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/telegram/webhook"
SECRET_HEADER = "x-telegram-bot-api-secret-token"
_MAX_BODY_BYTES = 1_000_000
_SECRET_SHAPE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")  # Telegram allows 1-256 of these; shorter than 32 is guessable
_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
}


def check_webhook_settings() -> None:
    problems = []
    if not settings.telegram_webhook_url.startswith("https://") or not settings.telegram_webhook_url.endswith(WEBHOOK_PATH):
        problems.append(f"TELEGRAM_WEBHOOK_URL must be the public https:// address that ends in {WEBHOOK_PATH}")
    if not _SECRET_SHAPE.match(settings.telegram_webhook_secret):
        problems.append("TELEGRAM_WEBHOOK_SECRET must be 32-256 characters of A-Z a-z 0-9 _ - (openssl rand -hex 32)")
    if problems:
        raise SystemExit("TELEGRAM_MODE=webhook but " + "; ".join(problems))


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    document = f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>{body}"
    return HTMLResponse(document, status_code=status, headers=_PAGE_HEADERS)


def _refused() -> HTMLResponse:
    return _page("Not linked", "<p>This link did not work. It may have expired or been used already. Go back to Telegram and send /login again.</p>", 400)


def build_app(inbox: Inbox, linker: LinkFlow | None, *, webhook: bool) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    if webhook:

        @app.post(WEBHOOK_PATH)
        async def telegram_webhook(request: Request) -> Response:
            sent = request.headers.get(SECRET_HEADER, "")
            if not hmac.compare_digest(sent.encode(), settings.telegram_webhook_secret.encode()):
                metrics.channel_telegram_webhook_total.labels(outcome="forbidden").inc()
                return Response(status_code=403)
            body = await request.body()
            try:
                update = json.loads(body) if len(body) <= _MAX_BODY_BYTES else None
            except ValueError:
                update = None
            if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                metrics.channel_telegram_webhook_total.labels(outcome="invalid").inc()
                return Response(status_code=400)
            try:
                await inbox.push(update)
            except Exception:  # noqa: BLE001 - Redis down: a 503 makes Telegram redeliver, which is the right outcome
                logger.warning("telegram_webhook_push_failed", exc_info=True)
                metrics.channel_telegram_webhook_total.labels(outcome="unavailable").inc()
                return Response(status_code=503)
            metrics.channel_telegram_webhook_total.labels(outcome="accepted").inc()
            return Response(status_code=200)

    if linker is not None:

        @app.get(CALLBACK_PATH)
        async def link_callback(request: Request) -> HTMLResponse:
            code, state = request.query_params.get("code", ""), request.query_params.get("state", "")
            if request.query_params.get("error") or not code or not state:
                return _refused()
            ask = await linker.callback(code, state)
            if ask is None:
                return _refused()
            body = (
                "<h1>Connect this account?</h1>"
                f"<p>Telegram user <b>{html.escape(ask.telegram)}</b> will be able to use the assistant as <b>{html.escape(ask.account)}</b>.</p>"
                "<p>If you did not ask for this in Telegram yourself, close this page: someone may be trying to use your account.</p>"
                f"<form method=post action='{CONFIRM_PATH}'><input type=hidden name=token value='{html.escape(ask.token, quote=True)}'>"
                "<button type=submit>Yes, connect it</button></form>"
            )
            return _page("Connect your account", body)

        @app.post(CONFIRM_PATH)
        async def link_confirm(request: Request) -> HTMLResponse:
            form = urllib.parse.parse_qs((await request.body())[:4096].decode("utf-8", "replace"))
            account = await linker.confirm(form.get("token", [""])[0])
            if account is None:
                return _refused()
            return _page("Connected", f"<p>Connected as <b>{html.escape(account)}</b>. You can go back to Telegram.</p>")

    return app


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """Uvicorn would install its own SIGINT/SIGTERM handlers and swallow the bot's: the bot stops this server itself, after the turns running."""
        yield


async def serve(app: FastAPI, stop: asyncio.Event) -> None:
    config = uvicorn.Config(app, host=settings.telegram_listen_host, port=settings.telegram_listen_port, log_config=None, access_log=False)
    server = _Server(config)
    task = asyncio.create_task(server.serve())
    await stop.wait()
    server.should_exit = True
    await task

"""Account linking (specs/021 FR-008): a person proves who they are at the authorization server, in their own browser, and the bot then acts for
THAT account when they write from their Telegram id. The other way (specs/020) is the bot signing in as a `telegram-<id>` person it derives a
password for; that proves nothing about who the human is. Both are the same shape to the front door: an access token for a person.

    /login  ->  bot: a one-time `state` (+ PKCE) bound to this Telegram id, sent as a button
    browser ->  authorization server's own login page (the bot never sees a password)
    redirect -> the bot's /telegram/link/callback: exchanges the code with the verifier, then SHOWS "link Telegram user X to account Y?"
    confirm  ->  the person presses the button on that page: the link is saved

Why each step is there (all of them are tests):
- **`state` is single use, short lived and bound to the Telegram id that asked** (`getdel`), so a replayed or guessed callback binds nothing.
- **The confirmation page exists because of one attack:** someone sends THEIR /login link to a victim; the victim signs in, and without a question
  the attacker's Telegram would be bound to the victim's account. The page names the Telegram user and the account, so the victim can see it is not theirs.
- **Only a refresh token is kept, sealed with a Fernet key** (`TELEGRAM_LINK_KEY`) in Redis. Every turn refreshes it into a new access token and the
  door verifies THAT token, so what the person may do is whatever the authorization server says today: the stored `sub` is never trusted, a person
  disabled there is cut off at the next refresh (their refresh token is revoked at once, MEASURED on 4.24.0 in specs/020), and a refused refresh
  deletes the link. The tenant comes from the token (`owner`), never from anything in the callback's query: a token of another tenant is refused.
- **Private chats only**: the link is meant for one person.

NOT measured (Casdoor was not run for this): whether a refresh rotates the refresh token. Both are handled (a new one is stored when it comes),
and refreshes for one person are serialized so a rotating server never sees the old token twice.
"""
import asyncio
import json
import logging
import secrets
from dataclasses import dataclass

import httpx
import redis.asyncio as redis
from cryptography.fernet import Fernet, InvalidToken

from app.channels import oauth
from app.channels.oauth import SignInRefused, Tokens
from app.channels.telegram_api import BotApi
from app.core import metrics
from app.core.config import settings

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/telegram/link/callback"
CONFIRM_PATH = "/telegram/link/confirm"
_CONFIRM_SECONDS = 300


def check_settings() -> None:
    """Refuse to start a linking bot that cannot complete a link, the way the API refuses to start without its keys."""
    problems = []
    base = settings.telegram_link_public_url.strip()
    local = base.startswith(("http://localhost", "http://127.0.0.1"))
    if not (base.startswith("https://") or local):
        problems.append("TELEGRAM_LINK_PUBLIC_URL must be the bot's public https:// address (http://localhost is accepted for development)")
    try:
        Fernet(settings.telegram_link_key.encode())
    except (ValueError, TypeError):
        problems.append('TELEGRAM_LINK_KEY must be a Fernet key: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"')
    if not settings.web_auth_issuer.strip():
        problems.append("WEB_AUTH_ISSUER is not set: linking signs people in at the authorization server")
    if problems:
        raise SystemExit("TELEGRAM_LINKING is on but " + "; ".join(problems))


def _tenant() -> str:
    return settings.telegram_tenant.strip() or settings.default_tenant


def _client_id() -> str:
    return f"api-{_tenant()}"


def redirect_uri() -> str:
    return settings.telegram_link_public_url.strip().rstrip("/") + CALLBACK_PATH


class LinkStore:
    """Redis, with everything sealed: a state holds a PKCE verifier and a link holds a refresh token."""

    def __init__(self, client: redis.Redis, key: str) -> None:
        self._redis = client
        self._fernet = Fernet(key.encode())

    def _seal(self, payload: dict) -> str:
        return self._fernet.encrypt(json.dumps(payload).encode()).decode()

    def _open(self, raw: str | bytes | None) -> dict | None:
        if raw is None:
            return None
        try:
            opened = json.loads(self._fernet.decrypt(raw.encode() if isinstance(raw, str) else raw))
        except (InvalidToken, ValueError):
            return None  # tampered with, or sealed with another key: it is as good as absent
        return opened if isinstance(opened, dict) else None

    async def begin(self, user_id: int, chat_id: int, name: str) -> tuple[str, str]:
        """(state, PKCE challenge) of a login for this Telegram user."""
        state = secrets.token_urlsafe(24)
        verifier, challenge = oauth.pkce_pair()
        record = {"user_id": user_id, "chat_id": chat_id, "name": name, "verifier": verifier}
        await self._redis.set(f"telegram:link:state:{state}", self._seal(record), ex=settings.telegram_link_state_ttl_seconds)
        return state, challenge

    async def take_state(self, state: str) -> dict | None:
        """The login `state` stood for, exactly once: reading it deletes it (`GETDEL`)."""
        return self._open(await self._redis.getdel(f"telegram:link:state:{state}"))

    async def hold_for_confirmation(self, record: dict) -> str:
        token = secrets.token_urlsafe(24)
        await self._redis.set(f"telegram:link:confirm:{token}", self._seal(record), ex=_CONFIRM_SECONDS)
        return token

    async def take_confirmation(self, token: str) -> dict | None:
        return self._open(await self._redis.getdel(f"telegram:link:confirm:{token}"))

    def _user_key(self, user_id: int) -> str:
        return f"telegram:link:user:{_tenant()}:{user_id}"

    async def save(self, user_id: int, *, refresh_token: str, sub: str, account: str) -> None:
        await self._redis.set(self._user_key(user_id), self._seal({"refresh_token": refresh_token, "sub": sub, "account": account}))

    async def load(self, user_id: int) -> dict | None:
        return self._open(await self._redis.get(self._user_key(user_id)))

    async def delete(self, user_id: int) -> bool:
        return bool(await self._redis.delete(self._user_key(user_id)))


class LinkedAuth:
    """`app.channels.door.Auth` for a Telegram user who linked an account: a fresh access token from the stored refresh token."""

    def __init__(self, http: httpx.AsyncClient, store: LinkStore, user_id: int) -> None:
        self._http = http
        self._store = store
        self._user_id = user_id
        self._tokens: Tokens | None = None
        self._lock = asyncio.Lock()  # two chats of one person must not refresh with the same token at once (see the module docstring)

    async def sign_in(self) -> str:
        """The account's id (the token's `sub`). Raises `SignInRefused("not_linked")` when there is no link, or the authorization server refused it."""
        async with self._lock:
            if self._tokens is None or not self._tokens.fresh():
                self._tokens = await self._refresh()
        return str(oauth.jwt_claims(self._tokens.access_token).get("sub", ""))

    async def _refresh(self) -> Tokens:
        record = await self._store.load(self._user_id)
        if record is None:
            raise SignInRefused("not_linked")
        try:
            tokens = await oauth.refresh_tokens(
                self._http,
                settings.web_auth_issuer.strip(),
                client_id=_client_id(),
                refresh_token=str(record["refresh_token"]),
                scope=settings.web_auth_scope.strip(),
                resource=settings.web_auth_audience.strip().rstrip("/"),
            )
        except SignInRefused as exc:
            if exc.reason == "credentials":  # refused, not unreachable: revoked, expired, or the person was disabled
                await self._store.delete(self._user_id)
                metrics.channel_telegram_link_total.labels(step="refresh", outcome="revoked").inc()
                raise SignInRefused("not_linked") from exc
            metrics.channel_telegram_link_total.labels(step="refresh", outcome="unavailable").inc()
            raise
        if tokens.refresh_token and tokens.refresh_token != record["refresh_token"]:
            await self._store.save(
                self._user_id, refresh_token=tokens.refresh_token, sub=str(record.get("sub", "")), account=str(record.get("account", ""))
            )
        metrics.channel_telegram_link_total.labels(step="refresh", outcome="ok").inc()
        return tokens

    async def headers(self) -> dict[str, str]:
        await self.sign_in()
        assert self._tokens is not None  # sign_in either set it or raised
        return {"Authorization": f"Bearer {self._tokens.access_token}"}

    async def reauthenticate(self) -> bool:
        self._tokens = None
        try:
            await self.sign_in()
        except SignInRefused:
            return False
        return True


@dataclass(frozen=True)
class ToConfirm:
    token: str
    telegram: str
    account: str


class LinkFlow:
    def __init__(self, store: LinkStore, http: httpx.AsyncClient, api: BotApi) -> None:
        self._store = store
        self._http = http
        self._api = api

    async def login(self, chat_id: int, user_id: int, sender: dict) -> None:
        name = " ".join(str(sender.get(k)) for k in ("first_name", "last_name") if sender.get(k)) or f"user {user_id}"
        if sender.get("username"):
            name += f" (@{sender['username']})"
        state, challenge = await self._store.begin(user_id, chat_id, name)
        url = oauth.authorize_url(
            settings.web_auth_issuer.strip(),
            client_id=_client_id(),
            redirect_uri=redirect_uri(),
            scope=settings.web_auth_scope.strip(),
            resource=settings.web_auth_audience.strip().rstrip("/"),
            state=state,
            challenge=challenge,
        )
        minutes = max(1, settings.telegram_link_state_ttl_seconds // 60)
        await self._api.send_message(
            chat_id,
            f"Sign in to connect your account. The link works once, for {minutes} minutes, and only for this chat.",
            reply_markup={"inline_keyboard": [[{"text": "Sign in", "url": url}]]},
        )
        metrics.channel_telegram_link_total.labels(step="start", outcome="ok").inc()

    async def unlink(self, chat_id: int, user_id: int) -> None:
        removed = await self._store.delete(user_id)
        metrics.channel_telegram_link_total.labels(step="unlink", outcome="ok").inc()
        await self._api.send_message(chat_id, "Disconnected. Send /login to connect an account again." if removed else "No account was connected.")

    async def callback(self, code: str, state: str) -> ToConfirm | None:
        """The browser came back from the login page. Exchanges the code and returns what to ask the person, or None (refused: nothing is bound)."""
        record = await self._store.take_state(state)
        if record is None:
            metrics.channel_telegram_link_total.labels(step="callback", outcome="refused").inc()
            return None
        try:
            tokens = await oauth.exchange_code(
                self._http,
                settings.web_auth_issuer.strip(),
                client_id=_client_id(),
                code=code,
                verifier=str(record["verifier"]),
                redirect_uri=redirect_uri(),
                resource=settings.web_auth_audience.strip().rstrip("/"),
            )
        except SignInRefused as exc:
            metrics.channel_telegram_link_total.labels(step="callback", outcome="unavailable" if exc.reason == "unavailable" else "refused").inc()
            return None
        claims = oauth.jwt_claims(tokens.access_token)
        sub = str(claims.get("sub", ""))
        owner = claims.get("owner")
        if not sub or not tokens.refresh_token or (owner is not None and owner != _tenant()):
            # No subject names nobody; no refresh token cannot be kept alive; another tenant's account is not this bot's to link.
            metrics.channel_telegram_link_total.labels(step="callback", outcome="refused").inc()
            return None
        account = str(claims.get("name") or claims.get("preferred_username") or sub)
        token = await self._store.hold_for_confirmation(
            {**record, "refresh_token": tokens.refresh_token, "sub": sub, "account": account}
        )
        metrics.channel_telegram_link_total.labels(step="callback", outcome="ok").inc()
        return ToConfirm(token, str(record["name"]), account)

    async def confirm(self, token: str) -> str | None:
        """The person pressed the button. Saves the link and tells them in Telegram; returns the account name, or None (refused)."""
        record = await self._store.take_confirmation(token)
        if record is None:
            metrics.channel_telegram_link_total.labels(step="confirm", outcome="refused").inc()
            return None
        await self._store.save(int(record["user_id"]), refresh_token=str(record["refresh_token"]), sub=str(record["sub"]), account=str(record["account"]))
        metrics.channel_telegram_link_total.labels(step="confirm", outcome="ok").inc()
        await self._api.send_message(int(record["chat_id"]), f"Connected to {record['account']}. You can write to me now; /unlink disconnects.")
        return str(record["account"])


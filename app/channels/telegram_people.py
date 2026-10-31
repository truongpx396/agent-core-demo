"""Telegram people (specs/020 FR-004 to FR-006): every Telegram user is a distinct person `telegram-<id>` of ONE tenant at the authorization server,
and the bot reaches the front door as that person, so the door proves, meters and can cut off each of them like anyone else.

**No per-person secret is stored.** A person's password is derived from one master secret, `HMAC(secret, tenant, id)` (256 bits, never typed by a
human), so a restart gives the same people, concurrent first messages cannot disagree about it, and the bot has nothing per person to lose.

**Who may create a person.** By default only an operator (`make telegram-person TELEGRAM_ID=...`): the bot holds a password-derivation secret and
NO credential that can create or change a person, so a leak of its environment exposes the Telegram people and nobody else. MEASURED on Casdoor
4.24.0 (specs/020 research R1): an application's client id and secret are an administrator's of the whole organization (they can reset ANY person's
password), so the opt-in `TELEGRAM_AUTO_PROVISION` puts a tenant-wide credential in the bot, which the setting's comment says plainly.

**Disabled is final.** A person the authorization server reports as forbidden is refused and never re-created or re-enabled by the bot; a person
who does not exist is created only in the opt-in mode, once, under a lock per Telegram id.
"""
import asyncio
import base64
import hashlib
import hmac
import logging

import httpx

from app.channels import oauth
from app.channels.oauth import SignInRefused, Tokens
from app.core import metrics
from app.core.config import settings

logger = logging.getLogger(__name__)

MIN_SECRET_CHARS = 32


def person_name(user_id: int) -> str:
    return f"telegram-{user_id}"


def person_password(secret: str, tenant: str, user_id: int) -> str:
    """43 URL-safe characters carrying 256 bits, derived from the master secret. Separated by NUL so no tenant/id pair can collide with another."""
    digest = hmac.new(secret.encode(), f"telegram-person\x00{tenant}\x00{user_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def check_settings() -> None:
    """Refuse to start an identity-mode bot that has no way to sign anyone in, the way the API refuses to start without its keys."""
    if settings.auth_mode == "headers":
        return
    if len(settings.telegram_person_secret) < MIN_SECRET_CHARS:
        raise SystemExit(
            f"TELEGRAM_PERSON_SECRET must be set to at least {MIN_SECRET_CHARS} characters (openssl rand -hex 32): it is what the bot derives each "
            "Telegram person's password from. It is not needed with AUTH_MODE=headers (development)."
        )
    if settings.telegram_auto_provision and not (settings.telegram_provision_client_id and settings.telegram_provision_client_secret):
        raise SystemExit("TELEGRAM_AUTO_PROVISION is on but TELEGRAM_PROVISION_CLIENT_ID / TELEGRAM_PROVISION_CLIENT_SECRET are not set")


class PersonAuth:
    """`app.channels.door.Auth` for one Telegram user."""

    def __init__(self, people: "TelegramPeople", user_id: int) -> None:
        self._people = people
        self._user_id = user_id
        self._tokens: Tokens | None = None

    async def sign_in(self) -> str:
        """Sign in if there is no usable token yet; returns the person's id at the authorization server (the token's `sub`)."""
        if self._tokens is None or not self._tokens.fresh():
            self._tokens = await self._people.sign_in(self._user_id)
        return str(oauth.jwt_claims(self._tokens.access_token).get("sub", ""))

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


class TelegramPeople:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._tenant = settings.telegram_tenant.strip() or settings.default_tenant
        self._issuer = settings.web_auth_issuer.strip().rstrip("/")
        self._audience = settings.web_auth_audience.strip().rstrip("/")
        self._scope = settings.web_auth_scope.strip()
        self._client_id = f"api-{self._tenant}"
        self._redirect_uri = f"http://localhost:{settings.channel_cli_callback_port}/callback"  # registered for the tenant's client; never followed
        self._auth: dict[int, PersonAuth] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    @property
    def tenant(self) -> str:
        return self._tenant

    def person(self, user_id: int) -> PersonAuth:
        if user_id not in self._auth:
            self._auth[user_id] = PersonAuth(self, user_id)
        return self._auth[user_id]

    async def sign_in(self, user_id: int) -> Tokens:
        # One sign-in at a time per person: two first messages from a new user must see ONE person created, and a refused sign-in must not be
        # retried by a second message (five wrong passwords lock a person at the authorization server).
        async with self._locks.setdefault(user_id, asyncio.Lock()):
            try:
                tokens = await self._password_sign_in(user_id)
            except SignInRefused as exc:
                if exc.reason != "unknown_person" or not settings.telegram_auto_provision:
                    metrics.channel_signin_total.labels(channel="telegram", outcome=exc.reason).inc()
                    raise
                await self._provision(user_id)
                try:
                    tokens = await self._password_sign_in(user_id)
                except SignInRefused as again:
                    metrics.channel_signin_total.labels(channel="telegram", outcome=again.reason).inc()
                    raise
            metrics.channel_signin_total.labels(channel="telegram", outcome="ok").inc()
            return tokens

    async def _password_sign_in(self, user_id: int) -> Tokens:
        return await oauth.password_sign_in(
            self._http,
            self._issuer,
            client_id=self._client_id,
            organization=self._tenant,
            username=person_name(user_id),
            password=person_password(settings.telegram_person_secret, self._tenant, user_id),
            redirect_uri=self._redirect_uri,
            scope=self._scope,
            resource=self._audience,
        )

    async def _provision(self, user_id: int) -> None:
        """Create the person (opt-in only). Anything but a clean success is a refusal the person is told about, and is counted."""
        name = person_name(user_id)
        try:
            reply = await self._http.post(
                f"{self._issuer}/api/add-user",
                auth=httpx.BasicAuth(settings.telegram_provision_client_id, settings.telegram_provision_client_secret),
                json={
                    "owner": self._tenant,
                    "name": name,
                    "displayName": name,
                    "password": person_password(settings.telegram_person_secret, self._tenant, user_id),
                    "email": f"{name}@{self._tenant}.invalid",
                    "type": "normal-user",
                    "isAdmin": False,
                    "isForbidden": False,
                },
            )
            body = reply.json()
        except (httpx.HTTPError, ValueError) as exc:
            metrics.channel_person_provisioned_total.labels(outcome="failed").inc()
            raise SignInRefused("unavailable") from exc
        if not isinstance(body, dict) or body.get("status") != "ok":
            metrics.channel_person_provisioned_total.labels(outcome="failed").inc()
            logger.warning("telegram_person_not_created", extra={"telegram_user_id": user_id})
            raise SignInRefused("credentials")
        metrics.channel_person_provisioned_total.labels(outcome="created").inc()

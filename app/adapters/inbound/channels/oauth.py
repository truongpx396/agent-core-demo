"""A channel's sign-in at the authorization server (specs/020). The CLI and the Telegram channel are clients of the front door, so each
presents a person's access token like the web page does (app/adapters/inbound/api/static/auth.js): an OAuth authorization-code flow with PKCE at the tenant's
`api-<tenant>` client, for the API's audience (RFC 8707 `resource`) and the `api:chat` scope. Nothing here knows Redis, the queue or the graph.

Every Casdoor behaviour below was measured on 4.24.0 (specs/020/research.md R1), not assumed:
- the PASSWORD grant ignores `resource` (the token's audience stays the client id, which the front door refuses), so the Telegram channel
  signs in with the authorization-code flow too, headless, by posting the person's credentials where the login page would;
- a code exchange without `resource` yields the wrong audience, and a refresh must repeat the `resource` of the original grant;
- a disabled person gets "forbidden to sign in" and a missing one "doesn't exist" (told apart below), and a disabled person's refresh token
  is revoked at once, so disabling a person ends their sessions;
- five wrong passwords lock a person, so a refused sign-in is never retried in a loop.
"""
import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

# Casdoor answers HTTP 200 with {"status": "error", "msg": ...} for most failures; these are the phrases that tell the two it is worth
# telling apart (a person who must not be re-created, a person who does not exist yet) from a wrong password.
_FORBIDDEN = "forbidden"
_MISSING = ("doesn't exist", "does not exist")


class SignInRefusedError(Exception):
    """Why a sign-in did not produce a token. `reason` is one of: forbidden (the person is disabled: never re-create or re-enable them),
    unknown_person, credentials (a wrong password, or a refresh token that was refused), unavailable (the authorization server did not answer)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float  # epoch seconds

    def fresh(self, margin: float = 60.0) -> bool:
        return time.time() < self.expires_at - margin


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge)."""
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def jwt_claims(token: str) -> dict[str, Any]:
    """The payload of a token WITHOUT checking it. Only ever used to NAME something (the person's id in a conversation id): the front door is
    what verifies the token, and a request made with a forged one is refused there."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def authorize_url(issuer: str, *, client_id: str, redirect_uri: str, scope: str, resource: str, state: str, challenge: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": resource,
        }
    )
    return f"{issuer.rstrip('/')}/login/oauth/authorize?{query}"


def _tokens(body: dict[str, Any]) -> Tokens:
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise SignInRefusedError("credentials")
    lifetime = body.get("expires_in")
    expires_at = time.time() + float(lifetime) if isinstance(lifetime, (int, float)) else float(jwt_claims(access).get("exp", 0))
    refresh = body.get("refresh_token")
    return Tokens(access, refresh if isinstance(refresh, str) and refresh else None, expires_at)


async def _token_request(http: httpx.AsyncClient, issuer: str, form: dict[str, str]) -> Tokens:
    try:
        reply = await http.post(f"{issuer.rstrip('/')}/api/login/oauth/access_token", data=form)
        body = reply.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SignInRefusedError("unavailable") from exc
    if not isinstance(body, dict) or "error" in body:
        raise SignInRefusedError("credentials")
    return _tokens(body)


async def exchange_code(
    http: httpx.AsyncClient, issuer: str, *, client_id: str, code: str, verifier: str, redirect_uri: str, resource: str
) -> Tokens:
    return await _token_request(
        http,
        issuer,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": resource,  # the authorize request's `resource` alone is not enough (measured)
        },
    )


async def refresh_tokens(http: httpx.AsyncClient, issuer: str, *, client_id: str, refresh_token: str, scope: str, resource: str) -> Tokens:
    return await _token_request(
        http,
        issuer,
        {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token, "scope": scope, "resource": resource},
    )


async def password_sign_in(
    http: httpx.AsyncClient,
    issuer: str,
    *,
    client_id: str,
    organization: str,
    username: str,
    password: str,
    redirect_uri: str,
    scope: str,
    resource: str,
) -> Tokens:
    """Sign a person in WITHOUT a browser: post their credentials where the login page would, take the authorization code from the reply and
    exchange it. The redirect is never followed (nothing listens there); it only has to be one the client registered."""
    verifier, challenge = pkce_pair()
    query = urllib.parse.urlencode(
        {
            "clientId": client_id,
            "responseType": "code",
            "redirectUri": redirect_uri,
            "scope": scope,
            "state": secrets.token_urlsafe(8),
            "nonce": "",
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "resource": resource,
        }
    )
    try:
        reply = await http.post(
            f"{issuer.rstrip('/')}/api/login?{query}",
            json={"application": client_id, "organization": organization, "username": username, "password": password, "type": "code"},
        )
        body = reply.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SignInRefusedError("unavailable") from exc
    if not isinstance(body, dict):
        raise SignInRefusedError("unavailable")
    if body.get("status") != "ok":
        message = str(body.get("msg") or "").lower()
        if _FORBIDDEN in message:
            raise SignInRefusedError("forbidden")
        if any(phrase in message for phrase in _MISSING):
            raise SignInRefusedError("unknown_person")
        raise SignInRefusedError("credentials")
    code = body.get("data")
    if not isinstance(code, str) or not code:
        raise SignInRefusedError("credentials")
    return await exchange_code(http, issuer, client_id=client_id, code=code, verifier=verifier, redirect_uri=redirect_uri, resource=resource)

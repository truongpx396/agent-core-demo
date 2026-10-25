"""Test doubles for the MCP gateway (specs/017): an authorization server's signing key, the tokens it would sign, and the JWKS it serves.

Nothing here touches a network. The gateway's `JwksCache` is handed an `httpx.AsyncClient` over a `MockTransport`, and the MCP client
talks to the ASGI app in memory (`httpx.ASGITransport`), so a test exercises the real SDK middleware, the real transport and the real
tools; only the identity provider is a stand-in.
"""
import base64
import hashlib
import hmac
import json
import time

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.mcp.gateway import GatewayConfig

PUBLIC = "http://127.0.0.1:9000"
ISSUER = "https://auth.example.test"
JWKS_URL = "http://127.0.0.1:1/.well-known/jwks"
ECORP = f"{PUBLIC}/ecorp/mcp"
OPS = f"{PUBLIC}/ops/mcp"


class Signer:
    """One signing key, like one entry of an authorization server's JWKS."""

    def __init__(self, kid: str = "k1") -> None:
        self.kid = kid
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_pem = self._private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    @property
    def jwk(self) -> dict:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._private.public_key()))
        return {**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}

    def sign(self, claims: dict, *, kid: str | None = None) -> str:
        return jwt.encode(claims, self._private, algorithm="RS256", headers={"kid": kid or self.kid})


def claims(**overrides) -> dict:
    """What Casdoor puts in an access token for a user of organization `acme`, minted for the ecorp server with its scope."""
    base = {
        "iss": ISSUER,
        "aud": [ECORP],
        "sub": "user-1",
        "owner": "acme",
        "name": "alice",
        "scope": "ecorp:read",
        "azp": "client-1",
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}  # None removes a claim


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unsigned(payload: dict, kid: str = "k1") -> str:
    """A token whose header says `alg: none`: no signature at all."""
    return f"{_b64(json.dumps({'alg': 'none', 'typ': 'JWT', 'kid': kid}).encode())}.{_b64(json.dumps(payload).encode())}."


def hs256_signed_with_the_public_key(payload: dict, signer: Signer) -> str:
    """The algorithm-confusion forgery: the PUBLIC key (which anyone can read from the JWKS) used as an HMAC secret."""
    signing_input = f"{_b64(json.dumps({'alg': 'HS256', 'typ': 'JWT', 'kid': signer.kid}).encode())}.{_b64(json.dumps(payload).encode())}"
    signature = hmac.new(signer.public_pem, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


class AuthorizationServer:
    """Serves a JWKS and counts how often it is asked."""

    def __init__(self, *signers: Signer) -> None:
        self.signers = list(signers) or [Signer()]
        self.fetches = 0
        self.down = False

    @property
    def signer(self) -> Signer:
        return self.signers[0]

    def _handle(self, _request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        if self.down:
            return httpx.Response(503)
        return httpx.Response(200, json={"keys": [s.jwk for s in self.signers]})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))


def config(**overrides) -> GatewayConfig:
    values = {
        "public_url": PUBLIC,
        "issuer": ISSUER,
        "jwks_url": JWKS_URL,
        "tenant_claim": "owner",
        "principal_claim": "sub",
        "ops_tenants": frozenset({"ops-team"}),
        "jwks_ttl_seconds": 300,
    }
    values.update(overrides)
    return GatewayConfig(**values)

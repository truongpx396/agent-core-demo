"""Test doubles for the API's check of the front door's identity (app/core/internal_identity.py, app/api/identity.py): a signing key, the
identities it would sign, and the key endpoint that publishes it.

Nothing here touches a network. `InternalKeys` is handed an `httpx.AsyncClient` over a `MockTransport`, so a test exercises the real verifier,
the real key cache and (tests/api/test_deps_auth_modes.py) the real dependency and exception handler; only the front door is a stand-in.
"""
import json
import time

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ed25519

from app.core import internal_identity as ii

URL = "http://authlimit:8080/internal/jwks.json"
TTL, GRACE, MIN_REFRESH = 60, 600, 10


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Signer:
    """One signing key of the front door."""

    def __init__(self, kid: str = "k1") -> None:
        self.kid = kid
        self._private = ed25519.Ed25519PrivateKey.generate()

    @property
    def jwk(self) -> dict:
        public = json.loads(jwt.algorithms.OKPAlgorithm.to_jwk(self._private.public_key()))
        return {**public, "kid": self.kid, "use": "sig", "alg": "EdDSA"}

    def sign(self, claims: dict, *, kid: str | None = None, typ: str = ii.IDENTITY_TYP) -> str:
        return jwt.encode(claims, self._private, algorithm="EdDSA", headers={"kid": kid or self.kid, "typ": typ})


class FrontDoor:
    """The key endpoint of the authlimit service: serves its signers' public keys, counts fetches, and can be told to fail."""

    def __init__(self, *signers: Signer) -> None:
        self.signers = list(signers) or [Signer()]
        self.fetches = 0
        self.fail_with: int | None = None
        self.body: object = None  # a replacement body, served as-is, to model a front door that answers rubbish

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        if self.fail_with is not None:
            return httpx.Response(self.fail_with)
        if self.body is not None:
            return httpx.Response(200, content=self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode())
        return httpx.Response(200, json={"keys": [s.jwk for s in self.signers]})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    @property
    def signer(self) -> Signer:
        return self.signers[0]


def claims(**overrides) -> dict:
    """What the front door puts in an identity for a person of tenant `acme`; None removes a claim."""
    now = int(time.time())
    base = {
        "iss": ii.ISSUER, "aud": ii.AUDIENCE, "sub": "user-1", "tenant": "acme", "scope": "api:chat", "jti": "id-1",
        "iat": now, "nbf": now, "exp": now + 30,
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}

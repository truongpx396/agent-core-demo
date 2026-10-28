"""The gateway's signing-key cache (app/mcp/gateway_auth.py::JwksCache) and the verifier that uses it, with a clock the test moves.

The properties that matter operationally: a key is fetched once, not per request; a rotated key is picked up; a caller cannot make the
gateway hammer the authorization server by sending tokens with key ids it does not have; and a key set that cannot be confirmed current
is not trusted (the gateway then refuses, and says why in a metric: to a client an outage looks like an expired login).
"""
import time

import httpx
import jwt
import pytest

from app.core import metrics
from app.mcp.gateway_auth import JwksCache, JwksUnavailable, JwtVerifier
from tests.conftest import metric_value
from tests.mcp import gateway_support as gs

TTL = 300
MIN_REFRESH = 30


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _setup(*signers: gs.Signer):
    server = gs.AuthorizationServer(*signers)
    clock = Clock()
    keys = JwksCache(gs.JWKS_URL, ttl_seconds=TTL, min_refresh_seconds=MIN_REFRESH, client=server.client(), clock=clock)
    verifier = JwtVerifier(server="ecorp", issuer=gs.ISSUER, audience=gs.ECORP, keys=keys)
    return server, clock, keys, verifier


def _jwks_unavailable() -> float:
    return metric_value(metrics.agent_mcp_gateway_requests_total, server="ecorp", outcome="jwks_unavailable")


async def test_the_keys_are_fetched_once_however_many_tokens_arrive():
    server, _clock, _keys, verifier = _setup()

    for _ in range(20):
        assert await verifier.verify_token(server.signer.sign(gs.claims())) is not None

    assert server.fetches == 1


async def test_a_verified_token_carries_what_the_tools_and_the_sdk_need():
    server, _clock, _keys, verifier = _setup()

    access = await verifier.verify_token(server.signer.sign(gs.claims(scope="openid ecorp:read", owner="acme", sub="u1")))

    assert access is not None
    assert access.scopes == ["openid", "ecorp:read"]
    assert access.resource == gs.ECORP
    assert access.subject == "u1"
    assert access.claims["owner"] == "acme"
    assert access.client_id == "client-1"
    assert access.expires_at is not None and access.expires_at > time.time()


async def test_tokens_with_key_ids_nobody_published_cannot_make_the_gateway_hammer_the_authorization_server():
    server, clock, _keys, verifier = _setup()
    await verifier.verify_token(server.signer.sign(gs.claims()))  # the first fetch
    assert server.fetches == 1

    for i in range(200):
        assert await verifier.verify_token(server.signer.sign(gs.claims(), kid=f"made-up-{i}")) is None

    assert server.fetches == 1  # all inside the refresh interval: no new fetch for any of them

    clock.advance(MIN_REFRESH + 1)
    await verifier.verify_token(server.signer.sign(gs.claims(), kid="made-up-again"))
    assert server.fetches == 2  # one more is allowed once the interval has passed, and only one


async def test_a_rotated_key_is_picked_up_once_the_refresh_interval_has_passed():
    old, new = gs.Signer("k-old"), gs.Signer("k-new")
    server, clock, _keys, verifier = _setup(old)
    assert await verifier.verify_token(old.sign(gs.claims())) is not None

    server.signers = [old, new]  # the authorization server publishes its new key
    assert await verifier.verify_token(new.sign(gs.claims())) is None  # inside the interval: not looked for yet

    clock.advance(MIN_REFRESH + 1)
    assert await verifier.verify_token(new.sign(gs.claims())) is not None
    assert await verifier.verify_token(old.sign(gs.claims())) is not None  # the old key stays valid while it is published


async def test_an_outage_shorter_than_the_ttl_changes_nothing_because_the_keys_are_still_current():
    server, clock, _keys, verifier = _setup()
    assert await verifier.verify_token(server.signer.sign(gs.claims())) is not None

    server.down = True
    clock.advance(TTL - 1)

    assert await verifier.verify_token(server.signer.sign(gs.claims())) is not None


async def test_keys_older_than_the_ttl_are_not_trusted_when_they_cannot_be_refreshed():
    server, clock, _keys, verifier = _setup()
    assert await verifier.verify_token(server.signer.sign(gs.claims())) is not None
    before = _jwks_unavailable()

    server.down = True
    clock.advance(TTL + 1)

    assert await verifier.verify_token(server.signer.sign(gs.claims())) is None
    assert _jwks_unavailable() - before == 1  # the metric is where this outage shows: the client only sees a 401


async def test_the_gateway_recovers_when_the_authorization_server_comes_back():
    server, clock, _keys, verifier = _setup()
    await verifier.verify_token(server.signer.sign(gs.claims()))
    server.down = True
    clock.advance(TTL + 1)
    assert await verifier.verify_token(server.signer.sign(gs.claims())) is None

    server.down = False
    clock.advance(MIN_REFRESH + 1)

    assert await verifier.verify_token(server.signer.sign(gs.claims())) is not None


async def test_a_failing_authorization_server_is_asked_at_most_once_per_interval():
    server, clock, _keys, verifier = _setup()
    server.down = True

    for _ in range(50):
        assert await verifier.verify_token(server.signer.sign(gs.claims())) is None

    assert server.fetches == 1
    clock.advance(MIN_REFRESH + 1)
    await verifier.verify_token(server.signer.sign(gs.claims()))
    assert server.fetches == 2


async def test_a_key_set_with_one_unusable_entry_keeps_the_usable_ones():
    server, _clock, keys, verifier = _setup()
    unusable = [type("Odd", (), {"jwk": jwk})() for jwk in ({"kty": "bogus", "kid": "bad"}, {"kty": "RSA"})]  # an unknown type; no kid
    good = server.signer
    server.signers = [*unusable, good]

    assert await verifier.verify_token(good.sign(gs.claims())) is not None
    assert await keys.key_for("bad") is None


async def test_a_response_that_is_not_a_key_set_is_unavailable_not_a_crash():
    class NotJwks:
        async def get(self, _url):
            return httpx.Response(200, json={"keys": "nope"}, request=httpx.Request("GET", gs.JWKS_URL))

    keys = JwksCache(gs.JWKS_URL, ttl_seconds=TTL, client=NotJwks(), clock=Clock())  # a stand-in with only `.get`, which is all the cache calls

    with pytest.raises(JwksUnavailable):
        await keys.key_for("k1")


async def test_the_algorithm_comes_from_the_allow_list_not_from_the_token_header():
    server, _clock, _keys, verifier = _setup()
    payload = gs.claims()

    assert await verifier.verify_token(gs.unsigned(payload)) is None
    assert await verifier.verify_token(gs.hs256_signed_with_the_public_key(payload, server.signer)) is None
    # And RS256 itself, signed by the published key, is what is accepted.
    assert await verifier.verify_token(server.signer.sign(payload)) is not None


async def test_a_token_is_never_raised_about_whatever_it_contains():
    """The SDK calls the verifier from its authentication middleware: an exception there is a 500, not a refusal."""
    server, _clock, _keys, verifier = _setup()
    odd = [
        "", " ", "a.b", "a.b.c", "....", "\x00", "é" * 50, "Bearer x",
        jwt.encode({"sub": "u"}, "secret", algorithm="HS256"),
        server.signer.sign(gs.claims(scope=12345)),  # a number where text belongs: the claim is ignored, not fatal
    ]

    for token in odd:
        result = await verifier.verify_token(token)
        assert result is None or result.scopes == []

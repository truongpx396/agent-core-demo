"""app/core/internal_identity.py: the API's check of the identity the front door signs (specs/018, task T057).

Three layers, each with the collaborator faked and nothing else:
  * the REAL identity the Go service minted (`testdata/auth-vectors/identity.json`), verified at the instant the file says, so the two
    languages are held to the same bytes and neither can drift from the other;
  * a token minted here for each way an identity can be wrong (the cases the task names, and the rest of the metric's vocabulary);
  * the public-key cache, with a clock the test moves and a front door that can be told to fail.

No network and no real clock. PyJWT reads the wall clock inside `jwt.api_jwt`, so the golden-identity test replaces the `datetime` it
imports with one that answers the file's `now` (tests/mcp/test_gateway_vectors.py does the same for the external-token vectors).
"""
import json
import logging
import time
from datetime import datetime as real_datetime
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from app.core import internal_identity as ii
from app.core import metrics
from app.core.internal_identity import (
    Identity,
    InternalIdentityVerifier,
    InternalKeys,
    Refusal,
)
from tests.conftest import metric_value

GOLDEN = json.loads((Path(__file__).resolve().parents[2] / "testdata" / "auth-vectors" / "identity.json").read_text())
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


def _setup(*signers: Signer):
    door = FrontDoor(*signers)
    clock = Clock()
    keys = InternalKeys(URL, ttl_seconds=TTL, stale_grace_seconds=GRACE, min_refresh_seconds=MIN_REFRESH, client=door.client(), clock=clock)
    return door, clock, keys, InternalIdentityVerifier(keys)


def _count(outcome: str) -> float:
    return metric_value(metrics.agent_api_identity_total, outcome=outcome)


class _FrozenDatetime(real_datetime):
    """`datetime.now(tz)` answers the golden file's instant; everything else is the real class."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]  # signature matches datetime.now; only the answer is fixed
        return real_datetime.fromtimestamp(GOLDEN["now"], tz=tz)


# --- the identity the Go service really minted ---------------------------------------------------------------------------------------


async def test_the_identity_the_go_service_minted_verifies_at_the_instant_the_file_says(monkeypatch):
    monkeypatch.setattr(jwt.api_jwt, "datetime", _FrozenDatetime)
    door = FrontDoor()
    door.signers = []  # the golden key set replaces the generated one
    door.body = GOLDEN["jwks"]
    keys = InternalKeys(URL, ttl_seconds=TTL, stale_grace_seconds=GRACE, client=door.client(), clock=Clock())

    verdict = await InternalIdentityVerifier(keys).verify(GOLDEN["token"])

    assert verdict == Identity(tenant=GOLDEN["expect"]["tenant"], principal=GOLDEN["expect"]["principal"])


async def test_that_same_identity_is_refused_once_it_has_expired_in_real_time():
    """The premise of the test above: it passes only because the clock is the file's. Without that the identity is long expired."""
    door = FrontDoor()
    door.body = GOLDEN["jwks"]
    keys = InternalKeys(URL, ttl_seconds=TTL, stale_grace_seconds=GRACE, client=door.client(), clock=Clock())

    verdict = await InternalIdentityVerifier(keys).verify(GOLDEN["token"])

    assert verdict == Refusal("expired")


# --- an identity that is right ----------------------------------------------------------------------------------------------------------


async def test_a_valid_identity_yields_the_tenant_and_the_person_and_counts_ok():
    door, _clock, _keys, verifier = _setup()
    before = _count("ok")

    verdict = await verifier.verify(door.signer.sign(claims(tenant="acme", sub="u-7")))

    assert verdict == Identity(tenant="acme", principal="u-7")
    assert _count("ok") == before + 1


# --- an identity that is wrong, one reason each ----------------------------------------------------------------------------------------

_RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _external_rs256_token() -> str:
    """A Casdoor-style access token, for the wrong audience of this check: signed RS256, no `typ` of ours."""
    return jwt.encode(claims(aud="https://api.example.test", owner="acme"), _RSA, algorithm="RS256", headers={"kid": "k1"})


def _token_for(case: str, door: FrontDoor) -> str:
    signer = door.signer
    foreign = Signer(kid=signer.kid)  # a key nobody published, claiming the published key's id
    now = int(time.time())
    return {
        "wrong_audience": lambda: signer.sign(claims(aud="somebody-else")),
        "wrong_issuer": lambda: signer.sign(claims(iss="urn:somebody:else")),
        "wrong_typ": lambda: signer.sign(claims(), typ="JWT"),
        "foreign_key": lambda: foreign.sign(claims()),
        "expired": lambda: signer.sign(claims(iat=now - 120, nbf=now - 120, exp=now - 60)),
        "not_yet_valid": lambda: signer.sign(claims(iat=now + 120, nbf=now + 120, exp=now + 150)),
        "an_external_rs256_token": _external_rs256_token,
        "alg_none": lambda: jwt.encode(claims(), None, algorithm="none", headers={"kid": signer.kid, "typ": ii.IDENTITY_TYP}),
        "hmac_with_a_shared_secret": lambda: jwt.encode(claims(), "x" * 32, algorithm="HS256", headers={"kid": signer.kid, "typ": ii.IDENTITY_TYP}),
        "no_tenant": lambda: signer.sign(claims(tenant=None)),
        "empty_tenant": lambda: signer.sign(claims(tenant="")),
        "blank_tenant": lambda: signer.sign(claims(tenant="   ")),
        "tenant_not_text": lambda: signer.sign(claims(tenant=7)),
        "no_subject": lambda: signer.sign(claims(sub=None)),
        "subject_not_text": lambda: signer.sign(claims(sub=["a"])),
        "no_expiry": lambda: signer.sign(claims(exp=None)),
        "no_not_before": lambda: signer.sign(claims(nbf=None)),
        "unknown_kid": lambda: signer.sign(claims(), kid="never-published"),
        "no_kid": lambda: jwt.encode(claims(), signer._private, algorithm="EdDSA", headers={"typ": ii.IDENTITY_TYP}),
        "tampered_payload": lambda: _tamper(signer.sign(claims(tenant="acme"))),
        "not_a_token": lambda: "this is not a token",
        "three_empty_parts": lambda: "..",
        "far_too_long": lambda: "a" * (ii.MAX_TOKEN_CHARS + 1),
    }[case]()


def _tamper(token: str) -> str:
    """The same header and signature over a payload that names another tenant."""
    import base64

    header, payload, signature = token.split(".")
    body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    body["tenant"] = "someone-elses"
    forged = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
    return ".".join([header, forged, signature])


REFUSALS = {
    "wrong_audience": "audience",
    "wrong_issuer": "issuer",
    "wrong_typ": "type",
    "foreign_key": "signature",
    "expired": "expired",
    "not_yet_valid": "not_yet_valid",
    "an_external_rs256_token": "algorithm",
    "alg_none": "algorithm",
    "hmac_with_a_shared_secret": "algorithm",
    "no_tenant": "claims",
    "empty_tenant": "claims",
    "blank_tenant": "claims",
    "tenant_not_text": "claims",
    "no_subject": "claims",
    "subject_not_text": "claims",
    "no_expiry": "claims",
    "no_not_before": "claims",
    "unknown_kid": "unknown_kid",
    "no_kid": "unknown_kid",
    "tampered_payload": "signature",
    "not_a_token": "malformed",
    "three_empty_parts": "malformed",
    "far_too_long": "malformed",
}


@pytest.mark.parametrize("case,outcome", REFUSALS.items(), ids=REFUSALS.keys())
async def test_an_identity_that_is_wrong_is_refused_for_its_own_reason_and_that_reason_is_counted(case, outcome):
    door, _clock, _keys, verifier = _setup()
    before = _count(outcome)

    verdict = await verifier.verify(_token_for(case, door))

    assert verdict == Refusal(outcome), case
    assert _count(outcome) == before + 1


@pytest.mark.parametrize("missing", [None, ""], ids=["no header", "empty header"])
async def test_no_identity_at_all_is_refused_as_missing(missing):
    _door, _clock, _keys, verifier = _setup()
    before = _count("missing")

    assert await verifier.verify(missing) == Refusal("missing")
    assert _count("missing") == before + 1


def test_the_reasons_the_tests_expect_are_the_reasons_the_metric_can_carry():
    assert set(REFUSALS.values()) | {"missing", "ok", "keys_unavailable", "unexpected"} <= ii.OUTCOMES


async def test_the_refusal_and_the_logs_never_carry_the_token(caplog):
    door, _clock, _keys, verifier = _setup()
    token = door.signer.sign(claims(aud="somebody-else"))
    caplog.set_level(logging.DEBUG)

    verdict = await verifier.verify(token)

    assert verdict == Refusal("audience")
    assert token not in repr(verdict) and token not in caplog.text
    assert token.split(".")[1] not in caplog.text, "nor any claim of it"


async def test_a_verifier_that_fails_for_a_reason_nobody_foresaw_refuses_instead_of_raising(monkeypatch):
    door, _clock, _keys, verifier = _setup()

    def boom(*_args, **_kwargs):
        raise RuntimeError("something nobody foresaw: do not echo me")

    monkeypatch.setattr(ii.jwt, "decode", boom)
    before = _count("unexpected")

    verdict = await verifier.verify(door.signer.sign(claims()))

    assert verdict == Refusal("unexpected")
    assert _count("unexpected") == before + 1


# --- the public-key cache ---------------------------------------------------------------------------------------------------------------


async def test_the_keys_are_fetched_once_however_many_identities_arrive():
    door, _clock, _keys, verifier = _setup()

    for _ in range(20):
        assert isinstance(await verifier.verify(door.signer.sign(claims())), Identity)

    assert door.fetches == 1


async def test_a_key_rotated_in_is_picked_up_on_its_first_unknown_kid():
    door, clock, _keys, verifier = _setup()
    assert isinstance(await verifier.verify(door.signer.sign(claims())), Identity)
    successor = Signer(kid="k2")
    door.signers.append(successor)
    clock.advance(MIN_REFRESH + 1)

    assert isinstance(await verifier.verify(successor.sign(claims())), Identity)
    assert door.fetches == 2


async def test_identities_with_key_ids_nobody_published_cannot_make_the_api_hammer_the_front_door():
    door, clock, _keys, verifier = _setup()
    await verifier.verify(door.signer.sign(claims()))  # the first fetch
    assert door.fetches == 1

    for i in range(200):
        assert await verifier.verify(door.signer.sign(claims(), kid=f"made-up-{i}")) == Refusal("unknown_kid")

    assert door.fetches == 1  # all inside the refresh interval

    clock.advance(MIN_REFRESH + 1)
    await verifier.verify(door.signer.sign(claims(), kid="made-up-again"))
    assert door.fetches == 2  # one more once the interval has passed, and only one


async def test_when_the_front_door_cannot_be_reached_the_last_keys_keep_working_for_the_grace_and_not_a_second_longer():
    door, clock, _keys, verifier = _setup()
    assert isinstance(await verifier.verify(door.signer.sign(claims())), Identity)
    door.fail_with = 503

    clock.advance(TTL + 1)  # past the TTL: a refresh is due, and it fails
    assert isinstance(await verifier.verify(door.signer.sign(claims())), Identity), "inside the grace the last keys still count"

    clock.advance(GRACE)  # past TTL + grace
    before = _count("keys_unavailable")
    verdict = await verifier.verify(door.signer.sign(claims()))

    assert verdict == Refusal("keys_unavailable", unavailable=True), "fail closed: a key set that cannot be confirmed current is not trusted"
    assert _count("keys_unavailable") == before + 1


async def test_the_api_recovers_by_itself_when_the_front_door_comes_back():
    door, clock, _keys, verifier = _setup()
    await verifier.verify(door.signer.sign(claims()))
    door.fail_with = 503
    clock.advance(TTL + GRACE + 1)
    assert (await verifier.verify(door.signer.sign(claims()))).unavailable  # type: ignore[union-attr]  # a Refusal here

    door.fail_with = None
    clock.advance(MIN_REFRESH + 1)

    assert isinstance(await verifier.verify(door.signer.sign(claims())), Identity)


async def test_with_no_keys_ever_fetched_the_api_refuses_as_unavailable_not_as_a_bad_identity():
    door, _clock, _keys, verifier = _setup()
    door.fail_with = 500

    verdict = await verifier.verify(door.signer.sign(claims()))

    assert verdict == Refusal("keys_unavailable", unavailable=True)


async def test_every_fetch_is_counted_by_result():
    door, clock, _keys, verifier = _setup()
    ok_before = metric_value(metrics.agent_api_identity_key_refresh_total, result="ok")
    err_before = metric_value(metrics.agent_api_identity_key_refresh_total, result="error")

    await verifier.verify(door.signer.sign(claims()))
    door.fail_with = 500
    clock.advance(TTL + 1)
    await verifier.verify(door.signer.sign(claims()))

    assert metric_value(metrics.agent_api_identity_key_refresh_total, result="ok") == ok_before + 1
    assert metric_value(metrics.agent_api_identity_key_refresh_total, result="error") == err_before + 1


@pytest.mark.parametrize(
    "body",
    [b"not json", {"keys": []}, {"keys": [{"kty": "OKP"}]}, {"nope": 1}, [1, 2]],
    ids=["not json", "no keys", "a key without kid or material", "no keys member", "wrong shape"],
)
async def test_a_front_door_that_answers_rubbish_is_a_failed_fetch_and_does_not_replace_the_good_keys(body):
    door, clock, _keys, verifier = _setup()
    assert isinstance(await verifier.verify(door.signer.sign(claims())), Identity)
    good = door.signer
    door.body = body
    clock.advance(TTL + 1)

    assert isinstance(await verifier.verify(good.sign(claims())), Identity), "the previous keys survive, inside the grace"


async def test_one_unusable_key_in_the_set_does_not_cost_the_others():
    door, _clock, _keys, verifier = _setup()
    door.body = {"keys": [{"kty": "EC", "crv": "nonsense", "kid": "bad"}, door.signer.jwk]}

    assert isinstance(await verifier.verify(door.signer.sign(claims())), Identity)


def test_the_default_client_gives_up_after_a_short_deadline():
    keys = InternalKeys(URL, ttl_seconds=TTL, stale_grace_seconds=GRACE)

    assert keys._client.timeout.read == ii.KEYS_FETCH_TIMEOUT_SECONDS == 5.0


async def test_closing_closes_a_client_the_keys_made_but_not_one_they_were_given():
    door = FrontDoor()
    given = door.client()
    await InternalKeys(URL, ttl_seconds=TTL, stale_grace_seconds=GRACE, client=given).aclose()
    assert not given.is_closed

    made = InternalKeys(URL, ttl_seconds=TTL, stale_grace_seconds=GRACE)
    await made.aclose()
    assert made._client.is_closed

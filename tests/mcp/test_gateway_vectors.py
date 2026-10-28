"""The MCP gateway's verifier against the shared external-token vectors (specs/018, tasks T065 and T066).

`testdata/auth-vectors/` is one file of tokens read by two verifiers in two languages: the Go service at the front door
(`services/authlimit/internal/extjwt`) and this one, `app/mcp/gateway_auth.py`. Each vector says what the `mcp` profile must conclude, so the
two cannot quietly disagree about what a valid token is. The reason a vector gives for the `mcp` profile is informational (the gateway counts its
own words); only `result` is compared.

Why it matters beyond tidiness. The front door runs in limit-only mode on the MCP routes (profile `identify`), and the rule the vectors README
states is that this mode never refuses a token the gateway accepts: a token it refused would reach the gateway without being charged to any
bucket, a way around the limiter. `test_the_limit_only_profile_never_refuses_what_the_gateway_accepts` re-checks that rule from the data, and
the Go tests check it from theirs.

No network and no real clock. The signing keys are the vectors' `jwks.json`, served to the gateway's own `JwksCache` through a mock transport
(the same approach as tests/mcp/test_gateway_keys.py). PyJWT reads the wall clock inside `jwt.api_jwt` for `exp`, `nbf` and `iat`, so the test
replaces the `datetime` it imports with one that answers the file's `now`: every `exp` in the file is relative to that instant and none ever
expires, which is the point of putting the clock in the file.
"""
import inspect
import json
from datetime import datetime as real_datetime
from pathlib import Path

import httpx
import jwt
import pytest

from app.mcp.gateway_auth import JwksCache, JwtVerifier

VECTORS_DIR = Path(__file__).resolve().parents[2] / "testdata" / "auth-vectors"
FILE = json.loads((VECTORS_DIR / "vectors.json").read_text())
JWKS = json.loads((VECTORS_DIR / "jwks.json").read_text())
CONFIG = FILE["config"]
NOW = FILE["now"]
VECTORS = FILE["vectors"]


class _FrozenDatetime(real_datetime):
    """`datetime.now(tz)` answers the vectors' instant; everything else is the real class."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]  # signature matches datetime.now; only the answer is fixed
        return real_datetime.fromtimestamp(NOW, tz=tz)


@pytest.fixture(autouse=True)
def _the_clock_is_the_files(monkeypatch):
    monkeypatch.setattr(jwt.api_jwt, "datetime", _FrozenDatetime)


def _verifier() -> JwtVerifier:
    """A gateway endpoint configured exactly as the vectors were designed against (`config` in the file)."""

    def serve_jwks(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=JWKS)

    keys = JwksCache(
        "http://127.0.0.1:1/.well-known/jwks",
        ttl_seconds=300,
        client=httpx.AsyncClient(transport=httpx.MockTransport(serve_jwks)),
        clock=lambda: float(NOW),
    )
    return JwtVerifier(
        server="ecorp",
        issuer=CONFIG["issuer"],
        audience=CONFIG["mcp_audience"],
        keys=keys,
        leeway_seconds=CONFIG["leeway_seconds"],
    )


def test_the_file_is_what_this_test_assumes():
    assert len(VECTORS) == len({v["name"] for v in VECTORS}), "vector names are unique"
    assert {"valid", "refused"} == {v["expect"]["mcp"]["result"] for v in VECTORS}, "premise: both verdicts are exercised"
    assert all({"chat", "identify", "mcp"} <= set(v["expect"]) for v in VECTORS)


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
async def test_the_gateway_reaches_the_verdict_the_vector_expects(vector):
    expect = vector["expect"]["mcp"]

    access = await _verifier().verify_token(vector["token"])

    got = "valid" if access is not None else "refused"
    assert got == expect["result"], f"{vector['name']}: {vector['note']} (the file expects {expect})"


def test_the_limit_only_profile_never_refuses_what_the_gateway_accepts():
    """A token the front door's limit-only mode refuses is never charged to a bucket, yet the gateway would serve it."""
    gaps = [
        v["name"]
        for v in VECTORS
        if v["expect"]["mcp"]["result"] == "valid" and v["expect"]["identify"]["result"] != "valid"
    ]

    assert not gaps, f"accepted by the gateway but refused by limit-only mode, so never counted: {gaps}"


def test_the_gateways_clock_tolerance_is_no_wider_than_the_one_the_vectors_were_designed_against():
    """The one thing the vectors cannot show. A token the front door's limit-only mode cannot identify goes on to the gateway uncounted, so the
    gateway must not accept a token for longer past its expiry than the service does. The service's tolerance is a setting (`AUTHLIMIT_CLOCK_LEEWAY`,
    10 seconds by default, the file's `leeway_seconds`); the gateway's is a constant of its verifier. This pins the constant under the default. An
    operator who sets the service's below the gateway's opens a window of the difference (contracts/authlimit.md, A.1)."""
    default = inspect.signature(JwtVerifier).parameters["leeway_seconds"].default

    assert default <= CONFIG["leeway_seconds"]

"""scripts/authlimit_smoke.py's `--upstream api` (specs/018, T068): which stages run, the forgery it makes, and the two helpers that talk to the API.

The smoke itself is a manual tier (it needs the whole stack). What is checkable without it is the logic around it, and that logic is where a smoke
quietly stops proving something: a stage selection that lets a stage run against an upstream it cannot say anything about, a forged identity that
is not actually valid (so "the door replaced it" proves nothing), a helper that waits on a stream it should have closed.
"""
import argparse
import json
import ssl
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from scripts import authlimit_smoke as smoke

# --- which stages run ---------------------------------------------------------------------------------------------------------------------


def test_behind_the_stub_the_default_is_the_old_safe_list_and_all_adds_the_destructive_ones():
    assert smoke.select_stages("echo", all_stages=False) == smoke.SAFE
    assert smoke.select_stages("echo", all_stages=True) == smoke.SAFE + smoke.DESTRUCTIVE


def test_behind_the_real_api_the_stages_that_read_the_stubs_report_are_left_out_and_api_joins():
    default = smoke.select_stages("api", all_stages=False)

    assert default == ["refusals", "api", "limits", "mcp", "revoke", "logs"]
    assert not set(default) & smoke.ECHO_ONLY
    everything = smoke.select_stages("api", all_stages=True)
    assert "rotate" not in everything and "replica" in everything and everything[-1] == "apikeys", "the stages that stop the door come after the safe ones"


@pytest.mark.parametrize("stage", sorted(smoke.ECHO_ONLY))
def test_asking_for_a_stage_that_reads_the_stub_behind_the_real_api_is_an_error_not_a_stage_that_checks_nothing(stage):
    with pytest.raises(ValueError, match="read what the stub"):
        smoke.select_stages("api", all_stages=False, chosen=stage)


@pytest.mark.parametrize("stage", ["api", "apikeys"])
def test_a_stage_that_tests_the_api_behind_the_stub_is_an_error_and_so_is_a_stage_that_does_not_exist(stage):
    with pytest.raises(ValueError, match="--upstream api"):
        smoke.select_stages("echo", all_stages=False, chosen=stage)
    with pytest.raises(ValueError, match="unknown"):
        smoke.select_stages("api", all_stages=False, chosen="refusals,nonsense")



def test_every_stage_has_a_runner():
    import inspect

    source = inspect.getsource(smoke.run)
    for stage in smoke.SAFE + smoke.DESTRUCTIVE + smoke.API_ONLY:
        assert f'"{stage}": lambda' in source, stage


# --- the forged identity ---------------------------------------------------------------------------------------------------------------


def _keys_dir(tmp_path: Path, *, retired: bool = False) -> tuple[Path, ed25519.Ed25519PublicKey]:
    key = ed25519.Ed25519PrivateKey.generate()
    now = int(time.time())
    (tmp_path / "kid1.pem").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    (tmp_path / "keyset.json").write_text(
        json.dumps({"keys": [{"kid": "kid1", "publish_at": now - 100, "use_from": now - 90, "retire_at": (now - 10) if retired else 0}]})
    )
    return tmp_path, key.public_key()


def test_the_forged_identity_is_a_valid_one_so_the_door_replacing_it_proves_something(tmp_path):
    keys_dir, public = _keys_dir(tmp_path)

    token = smoke.mint_identity(keys_dir, "other-co", "someone-else")

    assert token is not None
    claims = smoke.verify_identity(token, {"kid1": public})
    assert claims is not None and (claims["tenant"], claims["sub"]) == ("other-co", "someone-else"), "the smoke's own verifier accepts it: it is not a bad forgery"
    assert jwt.get_unverified_header(token) == {"alg": "EdDSA", "kid": "kid1", "typ": "authlimit-identity+jwt"}


def test_with_no_usable_key_nothing_is_minted(tmp_path):
    keys_dir, _public = _keys_dir(tmp_path, retired=True)

    assert smoke.mint_identity(keys_dir, "other-co", "x") is None


# --- the helpers that talk to the API ----------------------------------------------------------------------------------------------------


def _door(handler, upstream: str = "api") -> smoke.Door:
    args = argparse.Namespace(api="https://api.test", mcp="https://mcp.test", auth="https://auth.test", compose="", keys_dir="", upstream=upstream)
    door = smoke.Door(args, ssl.create_default_context(), "unused")
    door.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return door


def _sse(*events: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


async def test_a_turn_behind_the_real_api_carries_a_real_body_and_a_bearer_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(path=request.url.path, auth=request.headers["authorization"], body=json.loads(request.content))
        return httpx.Response(200, content=_sse({"type": "done"}))

    response = await _door(handler).turn("TOKEN", message="hi", thread="t1")

    assert response.status_code == 200
    assert seen == {"path": "/chat/stream/queued", "auth": "Bearer TOKEN", "body": {"message": "hi", "thread_id": "t1"}}


async def test_a_refused_turn_keeps_its_body_so_the_envelope_can_be_read():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"code": "rate_limited"}, headers={"Retry-After": "3"})

    response = await _door(handler).turn("TOKEN")

    assert response.status_code == 429 and response.json()["code"] == "rate_limited" and response.headers["retry-after"] == "3"


async def test_a_turn_behind_the_stub_sends_what_it_always_did_no_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(content=request.content)
        return httpx.Response(200, json={"headers": {}})

    await _door(handler, upstream="echo").turn("TOKEN")

    assert seen == {"content": b""}


async def test_asking_reads_the_streamed_answer_to_its_end():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse({"type": "token", "content": "The "}, {"type": "token", "content": "answer"}, {"type": "done"}))

    assert await _door(handler).ask("TOKEN", "q") == (200, "The answer")


async def test_a_stream_that_ends_in_an_error_event_is_reported_as_one_not_as_an_empty_answer():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse({"type": "token", "content": "partial"}, {"type": "error", "code": "timeout"}))

    status, answer = await _door(handler).ask("TOKEN", "q")

    assert status == 200 and answer.startswith("ERROR ") and "timeout" in answer


async def test_asking_without_a_token_sends_no_authorization_and_returns_the_doors_refusal():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(401, json={"code": "unauthenticated"})

    status, body = await _door(handler).ask(None, "q")

    assert status == 401 and "unauthenticated" in body


async def test_cancelling_behind_the_real_api_names_a_thread_because_it_needs_one():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(body=request.content)
        return httpx.Response(404, json={"detail": "session not found"})

    response = await _door(handler).cancel("TOKEN")

    assert response.status_code == 404 and json.loads(seen["body"]) == {"thread_id": "smoke-none"}

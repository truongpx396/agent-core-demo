"""POST /v1/embeddings and GET /v1/models against the real app, lifespan, registry and batching, with a fake ONNX session (no download).

The fake's hidden state for a token with id `i` is `[i, 2i, 3i, 4i]` (tests/ml_service/conftest.py), so with CLS pooling "hello" (id 2) is
[2, 4, 6, 8] and every expected number below is worked out by hand. The compatibility tests at the end are the point of the endpoint:
the official `openai` SDK and LangChain's `OpenAIEmbeddings` (what this app uses) must accept what it returns.
"""
import asyncio
import base64
import json

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from langchain_openai import OpenAIEmbeddings
from openai import AsyncOpenAI

from ml_service.app import create_app
from ml_service.embed import EmbedService
from ml_service.onnx_text import OnnxText
from ml_service.registry import ModelSpec
from ml_service.settings import Settings
from ml_service.text_model import TextModel
from tests.ml_service.conftest import FakeSession, make_tokenizer

NAME = "test-embed"
UNIT = np.array([2, 4, 6, 8], dtype=np.float32) / np.linalg.norm([2, 4, 6, 8])  # "hello"
FOO = np.array([4, 8, 12, 16], dtype=np.float32) / np.linalg.norm([4, 8, 12, 16])


def _spec(**kw) -> ModelSpec:
    base = dict(name=NAME, roles=("embed",), repo="r/r", file="f.onnx", revision="a" * 40, pooling="cls", normalize=True, max_length=8,
                pad_token="[PAD]", licence="MIT", needs_prefix={"query": "q: ", "document": ""})
    return ModelSpec(**{**base, **kw})


def _settings(**kw) -> Settings:
    base = dict(rerank_model_name="x", guard_model_repo="x", rerank_max_batch_size=8, guard_max_batch_size=8, max_batch_wait_ms=2,
                onnx_intra_op_threads=1, model_retry_base_s=0.02, model_retry_max_s=0.05, models_enabled=(), embed_max_items=4,
                embed_max_chars=40, embed_batch_size=8)
    return Settings(**{**base, **kw})


def _service(session=None, spec=None, settings=None) -> tuple[EmbedService, FakeSession]:
    session = session or FakeSession()
    spec = spec or _spec()
    text = OnnxText(session, make_tokenizer(), pooling=spec.pooling, normalize=spec.normalize, max_length=spec.max_length, pad_token=spec.pad_token)  # type: ignore[arg-type]
    s = settings or _settings()
    return EmbedService(TextModel(spec, text, 4, "a" * 40), max_batch_size=s.embed_batch_size, max_wait_ms=s.max_batch_wait_ms), session


def _app(loader=None, settings=None, spec=None):
    spec = spec or _spec()
    settings = settings or _settings()
    loaders = {NAME: loader or (lambda: _service(spec=spec, settings=settings)[0])}
    return create_app(settings, specs=[spec], loaders={"rerank": lambda: _NoopService(), "prompt-guard": lambda: _NoopService(), **loaders})


class _NoopService:
    async def start(self):
        return None

    async def stop(self):
        return None


def _post(client, body, **kw):
    return client.post("/v1/embeddings", content=json.dumps(body), headers={"content-type": "application/json"}, **kw)


# ---- the response -----------------------------------------------------------------------------------------------------------
def test_a_list_input_returns_one_embedding_per_item_in_order_with_usage():
    with TestClient(_app()) as client:
        response = _post(client, {"model": NAME, "input": ["hello", "foo"]})

    body = response.json()
    assert response.status_code == 200 and body["object"] == "list" and body["model"] == NAME
    assert [(d["object"], d["index"]) for d in body["data"]] == [("embedding", 0), ("embedding", 1)]
    np.testing.assert_allclose(body["data"][0]["embedding"], UNIT, rtol=1e-5)
    np.testing.assert_allclose(body["data"][1]["embedding"], FOO, rtol=1e-5)
    assert body["usage"] == {"prompt_tokens": 2, "total_tokens": 2}  # one real token each; padding is not counted


def test_a_single_string_input_is_accepted():
    with TestClient(_app()) as client:
        body = _post(client, {"model": NAME, "input": "hello"}).json()
    assert len(body["data"]) == 1 and body["data"][0]["index"] == 0


def test_base64_decodes_to_exactly_the_float_vector():
    with TestClient(_app()) as client:
        as_float = _post(client, {"model": NAME, "input": ["hello"]}).json()["data"][0]["embedding"]
        as_b64 = _post(client, {"model": NAME, "input": ["hello"], "encoding_format": "base64"}).json()["data"][0]["embedding"]

    decoded = np.frombuffer(base64.b64decode(as_b64), dtype="<f4")
    np.testing.assert_array_equal(decoded, np.asarray(as_float, dtype=np.float32))  # same bits, not merely close


def test_the_number_of_truncated_inputs_is_reported_in_a_header():
    with TestClient(_app(spec=_spec(max_length=2))) as client:
        response = _post(client, {"model": NAME, "input": ["hello world foo bar", "hello", "hello world foo"]})

    assert response.status_code == 200 and response.headers["X-Truncated-Items"] == "2"  # two of three were longer than max_length


# ---- rejections: every code in the contract ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"input": "x"}, 400, "invalid_input"),  # no model
        ({"model": NAME}, 400, "invalid_input"),  # no input
        ({"model": NAME, "input": []}, 400, "invalid_input"),
        ({"model": NAME, "input": ""}, 400, "invalid_input"),
        ({"model": NAME, "input": ["ok", ""]}, 400, "invalid_input"),
        ({"model": NAME, "input": ["ok", 7]}, 400, "invalid_input"),
        ({"model": NAME, "input": "hello", "encoding_format": "hex"}, 400, "invalid_input"),
        ({"model": NAME, "input": "hello", "dimensions": 256}, 400, "dimensions_unsupported"),
        ({"model": "nope", "input": "hello"}, 404, "model_not_found"),
        ({"model": NAME, "input": ["a"] * 5}, 400, "batch_too_large"),  # the limit in these tests is 4
        ({"model": NAME, "input": ["fine", "x" * 41]}, 400, "input_too_long"),  # the limit is 40 characters
    ],
)
def test_every_rejection_has_the_contracts_status_and_code_and_the_error_envelope(body, status, code):
    with TestClient(_app()) as client:
        response = _post(client, body)

    assert response.status_code == status
    assert set(response.json()) == {"error"} and response.json()["error"]["code"] == code and response.json()["error"]["message"]


def test_the_unknown_model_message_lists_what_is_served_and_the_too_long_one_names_the_input():
    with TestClient(_app()) as client:
        assert NAME in _post(client, {"model": "nope", "input": "x"}).json()["error"]["message"]
        assert "input 1" in _post(client, {"model": NAME, "input": ["ok", "x" * 41]}).json()["error"]["message"]


def test_a_body_that_is_not_json_is_a_400_not_a_500():
    with TestClient(_app()) as client:
        response = client.post("/v1/embeddings", content=b"{not json", headers={"content-type": "application/json"})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_input"


def test_a_model_that_failed_to_load_is_503_with_retry_after_and_the_service_still_answers_others():
    def broken():
        raise OSError("weights missing")

    with TestClient(_app(loader=broken)) as client:
        response = _post(client, {"model": NAME, "input": "hello"})
        health = client.get("/health").status_code

    assert response.status_code == 503 and response.json()["error"]["code"] == "model_unavailable"
    assert int(response.headers["Retry-After"]) >= 1 and health == 200


def test_a_failure_while_embedding_is_that_requests_500_not_the_models_death():
    service, session = _service()
    original = session.run
    state = {"fail": True}

    def flaky(names, feed):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("onnxruntime exploded")
        return original(names, feed)

    session.run = flaky  # type: ignore[method-assign]
    with TestClient(_app(loader=lambda: service)) as client:
        first = _post(client, {"model": NAME, "input": "hello"})
        second = _post(client, {"model": NAME, "input": "hello"})

    assert first.status_code == 500 and first.json()["error"]["code"] == "internal"
    assert second.status_code == 200  # the model is still ready: one bad request does not take it down


# ---- /v1/models --------------------------------------------------------------------------------------------------------------
def test_the_models_list_documents_the_prefix_each_model_expects_without_applying_it():
    with TestClient(_app()) as client:
        body = client.get("/v1/models").json()

    (model,) = body["data"]
    assert model["id"] == NAME and model["state"] == "ready" and model["dim"] == 4 and model["licence"] == "MIT"
    assert model["needs_prefix"] == {"query": "q: ", "document": ""} and model["revision"] == "a" * 40
    # and the server did not apply it: "hello" with the documented query prefix is a different text, so a different vector
    with TestClient(_app()) as client:
        plain = _post(client, {"model": NAME, "input": "hello"}).json()["data"][0]["embedding"]
    np.testing.assert_allclose(plain, UNIT, rtol=1e-5)


# ---- metrics ----------------------------------------------------------------------------------------------------------------
def test_requests_items_and_errors_are_counted_per_model():
    with TestClient(_app()) as client:
        _post(client, {"model": NAME, "input": ["hello", "foo"]})
        _post(client, {"model": NAME, "input": ["hello"]})
        _post(client, {"model": NAME, "input": ""})
        text = client.get("/metrics").text

    assert f'ml_embed_requests_total{{model="{NAME}"}} 2.0' in text
    assert f'ml_embed_items_total{{model="{NAME}"}} 3.0' in text
    assert f'ml_embed_errors_total{{code="invalid_input",model="{NAME}"}} 1.0' in text
    assert f'ml_embed_batch_size_count{{model="{NAME}"}} 2.0' in text


# ---- concurrency -------------------------------------------------------------------------------------------------------------
async def test_concurrent_requests_share_model_passes():
    service, session = _service(settings=_settings(max_batch_wait_ms=60))
    app = _app(loader=lambda: service, settings=_settings(max_batch_wait_ms=60))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            responses = await asyncio.gather(*(client.post("/v1/embeddings", json={"model": NAME, "input": ["hello", "foo"]}) for _ in range(6)))

    assert all(r.status_code == 200 for r in responses)
    assert len(session.calls) <= 2  # twelve texts from six requests went through the model in one or two passes, not twelve


# ---- compatibility with the clients that will really call it -----------------------------------------------------------------
class _Recording(httpx.AsyncBaseTransport):
    """Wraps the in-process ASGI transport and remembers each request body, so we can prove what the SDK really sent."""

    def __init__(self, app):
        self.inner = httpx.ASGITransport(app=app)
        self.bodies: list[dict] = []

    async def handle_async_request(self, request):
        self.bodies.append(json.loads(request.content))
        return await self.inner.handle_async_request(request)


async def test_the_official_openai_sdk_with_default_arguments_parses_the_response():
    """The SDK requests base64 when the caller does not say otherwise and decodes it itself; if our base64 were
    wrong this returns garbage or raises.
    """
    app = _app()
    transport = _Recording(app)
    async with app.router.lifespan_context(app):
        http = httpx.AsyncClient(transport=transport, base_url="http://t/v1")
        sdk = AsyncOpenAI(base_url="http://t/v1", api_key="unused", http_client=http)
        result = await sdk.embeddings.create(model=NAME, input=["hello", "foo"])

    assert transport.bodies[0].get("encoding_format") == "base64"  # the SDK's default, verified rather than assumed
    np.testing.assert_allclose(result.data[0].embedding, UNIT, rtol=1e-5)
    np.testing.assert_allclose(result.data[1].embedding, FOO, rtol=1e-5)
    assert result.usage.prompt_tokens == 2 and result.model == NAME


async def test_the_sdk_asking_for_floats_gets_the_same_numbers():
    app = _app()
    async with app.router.lifespan_context(app):
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t/v1")
        sdk = AsyncOpenAI(base_url="http://t/v1", api_key="unused", http_client=http)
        floats = await sdk.embeddings.create(model=NAME, input=["hello"], encoding_format="float")
        b64 = await sdk.embeddings.create(model=NAME, input=["hello"])

    np.testing.assert_array_equal(np.asarray(floats.data[0].embedding, dtype=np.float32), np.asarray(b64.data[0].embedding, dtype=np.float32))


async def test_langchains_openai_embeddings_the_client_this_app_uses_works_against_it():
    app = _app()
    async with app.router.lifespan_context(app):
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t/v1")
        client = OpenAIEmbeddings(
            model=NAME, base_url="http://t/v1", api_key="unused", http_async_client=http, check_embedding_ctx_length=False  # type: ignore[arg-type]
        )
        documents = await client.aembed_documents(["hello", "foo"])
        query = await client.aembed_query("hello")

    np.testing.assert_allclose(documents[0], UNIT, rtol=1e-5)
    np.testing.assert_allclose(documents[1], FOO, rtol=1e-5)
    np.testing.assert_allclose(query, UNIT, rtol=1e-5)

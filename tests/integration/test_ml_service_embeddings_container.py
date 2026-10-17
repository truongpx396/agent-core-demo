"""/v1/embeddings served by the REAL ML service image (specs/014 T065): real weights, the real registry, the real limits, and the
isolation claim (one broken model must not take the others down).

Self-skips (never fails) without Docker, like every `integration` test.
"""
import base64
import time

import httpx
import numpy as np
import pytest

pytestmark = pytest.mark.integration


def _embed(url, texts, **extra):
    return httpx.post(f"{url}/v1/embeddings", json={"model": "bge-small-en-v1.5", "input": texts, **extra}, timeout=60)


def test_the_default_model_is_served_with_its_pinned_revision_and_dimension(ml_service_url):
    models = httpx.get(f"{ml_service_url}/v1/models", timeout=10).json()["data"]
    health = httpx.get(f"{ml_service_url}/health/models", timeout=10).json()

    (bge,) = [m for m in models if m["id"] == "bge-small-en-v1.5"]
    assert bge["state"] == "ready" and bge["dim"] == 384 and bge["licence"] == "MIT"
    assert bge["revision"] == "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    assert health["models"]["bge-small-en-v1.5"]["state"] == "ready" and health["models"]["bge-small-en-v1.5"]["dim"] == 384
    # the two classifier models are loaded by default and both embed; the big embedder is declared but not enabled
    assert [m["id"] for m in models] == ["bge-small-en-v1.5", "all-MiniLM-L6-v2"]


def test_it_returns_unit_vectors_in_order_with_usage_and_related_texts_are_closer(ml_service_url):
    texts = ["How do I reset my password?", "To reset your password open Settings.", "Photosynthesis happens in chloroplasts."]
    body = _embed(ml_service_url, texts).json()

    vectors = np.array([d["embedding"] for d in body["data"]])
    assert [d["index"] for d in body["data"]] == [0, 1, 2] and vectors.shape == (3, 384)
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, rtol=1e-4)
    assert float(vectors[0] @ vectors[1]) > float(vectors[0] @ vectors[2]) + 0.1
    assert body["usage"]["prompt_tokens"] > 0 and body["usage"]["total_tokens"] == body["usage"]["prompt_tokens"]


def test_base64_is_the_same_vector_as_floats(ml_service_url):
    as_float = np.array(_embed(ml_service_url, ["hello world"]).json()["data"][0]["embedding"], dtype=np.float32)
    encoded = _embed(ml_service_url, ["hello world"], encoding_format="base64").json()["data"][0]["embedding"]
    np.testing.assert_array_equal(np.frombuffer(base64.b64decode(encoded), dtype="<f4"), as_float)


def test_the_limits_in_the_contract_are_enforced_by_the_image(ml_service_url):
    too_many = _embed(ml_service_url, ["x"] * 257)  # ML_EMBED_MAX_ITEMS defaults to 256
    too_long = _embed(ml_service_url, ["fine", "y" * 8001])  # ML_EMBED_MAX_CHARS defaults to 8000
    unknown = httpx.post(f"{ml_service_url}/v1/embeddings", json={"model": "nope", "input": "x"}, timeout=10)

    assert (too_many.status_code, too_many.json()["error"]["code"]) == (400, "batch_too_large")
    assert (too_long.status_code, too_long.json()["error"]["code"]) == (400, "input_too_long") and "input 1" in too_long.json()["error"]["message"]
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "model_not_found")


def test_a_hundred_concurrent_small_requests_all_succeed(ml_service_url):
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        responses = list(pool.map(lambda i: _embed(ml_service_url, [f"concurrent request number {i}"]), range(100)))

    assert all(r.status_code == 200 for r in responses)


def test_memory_with_all_models_loaded_leaves_room_for_another_model_inside_the_production_limit(ml_service, record_property):
    """Production gives this container `mem_limit: 2g`. Measured on this change (docker stats): the old image (reranker + guard) idles at
    574 MiB; the new one (plus bge-small, fp32) idles at 781 MiB (+207 MiB); after 100 concurrent requests, which grow the model
    arenas, it reached 1,069 MiB. That is 52% of the limit at peak, so the bound here is 75% (1,536 MiB): it leaves room for one more
    model to be enabled (ML_MODELS_ENABLED) and fails if something makes the service grow without bound. The current figure is recorded."""
    import docker

    stats = docker.from_env().containers.get(ml_service["container_id"]).stats(stream=False)
    used_mb = stats["memory_stats"]["usage"] / (1024 * 1024)
    record_property("ml_service_memory_mb", round(used_mb))
    assert used_mb < 1536


def test_one_model_that_cannot_load_does_not_take_the_other_endpoints_down(tmp_path):
    """FR-024 on the real image. Before the package split either model failing to load stopped the whole container. Here the registry
    names a file that does not exist: that model must be `failed` and 503, and /rerank must still answer."""
    from testcontainers.core.container import DockerContainer

    registry = tmp_path / "models.toml"
    registry.write_text(
        '[[model]]\nname = "bge-small-en-v1.5"\nroles = ["embed"]\nrepo = "BAAI/bge-small-en-v1.5"\nfile = "onnx/this-file-does-not-exist.onnx"\n'
        'revision = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"\npooling = "cls"\nmax_length = 512\npad_token = "[PAD]"\nlicence = "MIT"\n'
    )
    container = (
        DockerContainer("agent-core-demo-ml-service:test")
        .with_exposed_ports(80)
        .with_volume_mapping(str(registry), "/app/models.toml", "ro")
    )
    container.start()
    try:
        url = f"http://{container.get_container_host_ip()}:{int(container.get_exposed_port(80))}"
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            pytest.fail("the container never became live")

        # The supervisor retries a failed model after a backoff, and while a retry is in flight the state is honestly `loading`; a
        # snapshot taken at that instant is not the one this test is about. Wait for `failed` (the retry fails again within seconds).
        health = {}
        for _ in range(60):
            health = httpx.get(f"{url}/health/models", timeout=10).json()
            if health["models"]["bge-small-en-v1.5"]["state"] == "failed":
                break
            time.sleep(1)
        embed = httpx.post(f"{url}/v1/embeddings", json={"model": "bge-small-en-v1.5", "input": "x"}, timeout=10)
        rerank = httpx.post(f"{url}/rerank", json={"query": "q", "texts": ["a", "b"]}, timeout=60)

        assert health["status"] == "degraded" and health["models"]["bge-small-en-v1.5"]["state"] == "failed"
        assert health["models"]["bge-small-en-v1.5"]["retry_in_s"] >= 0 and health["models"]["bge-small-en-v1.5"]["reason"]
        assert health["models"]["rerank"]["state"] == "ready" and health["models"]["prompt-guard"]["state"] == "ready"
        # `failed` or mid-retry (`loading`), the answer is the same 503 with a Retry-After: never a hang, never zero vectors
        assert embed.status_code == 503 and embed.json()["error"]["code"] == "model_unavailable" and "Retry-After" in embed.headers
        assert rerank.status_code == 200  # the endpoint next door is untouched
    finally:
        container.stop()

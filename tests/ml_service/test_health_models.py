"""/health stays liveness; /health/models reports each model; /metrics exposes the state (contracts/ml-service.md)."""
import time

from fastapi.testclient import TestClient

from ml_service.app import create_app
from tests.ml_service.test_rerank_guard_contract import (
    _guard_loader,
    _rerank_loader,
    _settings,
)


def _app(**loaders):
    return create_app(_settings(), specs=[], loaders={"rerank": _rerank_loader, "prompt-guard": _guard_loader, **loaders})


def test_health_is_200_even_when_a_model_failed_to_load():
    def broken():
        raise OSError("gone")

    with TestClient(_app(rerank=broken)) as client:
        assert client.get("/health").json() == {"status": "ok"}  # Docker must not restart the container over one model


def test_health_models_lists_every_model_and_is_ok_when_all_are_ready():
    with TestClient(_app()) as client:
        body = client.get("/health/models").json()

    assert body["status"] == "ok"
    assert {name: m["state"] for name, m in body["models"].items()} == {"rerank": "ready", "prompt-guard": "ready"}
    assert body["models"]["rerank"]["roles"] == ["rerank"]
    assert "classifier" not in body and "labelsets" in body  # the trained-head path is gone (spec 016); label sets are what the matcher serves


def test_health_models_reports_a_failed_model_with_its_reason_and_when_it_will_be_retried():
    def broken():
        raise OSError("model file missing")

    with TestClient(_app(**{"prompt-guard": broken})) as client:
        body = client.get("/health/models").json()

    assert body["status"] == "degraded"
    guard = body["models"]["prompt-guard"]
    assert guard["state"] == "failed" and "model file missing" in guard["reason"] and guard["retry_in_s"] >= 0
    assert body["models"]["rerank"]["state"] == "ready"


def test_the_metrics_endpoint_exposes_one_gauge_per_model():
    def broken():
        raise OSError("gone")

    with TestClient(_app(rerank=broken)) as client:
        text = client.get("/metrics").text

    assert 'ml_model_state{model="prompt-guard"} 1.0' in text
    assert 'ml_model_state{model="rerank"} 0.0' in text


def test_the_supervisor_reloads_a_failed_model_without_a_restart():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("not yet")
        return _rerank_loader()

    import ml_service.app as app_module

    original = app_module.SUPERVISOR_INTERVAL_S
    app_module.SUPERVISOR_INTERVAL_S = 0.01
    try:
        with TestClient(_app(rerank=flaky)) as client:
            assert client.get("/health/models").json()["models"]["rerank"]["state"] == "failed"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if client.get("/health/models").json()["models"]["rerank"]["state"] == "ready":
                    break
                time.sleep(0.02)
            state = client.get("/health/models").json()["models"]["rerank"]["state"]
            served = client.post("/rerank", json={"query": "q", "texts": ["a"]}).status_code
    finally:
        app_module.SUPERVISOR_INTERVAL_S = original

    assert state == "ready" and served == 200 and attempts["n"] == 2

"""GET /health/ready's `warnings` (specs/014 FR-019): visible conditions that must NOT make the API unready.

An embedding-identity mismatch refuses retrieval from one collection, which degrades answers but leaves every turn able to run. If it
turned the API 503, a load balancer would pull every replica out of rotation over a condition no replica can fix by restarting, and the
whole product would go down to protect one collection. So the condition is reported, and `status` and the HTTP code stay what the
dependency checks say.
"""
import asyncio

from fastapi import Response

from app.api import health
from app.api.routers import system
from app.retrieval import qdrant_store


def _status(state, expected=None, found=None):
    async def fake(_collection=None):
        return qdrant_store.IdentityStatus(state, expected, found)

    return fake


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _MlClient:
    def __init__(self, body=None, fails=False):
        self._body, self._fails = body, fails

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        if self._fails:
            raise ConnectionError("down")
        return _Resp(self._body)


def _ml(monkeypatch, body=None, fails=False):
    monkeypatch.setattr(health.httpx, "AsyncClient", lambda **kw: _MlClient(body, fails))


async def test_a_mismatched_collection_is_named_with_the_command_that_fixes_it(monkeypatch):
    monkeypatch.setattr(qdrant_store, "identity_status", _status("mismatch", "aaaaaaaa", "bbbbbbbb"))
    _ml(monkeypatch, {"models": {}})

    warnings = await health.readiness_warnings()

    assert any("mismatch" in w and "docs" in w and "make reembed COLLECTION_NAME=docs" in w and "bbbbbbbb" in w for w in warnings)


async def test_an_adopted_legacy_collection_is_reported_as_unverified(monkeypatch):
    monkeypatch.setattr(qdrant_store, "identity_status", _status("adopted"))
    _ml(monkeypatch, {"models": {}})

    assert any("adopted (unverified)" in w for w in await health.readiness_warnings())


async def test_verified_and_absent_collections_say_nothing(monkeypatch):
    _ml(monkeypatch, {"models": {}})
    for state in ("verified", "absent", "unverified"):
        monkeypatch.setattr(qdrant_store, "identity_status", _status(state))
        assert await health.readiness_warnings() == []


async def test_an_ml_service_model_that_is_down_is_a_warning_with_its_reason(monkeypatch):
    monkeypatch.setattr(qdrant_store, "identity_status", _status("verified"))
    _ml(monkeypatch, {"models": {"rerank": {"state": "ready"}, "prompt-guard": {"state": "failed", "reason": "OSError: no such file"}}})

    warnings = await health.readiness_warnings()

    assert warnings == ["ML service model prompt-guard is failed: OSError: no such file"]


async def test_a_refused_label_set_is_a_warning_and_a_pending_or_loaded_one_is_not(monkeypatch):
    monkeypatch.setattr(qdrant_store, "identity_status", _status("verified"))
    _ml(monkeypatch, {"models": {}, "labelsets": {
        "route-topics": {"version": "abc", "labels": ["a", "b"], "models": {"bge": "loaded", "minilm": "refused: no calibration for minilm"}},
        "broken": {"state": "refused: malformed (JSONDecodeError reading broken.json)"},
        "fresh": {"version": "def", "labels": ["a", "b"], "models": {"bge": "pending: not indexed yet", "minilm": "pending: model not ready"}},
    }})

    warnings = await health.readiness_warnings()

    assert warnings == [
        "ML service label set route-topics on minilm is refused: no calibration for minilm",
        "ML service label set broken is refused: malformed (JSONDecodeError reading broken.json)",
    ]


async def test_an_odd_labelsets_payload_is_ignored_and_does_not_hide_a_valid_warning(monkeypatch):
    monkeypatch.setattr(qdrant_store, "identity_status", _status("verified"))
    for odd in (None, [], "x", 3):
        _ml(monkeypatch, {"models": {"rerank": {"state": "failed", "reason": "boom"}}, "labelsets": odd})
        assert await health.readiness_warnings() == ["ML service model rerank is failed: boom"]
    _ml(monkeypatch, {"models": {}, "labelsets": {"a": "refused", "b": None, "c": {"models": None}, "d": {"models": "x"}, "e": {"state": "refused: bad"}}})
    assert await health.readiness_warnings() == ["ML service label set e is refused: bad"]  # the malformed entries are skipped, the valid one still counts


async def test_a_source_that_cannot_be_read_contributes_nothing_and_never_raises(monkeypatch):
    async def boom(_c=None):
        raise ConnectionError("qdrant unreachable")

    monkeypatch.setattr(qdrant_store, "identity_status", boom)
    _ml(monkeypatch, fails=True)

    assert await health.readiness_warnings() == []  # the dependency checks already report an unreachable Qdrant / ML service


async def test_a_hung_source_is_bounded(monkeypatch):
    async def hang(_c=None):
        await asyncio.sleep(30)

    monkeypatch.setattr(qdrant_store, "identity_status", hang)
    monkeypatch.setattr(health, "_WARNINGS_TIMEOUT_SECONDS", 0.05)
    _ml(monkeypatch, {"models": {}})

    assert await asyncio.wait_for(health.readiness_warnings(), timeout=2.0) == []


async def test_warnings_never_change_the_status_or_the_http_code(monkeypatch):
    async def healthy():
        return {"qdrant": True, "appdata_postgres": True, "checkpointer_postgres": True, "redis": True, "ml_service": True}

    async def warnings():
        return ["embedding identity mismatch: docs holds b, this process is configured for a; retrieval from it is refused."]

    monkeypatch.setattr(health, "check_dependencies", healthy)
    monkeypatch.setattr(health, "readiness_warnings", warnings)
    response = Response()

    body = await system.health_ready(response)

    assert response.status_code == 200 and body.status == "ready"
    assert body.warnings and "mismatch" in body.warnings[0]


async def test_an_unreachable_dependency_still_makes_the_api_unready_whatever_the_warnings_say(monkeypatch):
    async def down():
        return {"qdrant": False, "appdata_postgres": True, "checkpointer_postgres": True, "redis": True, "ml_service": True}

    async def none():
        return []

    monkeypatch.setattr(health, "check_dependencies", down)
    monkeypatch.setattr(health, "readiness_warnings", none)
    response = Response()

    body = await system.health_ready(response)

    assert response.status_code == 503 and body.status == "degraded" and body.warnings == []

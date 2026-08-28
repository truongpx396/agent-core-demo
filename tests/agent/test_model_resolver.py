"""Tests for app/agent/model_resolver.py — mocks httpx.get so these stay
hermetic (no live LiteLLM), matching the rest of the suite."""
from app.agent import model_resolver


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_data


def _reset_cache():
    model_resolver._cache.clear()


class TestResolveModel:
    def test_resolves_a_known_alias(self, monkeypatch):
        _reset_cache()
        monkeypatch.setattr(
            model_resolver.httpx,
            "get",
            lambda *a, **kw: _FakeResponse(
                {"data": [{"model_name": "chat", "litellm_params": {"model": "ollama_chat/qwen2.5:3b"}}]}
            ),
        )
        assert model_resolver.resolve_model("chat") == "ollama_chat/qwen2.5:3b"

    def test_unknown_alias_returns_none(self, monkeypatch):
        _reset_cache()
        monkeypatch.setattr(
            model_resolver.httpx,
            "get",
            lambda *a, **kw: _FakeResponse({"data": []}),
        )
        assert model_resolver.resolve_model("nonexistent") is None

    def test_caches_after_first_successful_lookup(self, monkeypatch):
        _reset_cache()
        calls = []

        def fake_get(*a, **kw):
            calls.append(1)
            return _FakeResponse(
                {"data": [{"model_name": "chat", "litellm_params": {"model": "ollama_chat/qwen2.5:3b"}}]}
            )

        monkeypatch.setattr(model_resolver.httpx, "get", fake_get)

        model_resolver.resolve_model("chat")
        model_resolver.resolve_model("chat")
        model_resolver.resolve_model("chat")

        assert len(calls) == 1

    def test_degrades_to_none_on_connection_failure(self, monkeypatch):
        _reset_cache()

        def raise_error(*a, **kw):
            raise ConnectionError("LiteLLM unreachable")

        monkeypatch.setattr(model_resolver.httpx, "get", raise_error)

        assert model_resolver.resolve_model("chat") is None

    def test_degrades_to_none_on_http_error_status(self, monkeypatch):
        _reset_cache()
        monkeypatch.setattr(
            model_resolver.httpx, "get", lambda *a, **kw: _FakeResponse({}, status_code=500)
        )
        assert model_resolver.resolve_model("chat") is None

    def test_admin_base_url_strips_the_v1_suffix(self, monkeypatch):
        monkeypatch.setattr(model_resolver, "OPENAI_API_BASE", "http://localhost:4000/v1")
        assert model_resolver._admin_base_url() == "http://localhost:4000"

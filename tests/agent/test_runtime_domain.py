"""Tests for app/agent/runtime.py's `_resolve_domain_name`/`_domain_name` —
GRAPH_PATTERNS.md pattern 49's per-process domain tag, used only to stamp
app/agent/sessions.py's `chat_sessions.domain` column via `_upsert_session`.
Hermetic: never touches init_graph_async/init_graph_sync's own real
checkpointer setup, since `_resolve_domain_name` is a pure function of its
`manifest` argument and `_upsert_session` only needs `app.agent.sessions`
mocked out, the same "test the function, not the graph" scope
tests/agent/test_multimodal.py and friends already keep for this module.
"""
from app.agent import runtime
from app.agent.manifest import AgentManifest
from tests.conftest import TEST_CTX


class TestResolveDomainName:
    def test_defaults_to_acme_when_no_manifest_is_given(self):
        assert runtime._resolve_domain_name(None) == "acme"

    def test_uses_the_given_manifests_own_name(self):
        manifest = AgentManifest(name="support", system_prompt="irrelevant here")
        assert runtime._resolve_domain_name(manifest) == "support"


class TestUpsertSessionStampsTheCurrentDomain:
    def test_passes_the_process_wide_domain_name_through(self, monkeypatch):
        captured = {}

        def fake_upsert_session(ctx, thread_id, title, domain):
            captured.update(ctx=ctx, thread_id=thread_id, title=title, domain=domain)

        # _upsert_session does `from app.agent import sessions` lazily inside
        # its own body — patching the already-imported module object (not a
        # `runtime.sessions` name, which doesn't exist at module level here)
        # is what that lazy import actually re-reads.
        import app.agent.sessions as sessions_module

        monkeypatch.setattr(sessions_module, "upsert_session", fake_upsert_session)
        monkeypatch.setattr(runtime, "_domain_name", "ops")

        runtime._upsert_session(TEST_CTX, "t1", "did anything break overnight?")

        assert captured == {
            "ctx": TEST_CTX,
            "thread_id": "t1",
            "title": "did anything break overnight?",
            "domain": "ops",
        }

    def test_defaults_to_acme_when_no_process_ever_set_a_different_domain(self, monkeypatch):
        captured = {}
        import app.agent.sessions as sessions_module

        monkeypatch.setattr(
            sessions_module, "upsert_session", lambda ctx, thread_id, title, domain: captured.update(domain=domain)
        )
        monkeypatch.setattr(runtime, "_domain_name", "acme")

        runtime._upsert_session(TEST_CTX, "t1", "hello")

        assert captured["domain"] == "acme"

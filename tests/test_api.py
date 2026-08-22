"""Tests for app/api.py's plain, dependency-free route handlers.

Deliberately NOT using FastAPI's TestClient here: exercising the app
through it would trigger the real `lifespan` (a real durable checkpointer
file, a real Qdrant/embedding call the first time a turn runs) — this
suite calls the handler functions directly instead, the same "test the
function, not the framework wiring" approach the rest of this codebase
already takes for graph nodes (see tests/test_nodes.py's module docstring).
"""
from app.api import ui


class TestUi:
    def test_returns_html_referencing_the_documented_sse_vocabulary(self):
        """The page must talk to the one published endpoint/event
        vocabulary (POST /chat/stream) — never a special-cased endpoint
        of its own (see ui()'s own docstring)."""
        html = ui()
        assert "<title>" in html
        assert "/chat/stream" in html
        for event_type in ("token", "tool_start", "tool_end", "citations", "approval_required", "error"):
            assert event_type in html

    def test_sends_the_trusted_identity_headers(self):
        """The UI must send X-Tenant-Id/X-Principal-Id itself — POST
        /chat/stream fails closed (422) without both (see app/api.py's
        get_ctx)."""
        html = ui()
        assert "X-Tenant-Id" in html
        assert "X-Principal-Id" in html

"""Tests for app/scrubbing.py — pure string transformation, no I/O
except reading app.config's current values (monkeypatched per test)."""
from app import scrubbing


class TestStaticPatterns:
    def test_openai_style_key_is_redacted(self):
        result = scrubbing.scrub("here is your key: sk-abcdefghijklmnopqrstuvwx")
        assert "sk-abcdefghijklmnopqrstuvwx" not in result
        assert "[REDACTED]" in result

    def test_aws_access_key_is_redacted(self):
        result = scrubbing.scrub("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_password_equals_pair_is_redacted(self):
        result = scrubbing.scrub("connection failed, password=hunter2secret")
        assert "hunter2secret" not in result

    def test_url_embedded_password_is_redacted(self):
        result = scrubbing.scrub("db at postgresql://admin:sup3rsecret@db.internal:5432/appdata")
        assert "sup3rsecret" not in result
        assert "db.internal" in result  # host is not scrubbed, only the password

    def test_jwt_shaped_string_is_redacted(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = scrubbing.scrub(f"token: {jwt}")
        assert jwt not in result

    def test_ordinary_text_is_unchanged(self):
        text = "Priya Nair — Staff Engineer, Engineering (hired 2021-03-01)"
        assert scrubbing.scrub(text) == text

    def test_blank_text_passes_through(self):
        assert scrubbing.scrub("") == ""


class TestBoundSecrets:
    def test_the_deployments_own_openai_api_key_is_redacted_even_with_no_pattern_match(
        self, monkeypatch
    ):
        """A structured tool result (e.g. a database row) can echo this
        deployment's real secret verbatim without looking like any
        generic credential pattern — the bound-secret layer catches that
        exact-match case the pattern layer can't."""
        import app.config as config

        monkeypatch.setattr(config, "OPENAI_API_KEY", "my-actual-configured-key-xyz")

        result = scrubbing.scrub("row value: my-actual-configured-key-xyz")

        assert "my-actual-configured-key-xyz" not in result
        assert "[REDACTED]" in result

    def test_unset_secrets_are_never_scrubbed_as_empty_string(self, monkeypatch):
        """An unset secret is "" in this app's Settings defaults —
        scrubbing "" would corrupt every tool result by inserting
        [REDACTED] between every character. Regression guard."""
        import app.config as config

        monkeypatch.setattr(config, "OPENAI_API_KEY", "")
        monkeypatch.setattr(config, "LANGFUSE_SECRET_KEY", "")
        monkeypatch.setattr(config, "LANGFUSE_PUBLIC_KEY", "")

        text = "Priya Nair — Staff Engineer, Engineering"
        assert scrubbing.scrub(text) == text

    def test_appdata_database_url_password_is_scrubbed(self, monkeypatch):
        import app.config as config

        monkeypatch.setattr(
            config, "APPDATA_DATABASE_URL", "postgresql://langfuse:realpassword@localhost:5432/appdata"
        )

        result = scrubbing.scrub("echoing back: realpassword")

        assert "realpassword" not in result


class TestNeverRaises:
    def test_scrub_never_raises_on_unusual_input(self):
        assert scrubbing.scrub("word " * 5000)  # long text, must not raise
        assert scrubbing.scrub("こんにちは、これはテストです") is not None

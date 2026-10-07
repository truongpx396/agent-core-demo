"""`.env.example` is the one place an operator learns what can be configured.

CLAUDE.md's rule is that a limit or flag is tuned through `Settings` AND listed in
`.env.example`. It had drifted badly: 44 of 61 settings had no entry (timeouts,
worker concurrency, the per-tenant budget, upload caps, the sandbox image, the
crawl4ai URL, ...), and one entry — `CHECKPOINT_DB_PATH`, described as the SQLite
file that holds paused human approvals — named something nothing reads any more,
because the checkpointer moved to Postgres. An operator following it would set a
variable with no effect and believe approvals persisted somewhere they do not.

Two checks, both cheap and hermetic:
  * every `Settings` field appears in `.env.example` (an entry may be commented out
    — a commented line is how an optional tunable shows its default);
  * every other name in `.env.example` is actually read somewhere in the repo's
    code or config, so a removed setting cannot linger there.
"""
import re
import subprocess
from pathlib import Path

from app.core.config import Settings

REPO = Path(__file__).resolve().parents[2]
_ENTRY = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]+)\s*=", re.MULTILINE)


def _env_example_names() -> set[str]:
    return set(_ENTRY.findall((REPO / ".env.example").read_text()))


def _settings_names() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_every_setting_is_listed_in_env_example():
    missing = sorted(_settings_names() - _env_example_names())

    assert not missing, (
        f"{len(missing)} settings have no entry in .env.example (a commented-out line is fine): "
        + ", ".join(missing)
    )


def _tracked_code_and_config() -> list[Path]:
    """Files whose reading of an environment variable counts: code and config,
    not docs (a README mentioning a name would keep a dead entry alive) and not
    `.env*` themselves."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    suffixes = (".py", ".yml", ".yaml", ".toml", ".sh", ".json")
    return [
        REPO / name
        for name in tracked
        if (name.endswith(suffixes) or name == "Makefile")
        and not name.startswith(("specs/", ".env"))
        and (REPO / name).is_file()
    ]


def test_every_entry_that_is_not_a_setting_is_read_somewhere():
    extra = _env_example_names() - _settings_names()
    corpus = "\n".join(path.read_text(errors="ignore") for path in _tracked_code_and_config())

    unused = sorted(name for name in extra if name not in corpus)

    assert not unused, (
        "entries in .env.example that nothing in the repo reads (stale — remove them): " + ", ".join(unused)
    )


_VALUE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]+)=(\S*)", re.MULTILINE)


def _same(example_value: str, default) -> bool:
    if default is None:
        # A setting with NO default (CREDITS_PER_USD: a price is the operator's decision) is listed empty.
        return example_value == ""
    if isinstance(default, bool):
        return example_value.lower() == str(default).lower()
    if isinstance(default, (int, float)):
        try:
            return float(example_value) == float(default)
        except ValueError:
            return False
    return example_value == str(default)


def test_a_listed_default_is_the_settings_real_default():
    """A commented line documents the value used when it stays commented, so it
    must be the real one; a drifted default is worse than none. Active entries
    that deliberately differ from the default (keys an operator must fill in) are
    left empty here and so compare equal."""
    # `get_default` also resolves a `default_factory` (BILLING_WEBHOOK_SECRETS: an empty dict), which `.default` does not.
    defaults = {name.upper(): field.get_default(call_default_factory=True) for name, field in Settings.model_fields.items()}
    mismatched = []
    for name, value in _VALUE.findall((REPO / ".env.example").read_text()):
        if name in defaults and not _same(value, defaults[name]):
            mismatched.append(f"{name}: .env.example says {value!r}, Settings default is {defaults[name]!r}")

    assert not mismatched, "\n".join(mismatched)

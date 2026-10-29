"""The schema the package ships (spec 011 D20, D22; spec 012).

The wallet and the webhook inbox are two plain SQL scripts INSIDE the package (`credit_billing/sql/`), so
installing it carries its schema. They are idempotent (`CREATE ... IF NOT EXISTS`), keep their numbers (`20-`,
`22-`) so a host with other numbered scripts can slot them in order, and open with `\\connect appdata`, a
`psql` meta-command that is right as an init script and must be removed by anything applying the text another
way. `executable()` removes exactly `\\connect <database>` and refuses any other line starting with a
backslash, because dropping a meta-command it doesn't understand (`\\set`, `\\if`) would silently change what
the script does.

The agent app keeps byte-equal COPIES under `postgres-init/` because docker-compose, the deploy rsync and its
test containers read that fixed path; tests/core/test_billing_package_copies.py fails if either drifts.

Applying them to an existing database, recording what ran and reporting what is pending is
`credit_billing.migrate` (spec 012). This module only says what is shipped, in what order, and how to read it.
"""
import re
from importlib import resources

_SQL = resources.files("credit_billing") / "sql"
# `<number>-<words>.sql`. The number is the order to apply them in and is compared as an integer: sorted as text, `100-` would come before `22-`.
_NAME = re.compile(r"^(\d+)-[A-Za-z0-9._-]+\.sql$")
_CONNECT = re.compile(r"^\\connect\s+\S+$")


def _order(name: str) -> tuple[int, str]:
    match = _NAME.match(name)
    if match is None:
        raise ValueError(f"{name!r} is not a shipped script name: expected '<number>-<words>.sql', for example '20-credit-wallet.sql'")
    return int(match.group(1)), name


def scripts() -> list[str]:
    """The shipped script names in the order to apply them."""
    return sorted((entry.name for entry in _SQL.iterdir() if entry.name.endswith(".sql")), key=_order)


def read(name: str) -> str:
    """The text of one shipped script, exactly as shipped (including its `\\connect` line)."""
    return (_SQL / name).read_text(encoding="utf-8")


def read_bytes(name: str) -> bytes:
    """The bytes of one shipped script. What the checksum is taken over, so it equals `sha256sum` of the file and of the host's copy."""
    return (_SQL / name).read_bytes()


def executable(name: str) -> str:
    """The script as plain SQL that a database driver can run: `read(name)` without its `\\connect` line.

    Raises ValueError, naming the script and the line, for any other line whose first non-space character is a backslash."""
    kept: list[str] = []
    for number, line in enumerate(read(name).splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("\\"):
            if _CONNECT.match(stripped):
                continue
            raise ValueError(
                f"{name}, line {number}: {stripped[:40]!r} is a psql meta-command that cannot be applied through a database driver; "
                "only '\\connect <database>' is removed"
            )
        kept.append(line)
    return "\n".join(kept) + "\n"

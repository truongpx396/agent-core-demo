"""The schema the package ships (spec 011 D20, D22; spec 012).

The wallet and the webhook inbox are two plain SQL scripts that live INSIDE the package (`credit_billing/sql/`), so installing the
package carries its schema with it. They are written to be idempotent (`CREATE ... IF NOT EXISTS`), keep their numbers (`20-`, `22-`)
so a host that also has other numbered scripts can slot them in order, and open with `\\connect appdata`, which is a `psql` meta-command:
it is correct as an init script and must be removed by anything that applies the text some other way. `executable()` is that removal,
and it is deliberately strict: it removes exactly `\\connect <database>` and refuses any other line that starts with a backslash,
because a meta-command it does not understand (`\\set`, `\\if`) dropped silently would change what the script does.

The agent app keeps byte-equal COPIES under `postgres-init/` because docker-compose, the deploy rsync and its test containers read
that fixed path; a test fails if either copy drifts (tests/core/test_billing_package_copies.py in the repo).

Applying them to a database that already exists, recording what was applied, and reporting what is pending is `credit_billing.migrate`
(the `credit-billing-migrate` command, spec 012). This module only says what is shipped, in what order, and how to read it.
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

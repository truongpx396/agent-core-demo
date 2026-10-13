"""The schema the package ships (spec 011 D20, D22).

The wallet and the webhook inbox are two plain SQL scripts that live INSIDE the package (`credit_billing/sql/`), so installing the
package carries its schema with it: another project applies them to its own database with whatever it migrates with. They are
written to be idempotent (`CREATE ... IF NOT EXISTS`), keep their numbers (`20-`, `22-`) so a host that also has other numbered
scripts can slot them in order, and open with `\\connect appdata`, which is a `psql` meta-command: it is correct as an init script
and must be removed by anything that applies the text some other way (`tests/containers.py` shows one way).

The agent app keeps byte-equal COPIES under `postgres-init/` because docker-compose, the deploy rsync and its test containers read
that fixed path; a test fails if either copy drifts (tests/core/test_billing_package_copies.py in the repo).

There is no migration tool: scripts run on a fresh database, or by hand once against an existing one, as each script's header says.
That is the first prerequisite for running this as a service (specs/011-billing-module-boundary/design-note.md, section 6).
"""
from importlib import resources

_SQL = resources.files("credit_billing") / "sql"


def scripts() -> list[str]:
    """The shipped script names in the order to apply them."""
    return sorted(entry.name for entry in _SQL.iterdir() if entry.name.endswith(".sql"))


def read(name: str) -> str:
    """The text of one shipped script."""
    return (_SQL / name).read_text(encoding="utf-8")

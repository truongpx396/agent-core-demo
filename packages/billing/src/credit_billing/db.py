"""Where the billing core gets a database connection (specs/011-billing-module-boundary, contracts/billing-core-boundary.md).

The core used to `from app.agent.sql_store import get_connection` (now `app.core.sql_store`), which tied it to the agent's pool: copy
`packages/billing/src/credit_billing/` into another project and it failed to import. It now asks for a connection through this module, and
the host (or a future service, or a test) tells it where connections come from with `configure(...)`. The core
never opens a pool, never holds a connection string and never falls back to a default.

**The provider owns the transaction.** A provider must behave like `app.core.sql_store.get_connection`: one
`async with` is one transaction, committed on a clean exit and rolled back when the block raises. That is a real
invariant, not a convention: every single-statement writer in the core relies on the commit, and the webhook
handler gets "applied and marked applied" atomically from one block. `get_connection()` below delegates with
`async with provider() as conn: yield conn`, so an exception raised in the caller's block is thrown into the
provider's own `async with` and reaches its rollback (tests/billing/test_billing_core_boundary.py proves it, and
proves a clean exit does not look like one).

**Unset means refused** (constitution I: unknown resolves to refusal). A process that reached the core without
being wired gets `BillingNotConfigured`, never a connection to a database it was not told about. In the agent
app the wiring is `app/billing_integration/__init__.py`, which every host module that imports `credit_billing` also
imports (a scan enforces that), so no entry point can reach the core unwired. A caller that swallows the error
degrades through the same counted path as any other wallet fault (`_charge` into `credit_debit`, the gate into
`credit_read`), so an unwired process is loud in the alerts and not silent.

**The name is the contract with the tests.** Core modules do `from credit_billing.db import get_connection` and keep
that module-level name, so `monkeypatch.setattr(credits, "get_connection", fake)` (the hermetic suite's guard and
the real-Postgres tests) works as it always did.

Rules the `*_in(conn, ...)` functions already follow and must keep: they never commit and never begin an outermost
transaction on a connection they were handed (they use savepoints), and the core never checks out a second
connection while it holds one (the pool is finite, and two checkouts in one task is how a pool deadlocks itself).

Imports nothing from `app` (enforced by tests/billing/test_billing_core_boundary.py).
"""
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from psycopg import AsyncConnection

ConnectionProvider = Callable[[], AbstractAsyncContextManager[AsyncConnection]]


class BillingNotConfigured(RuntimeError):
    """The billing core was used before anything told it where database connections come from."""


_provider: ConnectionProvider | None = None


def configure(*, connection: ConnectionProvider | None) -> None:
    """Sets (or, with None, clears) the provider. Idempotent; storing a function opens nothing, so it is safe at import."""
    global _provider
    _provider = connection


@asynccontextmanager
async def get_connection() -> AsyncIterator[AsyncConnection]:
    """A connection from the configured provider, in the provider's own transaction (see the module docstring)."""
    provider = _provider
    if provider is None:
        raise BillingNotConfigured(
            "the billing core has no database connection provider: import app.billing_integration (the agent app's wiring), "
            "or call credit_billing.db.configure(connection=...) before using it"
        )
    async with provider() as conn:
        yield conn

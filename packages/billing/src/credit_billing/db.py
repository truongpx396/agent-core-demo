"""Where the billing core gets a database connection (specs/011-billing-module-boundary,
contracts/billing-core-boundary.md).

The core used to import the agent's pool (`app.core.sql_store.get_connection`), so copying `credit_billing/`
into another project failed to import. Now the host (or a service, or a test) says where connections come from
with `configure(...)`. The core never opens a pool, holds a connection string or falls back to a default.

**The provider owns the transaction.** It must behave like `app.core.sql_store.get_connection`: one `async
with` is one transaction, committed on a clean exit and rolled back when the block raises. This is an
invariant: every single-statement writer relies on the commit, and the webhook handler gets "applied and
marked applied" atomically from one block. `get_connection()` delegates with `async with provider() as conn:
yield conn`, so an exception in the caller's block reaches the provider's rollback
(tests/billing/test_billing_core_boundary.py proves it, and that a clean exit doesn't look like one).

**Unset means refused** (constitution I). A process reaching the core unwired gets `BillingNotConfigured`,
never a connection to a database it wasn't told about. In the app the wiring is
`app/billing_integration/__init__.py`, which every host module importing `credit_billing` also imports (a scan
enforces it). A caller that swallows the error degrades through the same counted path as any wallet fault
(`_charge` into `credit_debit`, the gate into `credit_read`), so an unwired process is loud in the alerts.

**The name is the contract with the tests.** Core modules do `from credit_billing.db import get_connection`,
so `monkeypatch.setattr(credits, "get_connection", fake)` works as before.

Rules the `*_in(conn, ...)` functions keep: they never commit and never begin an outermost transaction on a
connection they were handed (they use savepoints), and the core never checks out a second connection while
holding one (the pool is finite; two checkouts in one task is how a pool deadlocks itself).

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

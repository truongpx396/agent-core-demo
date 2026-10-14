# Contract: the billing core's boundary (PR A)

> **Living copy** (spec 011 D13). This is the version to read and edit. The original, `specs/011-billing-module-boundary/contracts/billing-core-boundary.md`, is the historical record of the decision it was written for and is not updated. A path in the text is relative to `packages/billing/` unless it names the agent application (`app/...`), which hosts this package in this repository.

## The rule

**The core imports only itself and third-party libraries.** Concretely: no import statement anywhere in `src/credit_billing/**` (top-level, nested in a function, `import x` or `from x import y`) names `app` or `app.<anything>` unless it is `credit_billing` or `credit_billing.<anything>`. Relative imports inside the core are fine.

Enforced by `tests/billing/test_billing_core_boundary.py` (an AST walk over every import node, plus a subprocess that imports every core module and fails if any other `app.*` module was loaded). It reports the file, line and import. It fails on an empty scan list, and its own tests prove it recognises each forbidden form.

## The three seams the core is given instead

### 1. Settings: `credit_billing.settings`
`BillingSettings` (pydantic-settings; `env_file=".env"`, `extra="ignore"`, `hide_input_in_errors=True`) holds the ten core fields and their validators. The module exposes `settings` and the constants consumers import (`BILLING_PROVIDERS`, `BILLING_WEBHOOK_SECRETS`, `BILLING_WEBHOOK_MAX_BODY_BYTES`, `BILLING_WEBHOOK_RATE_LIMIT_PER_MINUTE`, `BILLING_WEBHOOK_MAX_ATTEMPTS`, `BILLING_REFUND_HOLD_HOURS`, `BILLING_INBOX_RETENTION_DAYS`, `BILLING_INBOX_MIN_RETENTION_DAYS`, `STRIPE_API_KEY`, `POLAR_ACCESS_TOKEN`, `POLAR_ENVIRONMENT`), computed exactly as `app/core/config.py` computed them.
- Env var names, defaults, bounds and error messages are unchanged. A rejected value is never repeated in the error.
- Core modules import the constants by name, so a test can still re-point one with `monkeypatch.setattr(<module>, "NAME", value)`. The two adapters read theirs at construction, not at import.
- The host's `Settings` inherits `BillingSettings` and `app/core/config.py` re-exports the constants, so no host importer changes.

### 2. Connection: `credit_billing.db`
```python
ConnectionProvider = Callable[[], AbstractAsyncContextManager[AsyncConnection]]
def configure(*, connection: ConnectionProvider | None) -> None
@asynccontextmanager
async def get_connection() -> AsyncIterator[AsyncConnection]   # raises BillingNotConfigured when unset
```
- The provider must behave like `app.agent.sql_store.get_connection`: **commit on clean exit, roll back on an exception**, one transaction per `async with`. `get_connection()` delegates with `async with provider() as conn: yield conn`, so the caller's exception reaches the provider.
- Unset means **refused**, never a default connection and never a connection string (Principle I: unknown resolves to refusal).
- Core modules keep the module-level name `get_connection`, so `monkeypatch.setattr(credits, "get_connection", fake)` works as it did.
- The `*_in(conn, …)` functions never commit, never begin an outermost transaction on a connection they were given (they use savepoints), and the core never checks out a second connection while holding one.

### 3. Metrics: `credit_billing.metrics`
Four counters on the OpenTelemetry **API** only (`opentelemetry.metrics.get_meter`): the same names, labels and descriptions as before. The SDK, exporter, endpoint and `service.name` stay the process's concern, and instruments created before the SDK is installed are replayed once it is, so import order does not matter. The `Counter` wrapper keeps `.labels(**).inc()` and `.name`. The meter scope string is unchanged (research P6).

## The host's side: `app.billing_integration`
Importing the package calls `credit_billing.db.configure(connection=app.agent.sql_store.get_connection)`. It stores a function and opens nothing, so it is safe at import and idempotent. **Every host module that imports `credit_billing` also imports `app.billing_integration`** (enforced by a scan), so no entry point (API, worker, CLI, cron script) can reach the core unwired. It holds `reconcile` and `export`, which read the agent's usage events and the LiteLLM gateway and so are not part of the reusable core.

## What does not change
Every SQL statement, idempotency key, savepoint, metric name and label, env var, route, `make` target and alert name; the rule that no module serving an agent tool may import the wallet (the T013 test, now also covering `app.billing_integration`).

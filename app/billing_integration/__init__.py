"""The agent app's side of the billing boundary (specs/011-billing-module-boundary,
contracts/billing-core-boundary.md; specs/013).

`packages/billing/src/credit_billing/` is a module another project could reuse: it imports nothing from the
agent app and is handed what it needs. This package is where the agent app hands it those things. Since spec
013 that is all it does (the package owns the billing half of the meter: rating, the wallet debit, the export
outbox and worker, the wallet reconciliation, the credit gate), and what remains is

  * `port.py`, the one module host code imports to use billing (`record_usage_in`, `admit`, `credit_summary`,
    `account_balance`), and
  * `http.py`, the package's webhook route as the app mounts it.

**The wiring.** Importing this package tells the core where database connections come from: the agent's single
pool, `app.core.sql_store.get_connection`. Every host module that imports `credit_billing` also imports this
package (tests/billing/test_billing_core_boundary.py scans for a miss), so no entry point (API, queue worker,
chat CLI, cron script) can reach the core unwired. The core refuses to run unwired (`BillingNotConfigured`),
so a miss is a loud failure, not a silent default.

Wiring at import time is fine here although `app/core/telemetry.py` insists on start-up only: that rule exists
because `configure_telemetry` is call-once and installs a network exporter, whereas `configure` here stores a
function reference, opens nothing, connects to nothing and may be called any number of times.
"""
from app.core import sql_store
from credit_billing import db as billing_db

billing_db.configure(connection=sql_store.get_connection)

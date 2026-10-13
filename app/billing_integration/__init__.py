"""The agent app's side of the billing boundary (specs/011-billing-module-boundary, contracts/billing-core-boundary.md).

`app/billing/` is a module another project could reuse: it imports nothing from the agent app and is handed
what it needs. This package is where the agent app hands it those things, and where the code that only makes
sense next to the agent lives:

  * `reconcile.py` compares the agent's usage events with the LiteLLM gateway's spend log and with the wallets,
  * `export.py` drains an outbox built from those events to a provider that bills on usage.

Both read `usage_events` (the agent's LLM-shaped meter, which its dollar caps also read) and the gateway, which
a wallet-and-webhooks module has no business knowing about, so they were moved here from `app/billing/`
(spec D5, D15).

**The wiring.** Importing this package tells the core where database connections come from: the agent's single
pool, `app.agent.sql_store.get_connection`. Every host module that imports `app.billing` also imports this
package (tests/billing/test_billing_core_boundary.py scans for a miss), so no entry point (the API, a queue
worker, the chat CLI, a cron script) can reach the core unwired. The core refuses to run unwired
(`BillingNotConfigured`), so a miss is a loud failure, not a silent default.

Why wiring at import time is fine here when `app/core/telemetry.py` insists on start-up only: that rule exists
because `configure_telemetry` is call-once and installs a network exporter. `configure` below stores a function
reference. It opens nothing, connects to nothing and may be called any number of times, so there is nothing to
race and nothing to leak.

PR B of the same feature replaces the one-line imports of this package in the host with a single port module
that imports it (spec 011 plan.md, "PR B").
"""
from app.agent import sql_store
from app.billing import db as billing_db

billing_db.configure(connection=sql_store.get_connection)

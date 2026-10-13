# Feature Specification: A Self-Contained Billing Module

**Feature Branch**: `011-billing-module-boundary`

**Created**: 2026-10-13

**Status**: Proposed. Nothing in this spec is built yet; `plan.md` fixes the three-PR sequence below.

**Input**: "Properly do the necessary preparation to separate a self-contained payment/billing module, so later it can be extracted or run as a separate payment/billing service serving completely different projects. Do the preparation in 2 PRs, so the 3rd PR is a well standalone, self-contained, independent package in the monorepo."

> **Boundaries.** This builds on feature 010 (the wallet, the webhook inbox, the provider port and its Stripe/Polar adapters, the
> export outbox, reconciliation). It changes **where code lives and which way it may depend**, not what it does: no new feature,
> no new setting, no schema change, no metric rename, no changed behaviour for an operator or a tenant. It does **not** build a
> service, a network client, or a migration tool; it makes each of those a later, bounded decision instead of a rewrite.
>
> **Delivery shape (decided by the maintainer, 2026-10-13).** Three code PRs after this spec's own docs PR:
> **PR A** and **PR B** are the *preparation* (the module stops depending on its host, then the host depends on it only through one
> small port, with both directions enforced by a test); **PR C** is the *extraction* (a mechanical move into `packages/billing/`
> that is an independent package: its own dependencies, schema, tests, scripts, alert rules, dashboard and docs). Because A and B
> do all the thinking, C should be a move plus packaging, reviewable as renames. **The maintainer has waived the repo's ~400-line
> PR budget for this feature** (2026-10-13: PRs may run to thousands of lines); FR-020 says what replaces it.
>
> **Non-goals.** A running billing service or HTTP/queue client for the port; a migration framework; renaming `tenant`, tables,
> columns or metrics; changing how credits are priced; PayPal; publishing to a package index; a separate git repository.

## Why this is shaped the way it is

Read from the code on 2026-10-13, before writing this spec:

1. **The module is already almost a unit.** `app/billing/` is 2,698 lines in 13 files. Only four files outside it import it
   (`app/agent/usage_events.py`, `app/agent/budgets.py`, `app/api/routers/usage.py`, `app/api/routers/billing.py`), and the model has
   no path to it (a structural test already forbids any tool module from importing it). Moving it today would be easy and would
   achieve nothing, because of the next point.
2. **It still reaches back into its host.** Seven of its 13 files (`credits`, `inbox`, `webhooks`, `export`, `reconcile` and the
   Stripe and Polar adapters) import `app.core.config` (module-level constants), `app.core.metrics` (shared counters),
   `app.agent.sql_store.get_connection`, or, once, `app.agent.gateway`. The two real adapters do it lazily, inside their
   constructors, which is easy to miss in a scan of top-level imports. Removing `export` and `reconcile` (point 3) leaves five. A folder that imports its host is not a package: copy it into another project and it
   fails to import.
3. **Two of its files are not billing.** `reconcile.py` (467 lines) compares the agent's usage events with the LiteLLM gateway's
   spend log, and `export.py` (261 lines) drains an outbox built from those events. Both are *consumers of the agent's meter*. A
   different project that wants a wallet and a Stripe webhook does not want either. They belong on the host side of the line.
4. **The one hard seam is a transaction.** `usage_events._charge` calls `credits.debit_in(conn, …)` on the *same connection* as the
   usage-event insert, inside a savepoint. The charge is already idempotent on the event id, never raises, and is repaired by
   reconciliation, so a later service can replace the in-transaction call with an at-least-once consume. But that is a change in
   consistency (the balance would lag the meter) and has to be a named, documented decision, not a surprise.
5. **There is no migration tool.** `postgres-init/NN-*.sql` runs only on a fresh volume. A package that owns its schema and runs
   somewhere else needs more than that. This spec records the gap; it does not close it.

The order follows from these: **decouple in place first** (so the move is just a move), **narrow the host's access to one port**
(so the seam is explicit and replaceable), **then relocate** (so independence can be *proven*, not asserted).

## User Scenarios & Testing *(mandatory)*

The "users" here are the people who maintain the repo and the operators who run it. Tenants and their end users must notice nothing.

### User Story 1 - Billing no longer depends on the agent, and a test keeps it that way (Priority: P1) — PR A

A maintainer can read, test and change the billing core without loading the agent, the API, the job queue or the host's central
settings. Reconciliation and export, which read the agent's meter, are plainly host-side. A test fails the build if the core ever
imports the host again.

**Why this priority**: every later step is only as good as this one. Without it, relocation moves a folder that cannot import.

**Independent Test**: add one line `from app.agent import gateway` to a core module; the boundary test fails and names the file and
the import. Remove it; the test passes. Then run the full hermetic suite and the integration tier: the same tests pass as before.

**Acceptance Scenarios**:
1. **Given** the core after this PR, **When** every import in it is read, **Then** none targets the host (agent, API, channels, queue, domains, central settings, shared metrics registry or connection pool).
2. **Given** an operator's existing `.env`, **When** the app starts, **Then** every billing setting resolves to the same value and the same validation error as before.
3. **Given** the boundary test, **When** a scan target list is empty or misspelt, **Then** the test fails rather than passing vacuously (the existing T013 test is the model).
4. **Given** reconciliation and export, **When** they run, **Then** they behave as before but live on the host side and reach the core only through what the core exports.

---

### User Story 2 - The host reaches billing through one small, replaceable port (Priority: P1) — PR B

The agent app asks billing four things and nothing else: *debit this tenant for this event, in my transaction*; *how much can this
tenant still spend* (to gate a turn); *what is this tenant's account balance* (for the usage endpoint); and the webhook and operator
entry points the core serves itself. Each has a written contract: its idempotency key, how it is tenant-scoped, what it does when it
cannot be reached, and whether it is atomic with the caller. A test fails if the host imports anything else from billing.

**Why this priority**: this is the seam a future service sits behind. If it is implicit, extracting a service means rediscovering it.

**Independent Test**: in a test, substitute a stub for the in-process port; run the debit and the gate; both go through the stub and
nothing else in the host touches billing. Then run the real implementation and confirm identical SQL effects to before.

**Acceptance Scenarios**:
1. **Given** a usage event and a wallet, **When** the event is written, **Then** the debit commits or rolls back with the event's transaction exactly as before, and a wallet fault still undoes only the debit (the savepoint behaviour is unchanged).
2. **Given** the gate, **When** the wallet cannot be read, **Then** `CREDIT_CHECK_FAILURE_POLICY` decides exactly as before.
3. **Given** the host after this PR, **When** its imports are scanned, **Then** it reaches billing only through the port module.
4. **Given** a tool module, **When** it imports billing in any form, **Then** the existing structural test still fails the build (the model still has no path to a balance).
5. **Given** the contract document, **When** a maintainer reads it, **Then** it states, per operation, what changes if a network sits behind it (see US4).

---

### User Story 3 - Billing is an independent package in the monorepo (Priority: P2) — PR C

All of billing lives under `packages/billing/` as an installable package: its code, schema scripts, tests at every tier it has,
operator scripts, alert rules, dashboard and docs. The host installs it like any dependency. The package can be installed and tested
alone, and CI proves that on every push, so independence cannot silently rot.

**Why this priority**: this is the deliverable the maintainer asked for ("well standalone, self-contained, independent"), but it is
safe only after US1 and US2.

**Independent Test**: in a fresh virtual environment with *only* the package installed (the host's `app/` not on the path), the
package's hermetic tests pass. Build a fresh database from the repo before and after the move; the schemas are identical.

**Acceptance Scenarios**:
1. **Given** a clean environment, **When** only the package and its declared dependencies are installed, **Then** it imports and its hermetic tests pass with `app` absent.
2. **Given** a fresh Postgres volume, **When** the repo's init scripts run, **Then** the resulting schema is identical to the one built before the move, in the same relative order (the host's usage-event columns and export outbox still come after the billing tables they reference).
3. **Given** an existing volume, **When** the new code is deployed, **Then** no database action is required.
4. **Given** the operator's tooling, **When** they run any existing `make` target, use any env var, scrape any metric or load the dashboard, **Then** each works unchanged.
5. **Given** CI, **When** it runs after the move, **Then** every moved test is still selected (collected count unchanged) and a dedicated job installs the package alone and runs its tests.

---

### User Story 4 - The road to a real service is written down, with its triggers (Priority: P3) — in PR B

A future maintainer can read one document and learn when to turn the package into a service, what the port becomes on the wire, what
gets worse when it does, and what must be built first.

**Why this priority**: it carries no behaviour, but without it the preparation is a guess about a future nobody can check.

**Independent Test**: a reviewer, given only that document, can answer: *what would make us extract a service, which port
operation is hardest to move and why, and what is the first missing prerequisite?*

**Acceptance Scenarios**:
1. **Given** the document, **Then** it names the triggers (a second project that must share one wallet and provider account; a wish to isolate provider credentials and the public webhook endpoint from agent workers; independent deploy cadence) and says that without one of them the package stays a library.
2. **Given** the debit, **Then** it states the consistency change a remote wallet implies: an at-least-once consume keyed by event id instead of an in-transaction debit, the balance lagging the meter, overdraft already booked as debt, and what the gate does while the wallet is unreachable.
3. **Given** the schema, **Then** it names the missing migration tool as the first prerequisite for any deployment that is not a fresh volume.
4. **Given** the remaining couplings that this spec deliberately leaves, **Then** each is listed with why (see Decisions D3, D4, D9).

### Edge Cases

- **The hermetic suite patches `credits.get_connection` by name** (`tests/conftest.py:200`) so that no test can reach a real Postgres. Making connection access injectable must keep that guard failing closed, and the existing "the suite's own guard covers the new module" pattern (GRAPH_PATTERNS 58, `test_the_suites_own_guard_covers_this_read`) must be applied to the new seam.
- **Settings are read at construction, not import, on purpose** (`stripe.py`/`polar.py`: "tests re-point the settings"). The new settings object must stay re-pointable by tests, and a secret must still never appear in a `repr` (`SecretStr`).
- **Metrics are OpenTelemetry API instruments created at import time against a global proxy meter**, replayed once the process installs its SDK provider (`app/core/telemetry.py`), so import order between the host and the package does not matter. Moving a counter must not change its name, labels or description, or every dashboard and alert on it silently breaks; the instrumentation-scope (meter) name WILL change, and whether the collector's Prometheus exporter turns it into a label (and so a new series) depends on its version, so it is verified against the real collector, not assumed.
- **A short-lived job exports nothing unless it flushes.** The host's `scheduled_job` configures logging and telemetry and flushes on exit, even on failure (spec 008, B22). The package's own scripts must run without `app`, so they need their own equivalent, with the same flush-on-failure test.
- **Alerts straddle the line.** `BillingWebhookQuarantined` and `BillingWebhookFailing` are emitted by the core; `CreditDebitFailing`, `CreditGateUnenforced`, the export and reconcile alerts are emitted by the host. A rule travels with the side that emits its metric, and Prometheus must still load both files as one set.
- **The webhook route and its rate limit** key on the client address and live in `app/api/routers/billing.py`. Its URL, status codes and counters must not change.
- **Docker image and lock file.** The image installs `requirements-lock.txt`. The package must be in the image (the `docker-build` job), and the lock file is regenerated, never hand-edited.
- **A test that covers both sides** (e.g. `tests/api/test_api.py`, `tests/core/test_billing_settings.py`) is split by what each assertion tests, not moved whole.
- **An import cycle** created while splitting (host imports core imports host) must fail the boundary test, not surface as a runtime `ImportError` in one entry point only.
- **Operator scripts run as `python -m scripts.credits`** and similar; the module path changes must not change any `make` target or documented command.

## Requirements *(mandatory)*

### Functional Requirements

**Dependency direction (PR A)**
- **FR-001**: The billing core MUST NOT import host code: not the agent, API, channels, job queue, domains, the host's central settings module, its shared metrics registry, or its connection pool.
- **FR-002**: The core MUST read its configuration from its own settings object. Environment variable names, defaults, bounds and startup validation MUST be identical to today, so an existing `.env` works unmodified.
- **FR-003**: The core MUST define the metrics it emits. Names, labels and help text MUST be unchanged. A metric the host emits (the gate's refusals, the debit-failure degrade, the reconcile gauges) MUST stay with the host.
- **FR-004**: The core MUST obtain database connections from a provider handed to it by the host, and MUST refuse (fail closed, with a distinct error) when none has been supplied; it MUST NOT open a pool, hold a default connection string, or fall back to one. The host's single pooled connection manager MUST remain the only pool (constitution: appdata access goes through one pooled `get_connection()`). The core MUST NOT commit or begin an outermost transaction on a connection the caller passed in, and MUST NOT check out a second connection while holding one.
- **FR-005**: Reconciliation and export, with their scripts, settings, metrics, alerts and the export-outbox schema script, MUST be host-side adapters that use the core only through what it exports.
- **FR-006**: An automated check MUST fail the build if the core imports host code. It MUST name the offending file and import, MUST fail (not pass) on an empty or misspelt scan list, and MUST prove it recognises each import form it forbids.

**The port (PR B)**
- **FR-007**: The host MUST reach billing only through one port module exposing: a debit that runs on the caller's own connection and transaction; the available balance for gating; the account balance for display; and the entry points the core serves itself (webhook intake, operator grant/adjust/show).
- **FR-008**: The port MUST use plain data types in its signatures (identifiers, decimals, dataclasses), so a network implementation could satisfy the same contract. The in-process form MAY take the caller's connection; that is the one documented exception.
- **FR-009**: Each port operation MUST have a written contract: tenant scoping, idempotency key, failure behaviour, and whether it is atomic with the caller.
- **FR-010**: With the in-process implementation, behaviour MUST be identical to today: same SQL effects, same idempotency keys, same transaction and savepoint semantics, same failure policy.
- **FR-011**: An automated check MUST fail the build if host code imports anything from billing other than the port module. The existing check that no tool module imports billing MUST remain and MUST cover the new location.

**The package (PR C)**
- **FR-012**: The core MUST live under `packages/billing/` as an installable package imported under a name that is not under `app`, with its own dependency declaration listing only what it uses, installed by the host as an editable dependency.
- **FR-013**: The package MUST own, inside its directory: code, the schema scripts for the tables it owns, hermetic, integration and provider-sandbox tests, its operator scripts, its alert rules, its dashboard, and its living docs (the provider-port contract, the data model, the port contract and design note, a README). The historical feature specs (010, 011: spec, plan, tasks, research) stay in the repo's `specs/` (D13).
- **FR-014**: The tables the core owns MUST keep their definitions. A database built from scratch after the move MUST have a schema identical to one built before, with each script in the same position relative to the host's scripts that reference it.
- **FR-015**: Every operator-facing name (make targets, env vars, metric names, alert names, dashboard identity, HTTP routes) MUST keep working unchanged.
- **FR-016**: The package's hermetic tests MUST pass in an environment where the host's `app/` is not importable, and CI MUST run that on every push in a dedicated job.
- **FR-017**: Every test that exists today MUST still be collected and run, and none MAY be weakened or deleted. The tenant-isolation, idempotency and "the model has no path to a balance" tests move with the code.

**Record and process**
- **FR-018**: A design note MUST record the triggers for a standalone service, what each port operation becomes on the wire, the consistency change of a remote wallet, the missing migration tool as the first prerequisite, and the four extensions a general multi-project service needs beyond this preparation, in likely order of need (D16). It is written in PR B beside the port contract and moves into the package with it in PR C.
- **FR-019**: What remains coupled after this feature MUST be disclosed in the package's pattern file and the host's `BILLING_INTEGRATION.md` (stating the failure that motivated the boundary), per constitution VIII as amended by D14. `GRAPH_PATTERNS.md` MUST carry no billing content after PR C, and every old citation of a moved pattern MUST still resolve (D14).
- **FR-020**: Each PR MUST leave `main` green (`make lint`, `make typecheck`, `make test`, plus `make test-integration` when SQL or store code is touched), be revertible on its own, and state its before/after test counts. The ~400-line budget is waived for this feature, so what keeps a multi-thousand-line PR reviewable is this instead: a pure rename or move MUST be its own commit with no edits in it (so `git diff -M` shows it as a rename), edited code MUST be in separate commits, and the PR description MUST list which commits are pure moves.
- **FR-021**: No behaviour change: no new setting, no schema change, no metric rename, no new endpoint. A deviation found to be necessary MUST be recorded in the plan, not slipped in.

### Key Entities

- **Billing core**: the part reusable by another project: the wallet (credit lots, transactions, entries), the provider port with its Stripe, Polar and fake adapters, the webhook route and its decision logic, the inbox, the catalog and customer link, and the operator CLI.
- **Billing port**: the small set of operations through which the host uses the core; the future network boundary.
- **Host adapter**: code that consumes the agent's meter and the gateway (reconciliation, export) and lives on the host side.
- **Boundary check**: the automated test(s) that enforce the dependency direction and the single entry point.
- **Core-owned schema** (scripts 20 and 22: the wallet, the customer link, the catalog, the webhook inbox) and **host-owned schema** (script 19: the `usage_events` meter itself; script 21: credit columns on `usage_events`; script 23: the export outbox).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A scan of the core finds **zero** imports of host code, and adding one makes the build fail with the file and import named.
- **SC-002**: The set of passing tests in the hermetic suite and in the integration tier is **unchanged by every PR**: no test removed, disabled or loosened, and the collected count per tier recorded before and after in each PR.
- **SC-003**: The host's only import of billing is the port module: a scan finds **no other** billing import outside the package, and a stub substituted for the port is enough to exercise the debit and the gate in a test.
- **SC-004**: With only the package installed (host code absent), its hermetic tests pass, and the dedicated CI job is green on every push.
- **SC-005**: A database built from scratch before and after the move has an **identical schema** (shown by a catalog comparison), and an existing volume needs **no action**.
- **SC-006**: Before and after: the list of environment variables, `make` targets, metric names and labels, alert names and HTTP routes are **identical** (shown by a diff in each PR that touches them).
- **SC-007**: Each PR is individually revertible, and in each one the pure-move commits contain no edits (a diff of each against its parent, with rename detection, shows only renames).
- **SC-008**: A reviewer given only the design note can name the triggers for a standalone service, the hardest port operation to move and why, and the first missing prerequisite.

## Assumptions and Decisions

- **D1.** Preparation only. No service, no network client, no migration framework is built here; each is gated on a trigger (US4).
- **D2.** The package and the host keep **one Postgres database and one connection pool**. The in-transaction debit depends on it, and a second pool would be a second place to get transactions wrong. The core takes its connections from the host; it does not open its own.
- **D3.** `tenant` stays the opaque identifier at the boundary and no table or column is renamed. The wallet already keys on a plain text id, so another project can pass its own account id as `tenant`. A generic rename is a later decision that would also change the schema, which this feature forbids.
- **D4.** Metric names keep the `agent_` prefix. A rename breaks dashboards and alerts for no benefit while there is one consumer; it is a stage-3 decision, listed in the design note.
- **D5.** Reconciliation and export are host-side because they read the agent's usage events and the gateway. (Reconciliation is three comparisons; the one that checks events against wallet entries is generic and moves to the service with its own event store, D15. The one against the gateway stays in the agent.) What another project would reuse is the provider port and adapters, the wallet, the webhooks, the inbox, the catalog and the operator CLI.
- **D6.** The order is **decouple in place, then narrow to a port, then move**, because moving files before the reach-back imports are gone yields a folder, not a module.
- **D7.** Reuse by another project means: depend on the package, apply its schema, pass it a connection provider and a settings object. A *service* is a possible later way to share one wallet; it is not the default.
- **D8.** The Dockerfile and `requirements-lock.txt` are regenerated, not hand-edited, when the package is added.
- **D9.** Two couplings are left on purpose and disclosed: the conversion from a model call's dollar cost to credits (`CREDITS_PER_USD`, `MARKUP`, `credits.Pricing`) and the `usage_event_id` column name both come from the agent's meter. Where the conversion lives is a plan-level decision (O1), and neither is a schema or behaviour change.

- **D10. Shared infrastructure is injected, never imported, and each piece follows the host's existing lifecycle.**
  *Database*: the core declares the one thing it needs (an async context manager yielding a connection that commits on clean exit and rolls back on error) and the host supplies `sql_store.get_connection` once at process start, in the same slot as `configure_telemetry`. The hot-path debit already takes the caller's connection and never touches a pool. Unconfigured means refused, which also replaces the hermetic suite's patch-by-name guard with a stronger default (a test must opt in to a fake).
  *Metrics*: the core creates its four counters (`agent_credit_granted_total`, `agent_credit_overdraft_total`, `agent_credit_debited_total`, `agent_billing_webhook_total`) from the OpenTelemetry **API** only (a library depends on `opentelemetry-api`; the SDK, exporter, endpoint and `service.name` stay the process's concern). The core has no gauge or histogram, so the host's bucket Views are not needed by it. The other five billing metrics belong to reconcile and export and stay with the host.
  *Settings, logging*: the core has its own settings object reading the same env names, and logs through the standard `logging` module, so it takes whatever the host configured. It uses no tracing, error-envelope or scrubbing code from the host.
- **D11.** Process bootstrap is not library code. The package ships a small job bootstrap (logging, telemetry, flush on exit) for its own scripts and duplicates roughly the 20 lines of `scheduled_job`; the cost is two copies of a lifecycle rule, accepted because FR-016 requires the package to run with `app` absent. The host's scripts keep using `scheduled_job`.

- **D12. Two units in the repo, not three: no shared-core package.** The host (`app/`) depends on the package (`packages/billing/`); the package depends on nothing in the repo. Everything it used to take from `app/core` is injected (D10) or duplicated in a few lines (D11). `app/core` is agent-specific (central settings, the agent's metric set) and is NOT turned into a shared package. A shared package is justified only when a second extracted module needs the same code, and then it is tiny and holds no agent concept. The port's contract (its interface) lives in the billing package, which the host and any future network client depend on.

- **D13. Living docs move with the package; historical specs stay in `specs/`.** A feature spec is the record of a decision made in this repo. Spec 010 covers both the agent's usage meter (host) and the wallet and webhooks (core), the Spec Kit scripts number and scan the repo-root `specs/` directory, and `GRAPH_PATTERNS.md`, the README and the PR history cite those paths. The documents a user of the package needs (the provider-port contract, the data model, the port contract, the design note) are copied into `packages/billing/docs/` as the living versions, and the specs link to them. A package extracted into its own repository starts its own `specs/` there.

- **D14. `GRAPH_PATTERNS.md` is about the agent core only; the billing patterns move out.** Read on 2026-10-13, of its 66 patterns:
  *Move to the package's pattern file* (the module's own reasoning): 52 (the app-owned wallet), 54 (a webhook is a stranger's claim), 65 (the Stripe adapter), 66 (the Polar adapter).
  *Move to a root-level `BILLING_INTEGRATION.md`*, beside `WORKER_CONCURRENCY.md` (how the host integrates billing, which the package must not describe): 53 (charge on the event, gate before the call), 55 (usage export), 56 and 61 (reconciliation).
  *Stay, rewritten generic* (general lessons that billing taught): 59 (a settings error must not print the secret it rejected: the `hide_input_in_errors` / `SecretStr` rules stay, the Stripe and Polar specifics move) and 60 (a test that re-applies a migration must be the one that gives up: the `lock_timeout` helper stays, the migration-22 narrative moves). These two are an editorial judgment, not a mechanical move.
  *Stay unchanged*: 51, 57, 58, 62, 63, 64 are the agent's own usage meter, spend caps and budget holds. They mention credits in passing; the meter is the agent's, and the caps read it.
  Numbers are never reused or shifted. A moved pattern leaves a single "moved to" line in a short index, so old citations still resolve (13 exist outside the moved entries themselves: 10 between patterns in the file, 2 in `specs/010-…/tasks.md`, 1 in `CLAUDE.md`); the next new pattern is 67. "Extending Further" and "Graph Flow" hold no billing content. The README has the same mixture; trimming it is a separate decision, not made here.
  **Process consequence:** constitution VIII says every new architectural pattern MUST be added to `GRAPH_PATTERNS.md`. It needs a small amendment (a self-contained package records its own patterns in its own file), made through `/speckit-constitution` and a PR, and `CLAUDE.md` (which says "56 numbered patterns", already stale) gets a pointer to the new files. PR C depends on that amendment.

- **D15. `usage_events` is the agent's meter and stays host-owned; billing receives debits, not usage.** Challenged 2026-10-13 ("I thought usage_events would be billing-specific") and checked against the code:
  (1) the dollar caps and `GET /usage` sum it, and `USAGE_EVENTS_ENABLED=false` is refused at startup because every cap would read $0, so the agent cannot run without it, and billing is OFF by default (no `CREDITS_PER_USD`); making it a billing table would make the agent's cost safety depend on a payment package;
  (2) the row is LLM-shaped (`model_alias`, `resolved_model`, input, output and cached tokens, `cost_usd`, per-token prices, `thread_id`) and its id comes from a LangChain message id, so a billing package that owned it would either be LLM-shaped (no reuse by a project that meters API calls or seats) or would turn it into an untyped blob;
  (3) 17 files touch it, 9 of them agent runtime (budgets, budget holds, graph, metering, model resolver, spend, retention and two scripts); only `export` and `reconcile` are billing-flavoured, and those are already host adapters (D5).
  What billing is given is a debit: a tenant, an amount of credits, an idempotency key (the event id), a reason and a pricing snapshot. That is generic, and any other project can call it. The credit columns (script 21), the in-transaction charge and the export outbox are the host's billing-facing adapter, which is the port.
  **The end state (the maintainer's picture, 2026-10-13: the service manages the wallet, usage events, reconciliation and sweeps, and the SaaS talks to it by API) needs one refinement, which revises the first version of this decision.** The turnkey service owns a *generic* usage-event store (id, tenant, meter, quantity, time, dimensions), its own reconciliation of events against wallet entries, and its own retention sweeps, because a turnkey service cannot ask every SaaS to build a meter and an export. The agent keeps its LLM-shaped local meter permanently (the caps need low-latency local reads, and `USAGE_EVENTS_ENABLED` is refused off), and its comparison against the LiteLLM gateway stays in the agent, because the service cannot know the gateway. The agent's export outbox (script 23) then becomes its delivery path to the service's ingest endpoint, keyed by event id. So: two stores, one flow, no shared table. That arrives in 012 and 013, not 011. In 011 it means only that the port's debit already carries the event identity and the pricing snapshot, so it maps one-to-one onto an ingest call later, and that the cost-to-credits rating (O1) is expected to move behind the port in 013 while staying in the host for now.

- **D16. This feature prepares a boundary; it does not make the module a general billing service. A general one would add four things, none built here, none blocked by the port.** The process (decouple in-process, narrow to a port, then extract when a trigger fires) is the usual way to pull a service out of a monolith; the contract is shaped by one consumer. What a second, different project would need, in likely order:
  1. **A deployment model: DECIDED by the maintainer on 2026-10-13, one instance and one database per SaaS** ("one self-contained, high quality billing/payment service serving one SaaS, nearly ready to use with a few minimal configs"). So no project namespace is needed and none is built. Every key stays `tenant` alone (`credit_accounts.tenant` is the primary key; `billing_customers` is unique on `(tenant, provider)`; the catalog is keyed by `(provider, product_ref)`), which is correct for one SaaS, and the "tenant" of the SaaS is that SaaS's own customer or workspace. Sharing one instance between SaaS products would need a namespace (a schema change) and is explicitly not a goal.
  2. **Holds as a wallet primitive.** The gate computes `wallet.available − reserved` where `reserved` is the host's in-flight turn reservation (`budget_holds`, `app/agent/budgets.py:305`). A general service owns reserve, capture and release with a TTL, or every client reimplements the race protection. Adding it is a new table and port operations.
  3. **An eventual, service-shaped debit as the primary contract.** The in-process form passes the caller's connection (FR-008's documented exception). It is correct here because the charge is already idempotent on the event id, savepointed and never raises, so it can become an at-least-once consume later.
  4. **Raw usage ingestion and rating in the service** (D15's "when the opposite call would be right"), with the rate card owned by billing instead of `CREDITS_PER_USD` and `MARKUP` in the host's settings (O1).
  Not billing-design but also needed for a service: a migration tool, authenticated and versioned service-to-service calls, and metric names without the `agent_` prefix (D4). These come from general knowledge of how metering and billing products are usually built, not from a comparison done this session.

- **D17. The destination is a turnkey service for one SaaS; this feature is the first of two or three that get there, and it does not change what it is.** Read on 2026-10-13, what a SaaS could do with the module today: receive a signed webhook and be granted credits (one HTTP route, `POST /billing/webhooks/{provider}`); debit and read a wallet from Python in the same process; grant, adjust and show a wallet from a CLI. Nothing calls `create_checkout`, so a SaaS cannot sell a pack through it; the catalog and the customer link have write functions in `app/billing/store.py` but no route, CLI or config; there is no balance or ledger API, no hold primitive, no service entry point or container, no authenticated caller, and no migration tool. Per the 010 notes, a real PAID Polar order has not been run end to end, partial refunds and disputes are quarantined for lack of a policy, and PayPal does not exist. "A few minimal configs" therefore needs, after this feature, in this order (revised after the research in `research.md`, which found holds, API idempotency semantics and outbound events to be what a service needs before it has an API): **012, the pro-grade wallet contract, still in-process** (holds with reserve, capture and release and a TTL sweep, so the agent's gate stops computing `available − reserved` itself; the service-side generic event store with an idempotent `record_usage` whose debit is atomic with the event; a conflict error when a key is replayed with different parameters; outbound notifications through an outbox), then **013, the service shell** (an HTTP API over that port with service authentication, an entry point and container, health and readiness, a migration tool, and an HTTP client that implements the same port the in-process form does), then **014, turnkey operation** (the catalog and customer link as configuration, checkout and balance and ledger endpoints, a `check`/`track`-style client, a quickstart, a provider-by-provider verified setup, and a check of provider-collected cash against credits granted). Each is its own spec. Refactoring the agent is expected in 012 (its gate and holds) and 014 (its client); 011 refactors it only as far as the boundary needs. This feature's contribution is that the port they build on is already plain-typed, idempotent and independent of the host (FR-008, FR-009), and that the package can be run and tested alone (FR-016).

- **D18. The port contract is written as the service API, and 011 implements only what the host uses today.** From `research.md` (12 findings; the repo already matches the field on the separation of money from entitlement, idempotent ingestion by a stable id, a ledger with expiry, and fail-open by default): the written contract in PR B lists the full operation set a service exposes (`check`, `reserve`/`capture`/`release`, `record_usage`, `balance`, `grant`/`adjust`, `checkout`) with, for each, its idempotency key and its parameter-mismatch behaviour, but PR B implements only the three the host calls today. Not built in 011, and recorded so they are not mistaken for oversights: holds in the wallet, a conflict error on a key replayed with different parameters (today the wallet returns the existing transaction without comparing anything: `credits._begin_transaction`), outbound notifications, a check of cash collected against credits granted, and a configurable spend priority. **Why they are not folded in:** each changes the money path, and 011's proof is that every existing test passes unchanged across pure moves; mixing new behaviour into those PRs would remove that proof and make the review of a financial path harder, not easier. The maintainer accepts refactoring the agent; the refactors that change behaviour belong in 012, where new tests are the proof.

- **D19. (PR B, decided on the merged tree, 2026-10-14) The host reaches billing only through `app.billing_integration`; the contract stays in the core.** The plan said the host's wiring imports would "collapse into the port's own import". That cannot work: the port's contract is core-owned (D12), and a core module cannot import the host's wiring package. So two modules, two jobs. `app/billing/port.py` (core) is the contract: the `WalletPort` protocol, an in-process implementation over `credits`, and the plain types (`Pricing`, `Balance`, `Applied`). `app/billing_integration/port.py` (host) is the one module host code imports to use the wallet: it holds the implementation in use (a module-level `wallet`, which tests swap and a network client would replace), exposes the operations as functions, and, because importing anything in `app.billing_integration` wires the core, makes an unwired host path impossible. FR-011 is enforced in its stricter form: **outside `app/billing/`, `app/billing_integration/` and the two core operator scripts that PR C moves into the package (`scripts/credits.py`, `scripts/billing_inbox_sweep.py`), no file under `app/` or `scripts/` imports `app.billing`.** The webhook router moves into the core as `app/billing/http.py` and is mounted through `app/billing_integration/http.py`; the rate limit stays a host middleware on the path prefix. Raw SQL: `usage_events._enqueue_export`'s `INSERT … SELECT … FROM billing_customers` moves verbatim into `app/billing_integration/export.py` (the integration layer is the one host place allowed to know core table names; the savepoint and the degrade counter stay in `usage_events`), and a second scan fails the build when a non-docstring string literal that holds a core table name and an SQL keyword appears in any other host file.

### Questions for the plan (all four resolved in `plan.md`, 2026-10-13; the defaults below were kept except where noted)

Resolved: **O1** rating stays in the wallet API through 011. **O2** import name `credit_billing`; the router moves into the core in PR B and the package in PR C; FastAPI becomes an optional extra. **O3** (changed from the default): the package owns canonical SQL and `postgres-init/` keeps committed copies guarded by a byte-equality test, because single-file mounts would change three consumers that are hard to exercise. **O4** alerts and dashboard split by emitter and delivered like the SQL. Original wording kept for the record:

- **O1.** Does the cost-to-credits conversion (`credits_for_cost`, `Pricing`) stay in the core's wallet API, or become host policy that passes the core an amount of credits and an opaque snapshot? Default: leave the wallet API as is and record the coupling (D9), because moving it changes a signature the debit path and its tests depend on.
- **O2.** The import name of the package (it must not live under `app`), and whether the webhook route belongs in the package (as an installable router) or stays a thin host adapter. Default: the router moves with the package, with the rate limit and client-address keying unchanged.
- **O3.** How the host's init sequence and the package's schema scripts stay ordered on a fresh volume. Three places read `postgres-init/` today: the dev and prod compose bind mounts, the prod deploy workflow (which rsyncs only `postgres-init/` and `observability/` to the droplet), and `tests/containers.py` (which seeds testcontainers from every `*.sql` in it). A symlink into the package does not work for a bind mount (the target is outside the mounted directory). Candidates: bind-mount each package script as a single file into `/docker-entrypoint-initdb.d/` under its existing number (no duplicate, but the deploy rsync and the test helper must also find the package directory), or assemble a combined directory with a drift test. Default: single-file mounts, to be verified against a real container before relying on it; either way the fresh-volume schema must be identical (SC-005).
- **O4.** How the dashboard (which mixes core and host metrics) splits. Default: one dashboard in the package for the core's metrics, host panels stay in the host's.

# Feature Specification: A connection pooler in front of Postgres, and continuous profiling

**Feature Branch**: `infra/pgbouncer-and-pyroscope`

**Created**: 2026-11-04

**Status**: Implemented in one PR at the maintainer's request that PgBouncer and Pyroscope not be split. Verified against a real PgBouncer, Postgres, exporter and Pyroscope server in Docker (research.md); nothing has been deployed (the app never has been).

**Input**: "Should I apply PgBouncer and Pyroscope to profile the app?" The first answer was "not yet, and for neither is it the fix for a problem you have": nothing is deployed, and a profile of an I/O-bound worker mostly shows `await`. The maintainer then asked for both, the way a professional team builds them.

## Why

1. **The database connection count scales with the replica count, and nothing bounds it.** Every process that runs the graph holds two pools (appdata 10, checkpointer 10); `WORKER_CONCURRENCY.md` shows 100 `max_connections` fit about four processes before LiteLLM and Langfuse take their share, and exhaustion looks like an empty answer inside a 200 SSE stream.
2. **The pools were not written for a pooler.** The checkpointer sets `prepare_threshold: 0`, which in psycopg 3 means *prepare every query on its first run* (only `None` disables it); behind a transaction pooler that fails at random (`prepared statement "_pg3_1" does not exist`). The appdata pool uses the default of 5 and fails the same way under load. Neither was visible because nothing ran through a pooler.
3. **There is no way to see where CPU goes.** Metrics say a turn was slow, not which function. The two places that can be CPU-bound (ingest extraction, a blocking call on the API's event loop) are exactly where a flame graph answers a question a counter cannot.

## User Scenarios & Testing

### User Story 1 - The connection budget stops depending on replicas (P1)
**Acceptance**: (1) many client connections multiplex over a fixed, small number of server connections (measured: 150 concurrent transactions from 150 client connections used 5 server connections); (2) the cap is a tested invariant, `2 x max_db_connections + 30 <= max_connections`; (3) the readiness check goes through the pooler, so a dead pooler is a 503; (4) the pooler and its exporter run with the repo's hardening (read-only root, no capabilities, no new privileges).

### User Story 2 - The app is correct behind the pooler (P1)
**Acceptance**: (1) both pools, and the billing CLI's, disable client-side prepared statements, and a test through a real PgBouncer with `max_prepared_statements=0` fails if any of them does not; (2) no module of `app/` or `packages/billing` uses session state a transaction pooler would carry from one tenant's transaction to the next (a static scan with its own tests); (3) the appdata pool size is a setting.

### User Story 3 - An operator can see the flame graph (P2)
**Acceptance**: (1) with `PYROSCOPE_SERVER_ADDRESS` set, the API, the agent workers and the ingest worker push CPU profiles tagged `service`, `domain` and `replica`; (2) blank, the SDK is never imported; (3) Grafana has the datasource; (4) profiles carry no tenant, principal or thread id.

### User Story 4 - Neither can take the app down (P1)
**Acceptance**: (1) a missing SDK, a failing `configure` or an unreachable server leaves the process running (measured: startup 13 ms, no delay at shutdown); (2) the one failure the app can see is counted, `agent_profiling_start_failures_total`; (3) the pooler being saturated or down pages (`PgBouncerDown`, `PgBouncerClientsWaiting`, `PgBouncerMaxWait`).

## Requirements

- **FR-001** PgBouncer 1.26, transaction mode, in front of `appdata` and `checkpointer` only, in the dev and production compose files; LiteLLM, Langfuse, migrations and init scripts stay on Postgres directly. **FR-002** Images pinned by digest; the pooler, exporter and Pyroscope server hardened where their write paths were checked. **FR-003** `APPDATA_POOL_MAX_SIZE` in `Settings` and `.env.example`; `prepare_threshold=None` on every app pool.
- **FR-004** `app/core/profiling.py::start_profiling(service)` next to `configure_telemetry` in the three long-lived entrypoints; off unless `PYROSCOPE_SERVER_ADDRESS` is set. **FR-005** Pyroscope in both observability stacks, its port opened to the VPC only, a Grafana datasource.
- **FR-006** pgbouncer-exporter scraped in dev and, through the app droplet's collector, in production; three alerts.
- **FR-007** Each new degrade path has a metric; each new setting is in `.env.example`; patterns, README and `WORKER_CONCURRENCY.md` say what is and is not verified.

## Success criteria
SC-001 The integration test fails against a real PgBouncer with each pool's fix removed, one at a time (plan.md). SC-002 `make lint`, `make typecheck` and the hermetic suite are green. SC-003 `ENGINEERING_PATTERNS.md` 95 states the two findings (the `0` that means "always", and a profiler that is silent when its server is not ready).

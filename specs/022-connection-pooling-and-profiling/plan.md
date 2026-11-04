# Implementation Plan: A connection pooler in front of Postgres, and continuous profiling

**Spec**: [spec.md](spec.md) · **Research**: [research.md](research.md) · **Tasks**: [tasks.md](tasks.md) · **Branch**: `infra/pgbouncer-and-pyroscope` · **Base**: `main`

## Design in seven decisions

1. **Transaction pooling, for two databases only.** `appdata` and `checkpointer` go through PgBouncer; LiteLLM, Langfuse (Prisma), the exporters, `postgres-init` and the init scripts keep a direct connection. A transaction pooler is a promise by the client (no session state); only code this repo owns can keep it.
2. **The client disables prepared statements; the pooler's own backstop stays on.** `prepare_threshold=None` works with any PgBouncer version and setting. The tested pooler is made stricter than production (`MAX_PREPARED_STATEMENTS=0`, three server connections for thirty clients), so a pool that prepares fails in CI and does not work by luck.
3. **The budget is a tested sum, not a hope.** `MAX_DB_CONNECTIONS` (pool 15 + reserve 3) is the hard cap per database; `databases x cap + 30 <= max_connections`, with production writing `max_connections` down so the test can read it. What replaces "too many clients" is waiting, which has two alerts before the 30 s `QUERY_WAIT_TIMEOUT` turns it into an error.
4. **No session state, enforced by a scan.** Principle I behind a pooler: a session `SET`, advisory lock or `LISTEN` would be inherited by the next tenant's transaction. The repo uses only transaction-local state; `tests/core/test_pooler_safe_sql.py` keeps it so and has its own table of cases.
5. **Profiling is push, optional and silent about its own failure.** `app/core/profiling.py` imports the SDK only when `PYROSCOPE_SERVER_ADDRESS` is set, never raises, tags `domain` and `replica` and never a tenant. The one failure the process can see is counted (`agent_profiling_start_failures_total`); a down or not-ready server is invisible to it, by the SDK's design (research.md).
6. **Pyroscope runs in the v1 write-path mode, pinned by digest.** Measured against the 2.x default: ready in 19 s instead of about 3 minutes, 38 MiB instead of 97. On in dev, off in production until it has run on the droplet (`PYROSCOPE_SERVER_ADDRESS` empty there).
7. **Every new image is pinned by digest and hardened from a run.** The multi-arch index digest, read from the registry; read-only root, no capabilities, no new privileges; the write paths found by running it, then asserted.

## Constitution Check

| Principle | Touched | How it holds |
|---|---|---|
| **I. Fail-closed tenant isolation** (NN) | Yes | Pooled server connections are lent to different tenants' transactions. Only transaction-local state exists (`set_config(..., true)`, `pg_advisory_xact_lock`, `SET LOCAL`); the scan fails the build on anything session-scoped; the integration test runs 200 transactions over three server connections and finds no inherited setting. SQL still carries `WHERE tenant = %s`; nothing about it changed. Profiles carry no tenant. |
| **II. Mandatory approval** (NN) | No | No tool, approval or graph change. |
| **IV. Exactly-once side effects** (NN) | Indirectly | The guard is a row-level unique key inside one transaction; a pooler keeps transaction boundaries (`get_connection` still commits on exit, tested through the pooler). |
| V. Bounded, observable failure | Yes | The pooler's saturation and absence are alerted (`PgBouncerDown`, `PgBouncerClientsWaiting`, `PgBouncerMaxWait`); every wait has an end (`QUERY_WAIT_TIMEOUT`, `IDLE_TRANSACTION_TIMEOUT`); readiness goes through the pooler; the profiler's start failures are counted. |
| VII. Real backends where they matter | Yes | A real PgBouncer and Postgres in `tests/integration/test_pgbouncer.py`; the compose files were brought up for real. Not run: the droplet, the VPC, a browser on the flame graph. |
| VIII. Why-first docs, honest gaps | Yes | `ENGINEERING_PATTERNS.md` 95, `WORKER_CONCURRENCY.md`, research.md "Not verified". |

## Structure

```text
app/core/{config,metrics,sql_store}.py    3 settings, 1 counter, POOLER_SAFE_CONNECTION_KWARGS
app/core/profiling.py                     start_profiling(service)
app/adapters/postgres/checkpointing.py    prepare_threshold: 0 -> None;  app/api/health.py  same
packages/billing/.../cli/_runtime.py      the same, repeated (the package imports nothing from the app)
deploy/compose/*.yml                      pgbouncer, pgbouncer-exporter, pyroscope; app URLs and depends_on
observability/ + infra/terraform          scrape jobs, 3 alerts, Grafana datasource, port 4040 to the VPC
tests/core/test_{pool_settings,pooler_safe_sql,profiling,compose_pooler,compose_profiling}.py   tests/integration/test_pgbouncer.py
```

## Mutation check (run, not assumed)
Each of these made at least one test fail when removed, then was restored byte for byte: appdata pool kwargs (hermetic and integration) · checkpointer `prepare_threshold` back to `0` (hermetic, scan and integration) · `configure()` failure escaping · an app URL back on `postgres:5432` · a pool cap over the Postgres budget · an alert on a metric the exporter does not emit · the profile port open to `0.0.0.0/0`.

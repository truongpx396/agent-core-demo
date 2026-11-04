# Tasks: A connection pooler in front of Postgres, and continuous profiling

`[x]` done and verified as the line says; `[ ]` not done.

## Phase 1: The app is correct behind a pooler (US2)
- [x] T001 `POOLER_SAFE_CONNECTION_KWARGS` in `sql_store.py`; the appdata pool, the checkpointer pool (was `prepare_threshold: 0`), the readiness probe and the billing CLI pool pass it. `APPDATA_POOL_MAX_SIZE` in `Settings` and `.env.example`.
- [x] T002 `tests/core/test_pool_settings.py` (the pools, and a scan of every connection site with its own table) and `tests/core/test_pooler_safe_sql.py` (no session state in `app/` or the billing package).
## Phase 2: PgBouncer (US1)
- [x] T010 `pgbouncer` and `pgbouncer-exporter` in `docker-compose.yml` and `docker-compose.prod.yml`: digest-pinned, hardened from a run, app URLs and `depends_on` moved, `max_connections` written down in production.
- [x] T011 Scrape jobs (dev Prometheus, the app droplet's collector), alerts `PgBouncerDown`, `PgBouncerClientsWaiting`, `PgBouncerMaxWait`.
- [x] T012 `tests/core/test_compose_pooler.py` (the contract) and `tests/integration/test_pgbouncer.py` against a real PgBouncer from the shipped service definition, with the self-check that the old settings fail.
- [x] T013 Brought up from the real dev file in an isolated project: the app's pools and checkpointer `setup()`, 720 checkpoint operations and 400 queries without error; the billing migration applied and re-applied through it.
## Phase 3: Pyroscope (US3, US4)
- [x] T020 `app/core/profiling.py`, three settings-driven entrypoint calls, `agent_profiling_start_failures_total`, `pyroscope-io==1.3.0` (one line added to the lock: the package has no dependencies).
- [x] T021 Pyroscope in both observability stacks (v1 write path, 7-day retention, hardened), Grafana datasource (health "Data source is working"), `PYROSCOPE_SERVER_ADDRESS` in the app environments (on in dev, empty in production), Terraform rule for 4040 to the VPC.
- [x] T022 `tests/core/test_profiling.py`; a hardened container pushed a profile to the server from the real compose file, with `domain` and `replica` as labels.
## Phase 4: Docs
- [x] T030 `ENGINEERING_PATTERNS.md` 95, `WORKER_CONCURRENCY.md`, README, `ARCHITECTURE.md`, `CLAUDE.md`, `infra/README.md`, `.env.example`, `deploy/env/prod.env.example`, this spec.

## Not done (disclosed)
- [ ] T040 Run any of it on the production droplets: the firewall and `ufw` rule for 4040, the app-to-observability address, `terraform plan` against real state (no `terraform` binary here). HUMAN.
- [ ] T041 Open the flame graph in Grafana in a browser and add a panel to a dashboard (only the datasource's health was checked).
- [ ] T042 Measure the profiler's overhead under `make loadtest-queued`; if it matters, lower `PYROSCOPE_SAMPLE_RATE`.
- [ ] T043 Dependabot for the compose digests (`/deploy/compose`, `docker-compose` ecosystem, same two-week cooldown): left out because it would also propose LiteLLM's deliberately pinned digest.
- [ ] T044 Move Pyroscope to the 2.x default write path once its start-up is measured acceptable (`-write-path=ingester` is a documented but transitional mode).
- [ ] T045 Regenerate `requirements-lock.txt` from a clean install the documented way; here one dependency-free line was added by hand so unrelated pins would not move.

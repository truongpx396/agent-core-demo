# Research: what was read, what was run, what was not

All runs on 2026-11-04, Docker 29.5.2 on arm64, in an isolated network beside the maintainer's own stack (nothing of theirs touched).

## Run (measured)
- **psycopg 3.3.4 (`psycopg/_preparing.py`)**: `prepare_threshold=None` disables prepared statements; `0` prepares on the first execution (`count >= threshold`); the default is 5. The checkpointer's `prepare_threshold: 0` is the *opposite* of pooler-safe. An earlier answer in this conversation called it pooler-friendly; that was wrong, found by reading the source.
- **edoburu/pgbouncer v1.26.1-p0** (PgBouncer 1.26.1), `pool_mode=transaction`, `max_prepared_statements=0`, `default_pool_size=5`: runs read-only, `--cap-drop ALL`, `no-new-privileges`, as uid 70, with one tmpfs on `/etc/pgbouncer` (its entrypoint writes `pgbouncer.ini` and `userlist.txt` there). Scram auth works from `DATABASE_URLS`.
- **Behind it**, 60 runs of one statement over a 3-connection pool: `prepare_threshold=0` -> 8 distinct errors (`DuplicatePreparedStatement`, `InvalidSqlStatementName`); default -> none (a single client reuses one server connection); `None` -> none. A heavier load (12 pools x 40 transactions with `pg_sleep`): default -> ~70 of 480 fail, `None` -> 0 of 480, three attempts each. So the default is not safe, and a light test would not have shown it.
- **Multiplexing**: 150 concurrent transactions from 15 pools of up to 10 client connections finished on at most 5 server connections.
- **prometheuscommunity/pgbouncer-exporter v0.12.1** runs hardened (user `nobody`); metrics used: `pgbouncer_up`, `pgbouncer_pools_client_waiting_connections`, `pgbouncer_pools_client_maxwait_seconds`.
- **pyroscope-io 1.3.0 / 1.4.0 (cp313 manylinux wheels)**: from a container with `cap_drop: ALL`, read-only root, uid 1000, `no-new-privileges`, profiles reach the server and the flame graph contains the function that burned the CPU. `configure()` returns in ~13 ms. An unresolvable host or a refused port: exit 0, no startup or shutdown delay, errors only in the native log. **A server that is not ready drops pushes silently** (my first two runs showed nothing for this reason).
- **grafana/pyroscope 2.3.5**: the default (`v1-v2-dual` storage, segment-writer write path) took ~3 minutes to report ready and 97 MiB idle; `-architecture.storage=v1 -write-path=ingester` (the v1 behaviour, documented in the 2.0 release notes) took 19 s and 38 MiB, and accepts the SDK's pushes.

## Not verified (disclosed)
- Nothing ran on the production droplet or across the VPC; the firewall rule and the app-to-observability address are untested (`terraform validate` only).
- The Grafana datasource was provisioned but the Explore / flame-graph panel was not opened in a browser.
- Pyroscope's overhead on the app was not measured under load (the SDK samples at 100 Hz by default; `PYROSCOPE_SAMPLE_RATE` lowers it).
- `-write-path=ingester` is a transitional mode of a 2.x server; the default mode is not what was chosen (cost above) and a later 2.x may remove the flag.
- Compose image digests are not tracked by Dependabot (it reads `/` and `/docker`); they are bumped by hand like LiteLLM's.

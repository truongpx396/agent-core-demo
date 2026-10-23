---
paths:
  - "tests/**"
  - "pyproject.toml"
---

# Writing and running tests

Constitution Principle VII applies. The default suite must stay hermetic and fast.

## Default suite (`make test` / bare `pytest -q`)

- Fake LLM, no live Postgres/Redis/Qdrant/ml-service. `pyproject.toml`'s `addopts` excludes every
  marked tier by default; an explicit `-m` on the command line overrides it (it does not AND).
- `tests/conftest.py` has eight autouse guards: `mock_search_docs`, `mock_semantic_cache`,
  `mock_ml_moderation`, `mock_appdata_postgres`, `no_real_tracing`, `fixed_embedding_identity`, `no_real_qdrant`,
  `mock_classification` (the model router's /v1/decide call). A test's result must not depend on which
  containers happen to be running. If you add a live-service call on the turn path, add an
  autouse mock for it there. Inject a specific fake with `GraphDeps(search_docs=fake)` or the
  node-level `make_*_node(fake)` factories — that bypasses the default mock.
- Patch a module's OWN `get_connection` binding (`usage_ledger.get_connection`), not
  `sql_store.get_connection` — `from X import Y` makes a separate reference.
- `asyncio_mode = "auto"`: write `async def test_...` and `await` directly. Don't wrap bodies in
  `asyncio.run(...)`.
- `tests/core/test_doc_references.py` fails when a repo doc (or a rules file) names a file, or a function in it, that is gone. Update the reference; if it is MEANT to point at something retired, add it to that test's `INTENTIONAL` set with the reason.
- Name tests as sentences describing the behavior, e.g.
  `test_add_comment_a_repeated_tool_call_id_is_a_no_op_not_a_duplicate`.
- A bug fix needs a regression test that fails without the fix.

## Real-backend tiers

- Markers: `integration`, `llm`, `e2e`, `deepeval`, `crawl`, `sandbox`, `provider_sandbox` (a payment provider's real sandbox over the internet, manual, never CI). Only mark a test with one
  of these if it genuinely needs the real service; a fake-able test belongs in the default tier.
- Real services come from `tests/containers.py::ensure_postgres/redis/qdrant/ml_service/
  crawl4ai/ollama()`. Each starts its own testcontainer and the test self-skips (never fails) when
  Docker is unreachable.
- A test that re-applies a `postgres-init/*.sql` migration to the shared database goes through `tests/integration/schema_reapply.py::reapply`, never a bare `execute(sql)`: DDL takes table locks (even `ADD COLUMN IF NOT EXISTS` when nothing changes) and deadlocks with other workers' writes, and the other worker's test is the one that fails (pattern 60).
- `faulthandler_timeout = 300` (`pyproject.toml`) prints every thread's stack for a test that runs that long and lets it continue. To see an integration run's progress when its output is piped to a file, use `PYTHONUNBUFFERED=1 ... -v` (the default is block-buffered and hides the tail).
- Under `pytest -n auto` containers are shared across workers via a fixed cache dir, and
  `AsyncPostgresSaver.setup()` runs once under a lock before any test can race it. Don't call
  `.setup()` per worker. Integration runs use `--dist=loadgroup`.
- In CI, `e2e` (sync Playwright) runs as a separate pytest invocation from `llm`/`crawl`
  (async): under one `-n auto` run xdist can put both kinds on the same worker, and Playwright
  then corrupts that worker's asyncio state. `make test-live` still runs them in one invocation,
  so if unrelated async tests fail with `Runner.run() cannot be called from a running event
  loop`, split the run the way `.github/workflows/ci.yml` does.

## What a passing test does and doesn't prove

- Store tests use fake cursors and assert SQL text and params. That proves statement shape, not
  that a real `UNIQUE`/`ON CONFLICT` behaves; don't write or word a test as if it did. Add an
  `integration` test when real constraint, broker or checkpointer behavior is what's at stake.
- deepeval, garak and promptfoo-redteam are advisory signals (small local judge models are
  unreliable graders). Don't turn them into a hard gate.
- The same goes for an e2e test that needs the small local model to CHAIN several tool calls
  (a skill, a subagent delegation): mark it `@pytest.mark.advisory` and CI runs it in the
  non-blocking `e2e and advisory` step instead of the gate. Don't mark a single-hop test
  advisory to silence a failure; `tests/core/test_live_e2e_gate_split.py` lists the tests that are.

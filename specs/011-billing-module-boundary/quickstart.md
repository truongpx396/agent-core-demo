# Quickstart: prove a PR of this feature changed nothing

Run from the repo root with the venv active (`source .venv/bin/activate`). Docker needs to be reachable for step 5 (`export PATH=$PATH:/usr/local/bin` if the shell cannot find it).

## 0. Record the baseline (once, before editing)
```bash
pytest --collect-only -q | tail -1                       # collected count
make test 2>&1 | tail -3 > /tmp/baseline-test.txt        # passed / skipped
make test-integration 2>&1 | tail -3 > /tmp/baseline-integration.txt
python - <<'PY' > /tmp/baseline-surface.txt
from app.core import config, metrics
import re, pathlib
print(sorted(config.Settings.model_fields))                                   # fields
print(sorted(n for n in dir(metrics) if n.startswith("agent_")))               # metric objects
PY
git ls-files .env.example | xargs sha256sum; make help | sha256sum
```

## 1. Gates
```bash
make lint && make typecheck && make test
```
Expect: the same tests as the baseline pass, plus exactly the new ones.

## 2. The boundary test goes red and green (PR A)
```bash
echo 'from app.agent import gateway  # PROBE' >> app/billing/inbox.py
pytest tests/billing/test_billing_core_boundary.py -q     # FAILS, names app/billing/inbox.py and the import
git checkout app/billing/inbox.py
pytest tests/billing/test_billing_core_boundary.py -q     # passes
```

## 3. The surface is unchanged
Re-run the baseline's `python` block and `diff` it against `/tmp/baseline-surface.txt`. Expect **no output**. The four moved counters must still be reachable by the same names (now in `app.billing.metrics`); the field list and `.env.example` hash must match; `make help | sha256sum` must match.

## 4. The core stands alone at import
```bash
python -I -c "import sys, app.billing.credits, app.billing.webhooks, app.billing.inbox, app.billing.providers; \
bad=[m for m in sys.modules if m.startswith('app.') and not m.startswith('app.billing')]; print(bad); assert not bad"
```
Expect `[]`.

## 5. The integration tier (real Postgres), before and after
```bash
make test-integration
```
Expect the same passed/skipped as `/tmp/baseline-integration.txt`. This is the proof for the connection indirection; it also runs in CI.

## 6. A collector check, only if the meter scope is touched (PR C)
Run the throwaway-collector probe from `research.md` P6 against the new scope name and read `otel_scope_name` on the new series.

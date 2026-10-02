# Quickstart: Validate Multi-Domain Composition

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Contracts**: [contracts/](./contracts/)

A validation guide: what to run, what you should see, which requirement it proves. Cheapest tier first.
**Activate the venv**: `source .venv/bin/activate`.

> **Read this first.** A green Tier 1 proves the seam, each domain's sandbox set, the approval gate per write tool, the write-tool
> contract, the stores' *statement shape* and both scheduled jobs' happy paths. It does **not** prove tenant or owner scoping against a real
> database (the stores are tested with fake cursors), and it did **not** catch B7 or B8, which Scenarios B7 and B8 below reproduce. Those
> two scenarios are expected to show the defect *as the system stands*.

---

## Tier 1 — Hermetic (no services, ~3 s)

```bash
pytest tests/domains tests/agent/test_manifest.py tests/scripts/test_ops_digest.py tests/scripts/test_followup_sweep.py -q
```

**Expected** (observed 2026-10-02): `214 passed`.

| Requirement | Evidence |
|-------------|----------|
| FR-001/FR-002/SC-001 a second domain on the unmodified graph; the default domain unaffected | `tests/agent/test_manifest.py` (`TestDefaultsToTheEcorpDomain`, `TestSecondDomainProvesReuse`) |
| FR-003/SC-008 every name resolves; an unknown name lists the valid ones | `tests/domains/test_registry.py` |
| FR-005/FR-007/FR-009 a domain's tool set; the default assistant's tools absent | `tests/domains/support/test_domain.py::TestSandboxing`, `tests/domains/sales/test_domain.py::TestSandboxing` (**no ops equivalent — A5**) |
| FR-010 sandbox tools declared outward; approval per write tool | `test_every_sandbox_tool_present_is_declared_outward` (each domain); `TestMandatoryApprovalGate` and the per-tool approval tests |
| FR-010/SC-002 every write tool refuses without ctx and uses `idempotent()` with its id and name | `tests/domains/test_write_tools_contract.py` (27 tools × 2 checks + 2 guards = 56) |
| FR-011/SC-004 scoped delegation menu | `tests/domains/*/test_domain.py::TestDomainScopedSubagent` |
| FR-006 leak check uses the domain's own prompt | `test_manifest.py::TestSecondDomainProvesReuse::test_check_output_leak_detection_uses_this_domains_own_system_prompt` |
| FR-013/FR-016/FR-017 store statements carry the tenant and are parameterized; upsert; lost-lead closing | `tests/domains/{support,sales,ops}/test_store.py` (fake cursors) |
| FR-021/SC-006 the digest flags, summarizes, posts once, records usage | `tests/scripts/test_ops_digest.py` |
| FR-022 the sweep drafts, posts, marks done; nothing due → no model call; usage recorded | `tests/scripts/test_followup_sweep.py` |
| ops metric checks: threshold, `None`, query failure | `tests/domains/ops/test_metrics_client.py` |

**Not covered here**: FR-015/SC-005 (B7), FR-023/SC-007 (B8), FR-024 (A3), FR-026 (A4), FR-025 (`ops_investigate.py` — A5), FR-029/SC-010 (A7).

---

## Tier 2 — Real Postgres (Docker, no model)

There is **no** integration test for these stores. `make test-integration` does not exercise `support_tickets`, `crm_*` or `ops_incidents`; the
worker subprocesses in `tests/integration/test_worker_scaling.py` write to `appdata` only through the default assistant's tools; and the one live test
that drives domain tools (`tests/live/test_domain_crawl_tools_live.py`) covers only the two crawl tools and says in its docstring that it avoids the
stores. Principle VII known gap — see tasks.

---

## Scenario B7 — Reproduce: another customer of the same tenant can read, escalate and comment on a ticket (hermetic; expected: it reproduces)

In a scratch Python session (do not commit), with no services:

1. Replace `app.domains.support.store.get_connection` with an async context manager yielding a fake connection whose `execute(sql, params)`
   **emulates the database by matching rows only on the predicates the statement carries** (tenant and ticket id for the three statements;
   requester only if the statement has a `requester` condition). Seed one ticket: tenant `ecorp`, id 7, `requester = "telegram:111"`,
   a subject and description.
2. As a **different** customer — ctx `{tenant: "ecorp", principal: "telegram:222"}` — call `tools._check_ticket_status_impl(7, ctx)`,
   `store.escalate_ticket("ecorp", 7, "x")` and `store.add_comment("ecorp", 7, "hello")`.

**Observed 2026-10-02**: the status call returns the ticket (`Ticket #7 — open (high priority): My card was charged twice`); `escalate_ticket`
and `add_comment` both return `True`; none of the captured statements contains a `requester` condition (parameters were `['ecorp', 7]`).
**Fixed when**: all three return "not found" for `telegram:222` while `telegram:111` still succeeds. This is the failing test the B7 fix starts with.

## Scenario B8 — Reproduce: one failing follow-up aborts the sweep (hermetic; expected: it reproduces)

1. Patch `scripts.followup_sweep`: `store.due_followups` → three items (Ada, Bob, Carol; ids 1–3); `store.mark_followup_done` → record the id;
   `notify.post_to_team_channel` → record the message; `record_usage` → no-op.
2. Run `await run_followup_sweep(llm=<a stub whose ainvoke raises RuntimeError when the human prompt mentions "Bob">)`.

**Observed 2026-10-02**: the sweep **raises**; `marked done: [1]`; one draft posted; Carol (id 3) is never handled; Bob stays `pending` and would fail
again next run. **Fixed when**: the sweep drafts Ada and Carol, counts and logs Bob's failure, and exits non-zero at the end. This is the failing
test the B8 fix starts with.

## Scenario A7 — Inspect: no length bound on any free-form tool argument (hermetic; expected: no bounds)

```python
from app.domains.registry import DOMAINS
for dom in ("support", "ops", "sales"):
    for t in DOMAINS[dom][1].tools():
        sch = t.args_schema.model_json_schema()
        print(dom, t.name, {k: v.get("maxLength") for k, v in sch["properties"].items() if v.get("type") == "string"})
```

**Observed 2026-10-02**: every string field prints `None` (41 free-form string fields across 31 domain-module tools). The default assistant's
`add_note` (`title` 200, `content` 4000) and `remember` (2000) print numbers.

## Tier 3 — Full local stack, manual walk-through

**Prerequisites**: `make up`, `make pull-models`, `make ingest`, `make serve`, and a worker per domain: `make agent-worker-support`,
`agent-worker-ops`, `agent-worker-sales`. For the ops domain also `make obs-up` (Prometheus). Helper (a function — unquoted variables are not
word-split in zsh):

```bash
ask() { curl -N -X POST "localhost:8000/chat/stream/queued" -H 'Content-Type: application/json' \
  -H 'X-Tenant-Id: ecorp' -H "X-Principal-Id: ${4:-alice}" -H "X-Domain: $3" \
  -d "{\"message\":\"$1\",\"thread_id\":\"$2\"}"; }
```

| # | Request | Expected | Proves |
|---|---------|----------|--------|
| 1 | `ask "Open a ticket: my invoice total is wrong" c-1 support` | stream ends `approval_required` naming `create_ticket`; approve via `/chat/resume` → a ticket exists | FR-007, FR-010 |
| 2 | `ask "what is 17*23?" c-2 support` | the assistant has no calculator — it answers without one or says it can't | FR-005, FR-007 |
| 3 | `ask "status of ticket 1" c-3 support bob` (a different principal, same tenant) | **today: returns alice's ticket (B7)**; after the fix: "no ticket found" | FR-015, SC-005 |
| 4 | `ask "what is breaking?" o-1 ops` | `fetch_metrics_summary` runs without a pause and a summary of the readings is returned | FR-008 |
| 5 | `ask "log an incident: queue is backing up" o-2 ops` | `approval_required` for `log_incident`; on approval an incident exists (no tenant) | FR-010, FR-018 |
| 6 | `ask "Log that ada@example.com wants a demo" s-1 sales`, approve; then `ask "schedule a follow-up with ada@example.com in 0 days" s-1 sales`, approve | a lead and a pending follow-up exist | FR-009, FR-016 |
| 7 | `python -m scripts.followup_sweep` | `Drafted 1 follow-up nudge(s).`; the draft is in `var/team_channel.log`; the follow-up is `done` | FR-022 |
| 8 | `python -m scripts.ops_digest` | one digest line in `var/team_channel.log`; run it again → a second one | FR-021, A6 |
| 9 | `python -m scripts.ops_investigate "is anything unusual right now?"` | a read-only answer; ask it to "log an incident" and the answer is empty | FR-025 |
| 10 | `AGENT_DOMAIN=nosuch make agent-worker` | exits at once listing the valid domains | FR-003, SC-008 |

### Replay safety

Same manual check as feature 003 — repeat an insert by hand with the first row's `tool_call_id` and confirm `ON CONFLICT (tool_call_id) DO NOTHING`
affects 0 rows; for the sales domain use `crm_followups`.

## Troubleshooting

- *A domain request hangs ~30 s then reports "is an agent-worker running for this domain?"*: no worker for that `X-Domain` (feature 004).
- *`fetch_metrics_summary` returns "unknown" readings*: Prometheus (`make obs-up`) is not reachable; the tool degrades to `None`, not an error.
- *The sweep drafts nothing*: follow-ups are scoped to the default tenant, and only `pending` ones due now or earlier count.
- *Scenario B7/B8 do not reproduce*: confirm you are not on a branch that already fixed them.
- *Do not* run `make clean`, `clear-*` or `restart-all` while validating.

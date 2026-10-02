# Contract: Scheduled and Ad-Hoc Jobs

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §6–§7](../data-model.md) | **Why no loop**: constitution Principle II (last bullet),
`GRAPH_PATTERNS.md` pattern 47

**Status**: Retrospective — `scripts/ops_digest.py`, `scripts/followup_sweep.py`, `scripts/ops_investigate.py`; tests in
`tests/scripts/test_ops_digest.py`, `tests/scripts/test_followup_sweep.py`. **B8, A2, A3** below are open.

## The rule

An unattended job can never approve itself, so it **never calls a tool through the agent loop**. It calls the domain's own store, metrics and
notification functions directly, and uses a model only for a plain, non-tool-calling completion that turns numbers or notes into prose. Anything
that goes out to a person is a *draft for a human*. The jobs are meant to run under an external scheduler; nothing in the repository schedules them.

## `python -m scripts.ops_digest` (cron/timer, e.g. `0 8 * * *`)

1. `metrics_client.fetch_readings()` — each configured check's current value (an unreachable Prometheus or an empty result → `None`, never an error).
2. `detect_anomalies(readings)` — plain threshold comparison: a reading **strictly greater** than its ceiling is flagged; `None` is never flagged.
3. One completion (temperature 0, system prompt: 3–6 plain sentences, cite the numbers, never invent a cause) over the readings and flagged items.
4. `notify.post_to_team_channel("ops-digest", summary)` — **once per run**.
5. Usage recorded as principal `ops-cron`, id `ops-digest:<date>`, only when the model reports tokens.

"Safe to re-run" — each run posts a new digest (the script's docstring says "idempotent"; it is not — A6).

## `python -m scripts.followup_sweep` (cron/timer, e.g. `0 9 * * *`)

1. `due_followups(tenant, now)` — pending follow-ups due now or earlier, with lead name and contact, ordered by `due_at`. **Tenant = the default tenant
   only (A3).** None due → return `[]` without calling the model.
2. For **each** due item, in order: draft one nudge (the sales system prompt plus a constant drafting instruction, the lead and the scheduling note);
   record usage as `sales-followup-cron` (`followup-sweep:<id>`); post `Draft nudge for <name> (<contact>): <draft>` to `sales-followups`; **then**
   `mark_followup_done` (A2: a crash between the post and the mark repeats the draft on the next run).
3. Nothing is sent to a lead; a human reviews every draft. Returns the drafted texts.

**Failure policy (required vs. as built)**

| Failure | Required | As built |
|---------|----------|----------|
| the model call for one item raises | log, count, continue with the rest, exit non-zero at the end | **the sweep aborts; later items are never handled; the failing item stays pending (B8)** |
| the team-channel push fails | the post never raises; counted and alerted (feature 003) | ✔ (the local log file still receives the draft) |
| nothing due | return without a model call | ✔ |
| more than one tenant has due items | each is swept | **only the default tenant (A3)** |

## `python -m scripts.ops_investigate "<question>"` (ad-hoc, a person present)

Builds the ops graph once with an in-memory saver, a fresh thread id, the local identity (`local:<os user>`) and `require_approval=False`, runs the
question, and returns the last assistant message (`(no answer produced)` if none). A read-only investigation completes. **A state-changing call
(`log_incident`, `resolve_incident`, `post_to_team_channel`) pauses at the mandatory gate and this one-shot has no resume path, so the answer is
typically empty** — disclosed in the script's docstring and the README as the honest signal that a person must be in the loop. No tests (A5).

## Invariants a change must preserve

1. A scheduled job never enters the tool loop and never auto-approves.
2. A job's identity is a fixed automated principal, never a person's.
3. A multi-item job handles each item independently (**B8**).
4. Whatever leaves the system is a draft a human sends.

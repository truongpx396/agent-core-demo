## What and why

<!-- The change in 1–3 sentences and the problem it solves. For a bug fix: the failure mode and
     its root cause, and anything you deliberately left undone. -->

## Scope

- [ ] One logical change a reviewer can hold in their head. Target ≤ ~400 hand-written lines;
      ~1,000 is the ceiling (generated, lock and scaffold files don't count). If this is larger,
      say why it can't be split.

## Checklist

<!-- Delete the lines that don't apply; don't tick what you didn't do. -->

- [ ] `make lint`, `make typecheck` and `make test` pass (CI runs the same)
- [ ] Bug fix → a regression test that fails without the fix
- [ ] Store/queue/worker/SQL change → `make test-integration` run (needs Docker)
- [ ] New or changed `mutating`/`outward` tool → capability tier, tenant scoping and
      idempotency story are stated below (see `.claude/rules/side-effect-tools.md`)
- [ ] New degrade-and-continue path → a metric, plus an alert rule if a human could be left
      unaware of committed business state
- [ ] Prompt, model-alias or retrieval change → `make promptfoo` and `make eval` results noted
- [ ] `GRAPH_PATTERNS.md` / README updated; any gap this leaves open is disclosed there
- [ ] Touches a constitution principle (`.specify/memory/constitution.md`) → which one, and how
      it still holds
- [ ] No secrets or `.env` / `CREDENTIALS.local.md` content in the diff or this description

## Test plan

- [ ] <!-- what you ran or checked, and what it proved -->

## Notes for the reviewer

<!-- Anything non-obvious: risky areas, follow-ups you chose not to do, doc/code discrepancies
     you found along the way. -->

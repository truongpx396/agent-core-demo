# Quickstart: Validate Skills and Subagents

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Contracts**: [contracts/](./contracts/)

A validation guide: what to run, what you should see, which requirement it proves. Cheapest tier first.
**Activate the venv**: `source .venv/bin/activate`.

> **Read this first.** A green Tier 1 proves the loaders, the visibility rules, the read-only resolution, the delegated run's isolation and budgets, and the two skill guardrails —
> against fakes and `tmp_path` folders. It does **not** prove the shipped catalogs are well-formed (no test loads them — A7), that the container images contain them (they do not — B16),
> or that a run's memory and spend are accounted for on every path (B13, B14) — and it did **not** catch B13–B16, which Scenarios B13–B16 below reproduce. Those four scenarios are
> expected to show the defect *as the system stands*.

---

## Tier 1 — Hermetic (no services, ~10 s of tests)

```bash
pytest tests/agent/test_skills.py tests/agent/test_subagents.py tests/agent/test_tools.py \
       tests/agent/test_routing.py tests/agent/test_graph_integration.py \
       tests/agent/test_concurrent_turns.py tests/agent/test_safety_budgets.py \
       tests/agent/test_streaming_terminal_events.py \
       tests/domains/support/test_domain.py tests/domains/ops/test_domain.py tests/domains/sales/test_domain.py -q
```

**Expected** (observed 2026-10-02): `353 passed, 8 deselected` in about 9 s. (These files also hold tests for features 001/003/005; the narrower
`pytest tests/agent/test_tools.py -k "Skill or skill or Subagent or subagent"` gives `58 passed`.) With no Langfuse running, the process may then print a run of
`Unexpected error occurred … langfuse.com/support` lines at exit and take up to a minute to return; they are exit-time telemetry flush noise, not test output.

| Requirement | Evidence |
|-------------|----------|
| FR-001/FR-002/FR-010 loader accept and skip rules | `tests/agent/test_skills.py::TestParseSkillFile`, `TestLoadSkills`; `tests/agent/test_subagents.py::TestParseSubagentFile`, `TestLoadSubagents` (a malformed file skipped, a duplicate keeps the first, a missing folder is empty) |
| FR-003/FR-004 disk is truth; search over-fetch, filter, truncate; degrade messages | `test_tools.py::TestSkillSearch` (`…searches_the_dedicated_skills_collection`, `…formats_hits_as_name_and_description`, `…no_hits_tells_the_model_to_proceed…`, `…missing_collection_degrades…`), `TestUseSkill::test_returns_the_matched_skills_full_body_from_disk_not_qdrant` |
| FR-005/FR-011 visibility; opposite defaults | `TestSkillVisibleToDomain`, `TestFilterSkillHitsByDomain` (incl. `…drops_a_hit_for_a_skill_no_longer_on_disk`), `TestMakeSkillTools` (`…refuses_a_skill_tagged_to_another_domain_even_by_exact_name`), `TestSubagentDeclaredForDomain`, `TestBuildSubagentRegistryDomainFilter` |
| FR-006 no ctx needed for skills | `TestSkillSearch`/`TestUseSkill` `…needs_no_ctx_it_is_a_bundled_capability_not_tenant_data` |
| FR-007 list order | `TestSkillToolsFirst` |
| FR-008 `use_skill` without a search | `test_routing.py::TestUseSkillCalledWithoutSearch`, `TestShouldContinueUseSkillWithoutSearch`; `test_graph_integration.py::TestUseSkillWithoutSearchGate` |
| FR-009 required tool reminder and correction | `test_nodes.py::TestSkippedRequiredSandboxAfterSkill`, `test_retry_output_tells_the_model_the_skill_named_a_required_tool`; `test_agent_node.py::test_agent_appends_a_sandbox_reminder_after_a_sandbox_requiring_skill_loads`, `…skips_the_sandbox_reminder_when_no_skill_mentions_the_tool` |
| FR-012 read-only resolution; no recursion; empty stays empty | `test_tools.py::TestResolveSubagentTools` (all six), `TestSubagentDomainPluginCapabilityFix` |
| FR-013 closed enum, blank task, per-product tool | `TestRunSubagentTool`, `TestMakeDomainSubagentTool`; each product's `TestDomainScopedSubagent` (`…is_not_the_ecorp_level_run_subagent_object`, `…menu_offers_only_the_…`) |
| FR-014 read-only, never pauses | `test_routing.py::TestShouldContinueSubagent`; each product's `TestDomainScopedSubagent::test_never_pauses_it_is_read_only` |
| FR-015/FR-016 ctx and isolation | `TestRunSubagentImpl` (`…refuses_without_ctx`, `…task_is_the_subagents_sole_human_message_not_parent_history`, `…nested_system_prompt_has_its_own_prompt_and_the_citation_warning`) |
| FR-017 own budgets; no cache, no follow-ups | `TestRunSubagentImpl` (`…respects_its_own_smaller_iteration_ceiling…`, `…hitting_its_own_no_progress_budget…`, `…never_touches_the_shared_semantic_cache`, `…never_pays_for_a_discarded_suggest_followups_call`), `TestBuildSubagentGraphTopology` |
| FR-018 answer scrubbed; budget message | `TestRunSubagentImpl` (`…delegates_and_returns_the_final_answer`, `…an_uncited_answer_gets_auto_corrected…`, `…giving_up_on_a_stuck_retry_loop…`) |
| FR-019 spend folded and reset; ledger row | `test_safety_budgets.py::TestSubagentSpendBudget`, `…subagent_spend_recorded_in_one_turn_does_not_survive_into_the_next`, `…parallel_subagent_entries_within_one_turn_still_accumulate`; `TestRunSubagentTool::test_invoke_returns_a_command_with_the_answer_and_its_spend`; `TestRunSubagentImpl::test_records_usage_to_the_ledger_with_a_derived_thread_id` |
| FR-020 outcome metric on timeout | `TestRunSubagentImpl::test_timeout_raises_and_is_recorded` (**counter only — not the ledger, B14**) |
| FR-021 stream isolation, tagged activity | `test_streaming_terminal_events.py::TestSubagentEventsDontLeakIntoTheMainStream`; `test_nodes.py::…emit_message_false_skips_everything_for_the_nested_subagent_case` |
| FR-022 concurrency; graph reuse | `test_concurrent_turns.py::TestSubagentCallUnderConcurrency`; `test_tools.py::TestSubagentGraphCache` |

**Not covered here**: FR-023/SC-006 (B13), FR-024/SC-007 (B14), FR-025/SC-002 (B15), FR-026/SC-009 (B16), FR-027 (A1), FR-028/SC-010 (A3, A4, A7).

---

## Tier 2 — Real services

There is **no** integration-tier test for this feature (`make test-integration` has no skill or subagent path). The real skills index is exercised only by the live tier below.

## Tier 2b — Live (Docker + a local model; slow)

```bash
make test-live      # or only these two:  pytest -m "llm or e2e or crawl" tests/live/test_chat_ui.py -k "skill or subagent" -q
```

`tests/live/test_chat_ui.py::test_a_skill_is_found_and_followed` and `::test_a_subagent_delegates_and_returns_a_real_answer` start the API and a worker **as host subprocesses from the
repository root**, build the skills index with `scripts.index_skills`, and drive a real browser against the real small model. They prove SC-001 live. **They cannot see B16**, by construction.

---

## Scenario B13 — Reproduce: the cached nested graph retains every run (hermetic; expected: it reproduces)

In a **temporary** file under `tests/agent/` (so the autouse mocks apply; do not commit; delete after) — a plain script hits live services:

1. `from tests.agent.test_tools import _fake_subagent_record, _subagent_cfg`; call `subagent_tools.reset_subagent_graph_cache()`.
2. `registry = {"researcher": (_fake_subagent_record(), ())}`; define a **reusable** fake model class whose `async ainvoke(self, messages, *a, **kw)` always returns `AIMessage(content="An answer long enough to pass.")`.
   (It must be reusable: the cache pins the first run's model client, so a one-shot fake is exhausted on run 2.)
3. Loop 100 times: `await _run_subagent_impl("researcher", f"task {i}", _subagent_cfg(), registry=registry, llm=<fake>, use_cache=True)`; after runs 1, 10, 50 and 100 record
   `len(subagent_tools._subagent_graph_cache[("ecorp", "researcher")].checkpointer.storage)`.

**Observed 2026-10-02**: `{1: 1, 10: 10, 50: 50, 100: 100}`. **Fixed when** it stays at 0 (or the run's own thread only while it is running). This is the failing test the B13 fix starts with.

## Scenario B14 — Reproduce: a timed-out run records no usage (hermetic; expected: it reproduces)

Same temporary-file rule.

1. Monkeypatch `usage_ledger.record_usage` with a coroutine that appends its `total_tokens` argument to a list.
2. A fake model: its first `ainvoke` returns `AIMessage(content="", tool_calls=[{"name": "calculator", "args": {"expression": "1+1"}, "id": "c1"}], usage_metadata={"input_tokens": 400, "output_tokens": 100, "total_tokens": 500})`;
   every later call `await asyncio.sleep(5)`.
3. `monkeypatch.setattr(subagent_tools, "SUBAGENT_TIMEOUT_SECONDS", 0.3)`; registry `{"researcher": (_fake_subagent_record(), ("calculator",))}`;
   `with pytest.raises(TimeoutError): await _run_subagent_impl("researcher", "task", _subagent_cfg(), registry=registry, llm=<fake>)`.
4. Print the list.

**Observed 2026-10-02**: `[]` — the call raised `TimeoutError` and the ledger recorder was never called, though 500 tokens were spent. **Fixed when** the list is `[500]`. This is the failing test the B14 fix starts with.

## Scenario B15 — Reproduce: two Ecorp-only skills are offered everywhere (hermetic; expected: it reproduces)

Read-only; no services. In a Python session:

1. `from app.agent import skills as sk; from app.agent.tools import make_skill_tools, _filter_skill_hits_by_domain` and a tiny `Hit` class with `.payload = {"name": <name>}`.
2. For each of `ecorp`, `support`, `ops`, `sales`: `_filter_skill_hits_by_domain([Hit(n) for n in sk.get_skills()], <product>, sk.get_skills())` and list the names.
3. For `support`: `await make_skill_tools("support")[1].ainvoke({"name": "onboarding-brief"})`.
4. From `app.domains.registry.DOMAINS[<product>][1].tools()` list the tool names for support, ops and sales and look for `query_employees` and `calculator`.

**Observed 2026-10-02**: every product lists `expense-summary` and `onboarding-brief` (plus its own tagged skills); the support call returns the full onboarding instructions; none of the three non-Ecorp products has
`query_employees` or `calculator`. **Fixed when** both skills are visible to `ecorp` only.

## Scenario B16 — Inspect: the image contains no catalogs (read-only; needs Docker and a built image; expected: it shows the defect)

```bash
docker run --rm --entrypoint sh agent-core-demo-api:latest -c '
  ls /app
  ls -d /app/skills /app/subagents /app/scripts
  python -c "from app.agent import skills, subagents; import app.agent.tools as t; \
print(len(skills.get_skills()), len(subagents.get_subagents()), any(x.name == \"run_subagent\" for x in t.TOOLS))"'
```

(Build the image first with `docker build -t agent-core-demo-api:latest .` if you do not have one; the compose services all use the same Dockerfile.)

**Observed 2026-10-02** against the existing local image (created after the Dockerfile's last change): `/app` holds only `app` and `requirements-lock.txt`; the three `ls -d` targets report "No such file or directory";
the Python line prints `0 0 False`. **Fixed when** it prints the shipped counts (8 skills, 5 subagents) and `True`.

---

## Tier 3 — Full local stack, manual walk-through (host-native)

1. `make up`, `make pull-models`, `make restart-all` (this also runs `make index-skills`).
2. In the web UI, ask: *"put together an onboarding brief for a new hire in Engineering"*. **Expected**: the tool trace shows `skill_search` → `use_skill("onboarding-brief")` → `query_employees`/`search_docs`.
3. Ask: *"who's the most senior person in Engineering, and what's their start date?"* **Expected**: a `run_subagent` call to `researcher`; the trace shows the specialist's own tool activity tagged with its name; **no approval prompt** appears.
4. Switch the product (`X-Domain: support`) and ask the same onboarding question. **Expected (intended)**: no skill is found. **As built**: `skill_search` returns `onboarding-brief` (B15).
5. Add a new `skills/<x>/SKILL.md`, run `make index-skills`, ask a matching question **without restarting**. **As built**: it is not found (A3); restart the API and worker, and it is.

## Checking the alerts

There are **no** Prometheus alert rules for these metrics, deliberately in part: a specialist is read-only, so no committed business state can be left unknown. The gaps are in the *metrics*, not the alerts —
nothing counts a missing skills index (A2), a dropped specialist tool (A4) or an empty catalog at start (B16).

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `skill_search` says "No skills catalog is available right now" | The `skills` collection does not exist — run `make index-skills` (host-native only; B16/A2 for containers) |
| `skill_search` finds nothing in a container, and there is no `run_subagent` tool | The image has no `skills/` or `subagents/` (B16) |
| A new skill is indexed but never offered | The running process cached the old catalog (A3) — restart it |
| A new specialist is missing from the menu | The enum is built at import — restart (disclosed in pattern 46) |
| A skill is invisible in every product | Check its `domains:` spelling against `app/domains/registry.py` (A4) |
| A delegation returns "did not produce a final answer…" | It hit its 6-step / 4000-token / cost ceiling (`budget_exceeded`) — make the task narrower |
| A delegation fails with a timeout | 45 s was not enough on this model; the tokens it used are **not** in the ledger (B14) |
| Worker memory climbs slowly under heavy delegation | B13 |

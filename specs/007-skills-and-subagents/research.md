# Research: Skills and Subagents

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-10-02

**Status**: Retrospective — decisions reconstructed from the code, its comments, the config comments and `GRAPH_PATTERNS.md` patterns 45, 46 and 50. Each entry
names its evidence. **No `NEEDS CLARIFICATION` remains.** R21–R25 (Part C) are *findings* from verifying the as-built system, not decisions anyone made.

Format: **Decision** · **Rationale** · **Alternatives considered** · **Evidence**. *Alternatives are those the code or its docs name or argue against; where none is
recorded the entry says so rather than inventing one.*

---

## Part A — Skills

### R1. Disk is the truth; the search index holds only a lookup key

- **Decision**: `use_skill` reads the body from the on-disk catalog by exact name. The `skills` collection holds `{text, name, description}` — enough for `skill_search` to
  find a name — and never the body.
- **Rationale**: Two systems for one fact drift. Keeping "searchable" and "authoritative" apart means a skill's instructions can never disagree with what is on disk.
- **Alternatives considered**: storing the body in the index (rejected for the drift above; also grows every hit).
- **Evidence**: `app/agent/skills.py` module docstring; `tests/agent/test_tools.py::TestUseSkill::test_returns_the_matched_skills_full_body_from_disk_not_qdrant`.

### R2. Progressive disclosure: a two-tool pair, not every skill bound every turn

- **Decision**: Bind only `skill_search` and `use_skill`; a turn that needs no skill pays for two tool descriptions. The catalog can grow without growing what is bound to any turn.
- **Rationale**: Middle ground between "bind every capability's full instructions every turn" and "hand-pick a fixed subset per deployment".
- **Alternatives considered**: both of those, named in pattern 45.
- **Evidence**: `GRAPH_PATTERNS.md` pattern 45; `skills.py` docstring.

### R3. Visibility is filtered in Python on the disk catalog, in both tools, after over-fetching

- **Decision**: `skill_search` fetches `max(4 × top-k, 10)` hits, keeps those whose name is in the disk catalog **and** visible to the product, then truncates to top-k. `use_skill` applies
  the same visibility test on an exact-name load.
- **Rationale**: Product eligibility is a fact about the file; copying it into the index would be a second place to drift. Over-fetching keeps results correct when the best semantic matches
  belong to another product. Filtering in `use_skill` too covers a model that guesses a foreign skill's name. A hit whose name is no longer on disk is dropped for the same reason.
- **Alternatives considered**: a Qdrant payload filter on the product (rejected: second source of truth).
- **Evidence**: `make_skill_tools` docstring; `TestFilterSkillHitsByDomain`, `TestMakeSkillTools` (`…hides_a_skill_tagged_to_another_domain`, `…refuses_a_skill_tagged_to_another_domain_even_by_exact_name`,
  `…untagged_skill_is_reachable_from_every_domain`).

### R4. Top-k defaults to 1

- **Decision**: `SKILLS_SEARCH_TOP_K = 1`.
- **Rationale**: Measured live on the reference small model: even one distractor beside the right match made it hedge into "none of these are suitable"; k=2 and k=3 reproduced the failure, k=1 passed
  reliably. It trades a second chance for a confident commitment.
- **Evidence**: the comment on `skills_search_top_k` in `app/core/config.py`; `tests/live/test_chat_ui.py::test_a_skill_is_found_and_followed`.

### R5. The skill tools lead the bound tool list

- **Decision**: `skill_search`/`use_skill` are first in every product's bound list (`skill_tools_first`) and in Ecorp's `TOOLS`; `run_subagent` joins the same leading tier.
- **Rationale**: A live-verified fix: a sales deal-math question never called `skill_search` across many runs despite three prompt and docstring rewrites; only swapping the *position* made the
  model call `use_skill` first, 3 of 3 fresh runs. For a small grammar-constrained model, list position matters. The `run_subagent` placement is a **reasoned extension, not independently
  live-verified** — the code says so.
- **Evidence**: `skill_tools_first` docstring; `TestSkillToolsFirst`; `.claude/rules/runtime-reliability.md` ("don't reorder the bound tool list").

### R6. `use_skill` without a prior `skill_search` is rejected, not run

- **Decision**: A node rejects such a batch with one tool message per call ("call `skill_search` now… don't guess a name"), increments `agent_use_skill_without_search_total`, and loops back; it never pauses.
- **Rationale**: Without it the model guessed a name, got "not found" and narrated the failure into the final answer. Same reject-and-retry shape as `invalid_tool_call`; one tool message per pending call
  is mandatory or the next LLM call fails.
- **Evidence**: `app/agent/graph_skills.py`; `TestUseSkillCalledWithoutSearch`, `TestShouldContinueUseSkillWithoutSearch`, `TestUseSkillWithoutSearchGate` (rejected, then retried without pausing; a real search first dispatches normally).

### R7. Required-tool detection is a crude, explicit allowlist

- **Decision**: `_SKILL_REQUIRED_TOOL_MARKERS = ("run_python_in_sandbox",)`, matched as a substring of the skill text loaded this turn; if the tool has not been called, the agent is reminded
  proactively and a final answer that skipped it is rejected reactively.
- **Rationale**: A real bug found live through tracing: a skill whose own text said "don't estimate this by hand" was still ignored. `run_command_in_sandbox` was deliberately *excluded* because skills
  mentioned it as a negative example and the substring check could not tell, producing false "you skipped this" corrections. An allowlist, not a generic scanner, because most skills need no tool.
- **Alternatives considered**: intent parsing (rejected as larger than the problem).
- **Evidence**: `graph_skills.py` comments; `TestSkippedRequiredSandboxAfterSkill`; `test_agent_appends_a_sandbox_reminder_after_a_sandbox_requiring_skill_loads`;
  `test_agent_skips_the_sandbox_reminder_when_no_skill_mentions_the_tool`; `test_retry_output_tells_the_model_the_skill_named_a_required_tool`; `agent_skipped_required_tool_total`.

### R8. Skills are bundled capabilities, not tenant data

- **Decision**: No identity context in either skill tool; bodies are repository content.
- **Rationale**: Like the calculator, a skill is part of the app. A tenant-authored catalog is a larger extension with a different trust model, explicitly not attempted.
- **Evidence**: pattern 45's "Deliberate scope boundary"; `TestSkillSearch::test_needs_no_ctx_it_is_a_bundled_capability_not_tenant_data` and the matching `TestUseSkill` test.

---

## Part B — Subagents

### R9. Delegation is a real nested run, not more instructions

- **Decision**: `run_subagent` starts a separate `ainvoke` of `build_subagent_graph()` with its own messages, prompt, tools, model alias and budget — not additional text in the parent's context.
- **Rationale**: Keeps a multi-step lookup's intermediate steps out of the main conversation and lets a specialist have a focused prompt. (Skills are the opposite: more instructions, same context.)
- **Evidence**: `_run_subagent_impl` docstring; pattern 46; `TestRunSubagentImpl::test_task_is_the_subagents_sole_human_message_not_parent_history`.

### R10. Subagents may only use read-only tools, enforced at catalog-build time

- **Decision**: `_resolve_subagent_tools` keeps only declared tools that exist **and** are `read_only`; the rest are dropped with a warning, never upgraded. `run_subagent` is therefore a plain static `read_only` tool
  needing no special case in the approval gate.
- **Rationale**: The nested graph runs synchronously on an in-process store inside one outer tool call; nothing can resume *that* run hours later. A write-capable specialist would mean either an unresumable
  mandatory gate (a deadlock) or a bypass of the "no flag turns this off" rule. Read-only sidesteps the question. This is also constitution Principle II's explicit rule.
- **Alternatives considered**: letting specialists write behind a gate at the spawn boundary (rejected, above).
- **Evidence**: pattern 46; `TestResolveSubagentTools`; `TestShouldContinueSubagent`; constitution Principle II.

### R11. Recursion is blocked unconditionally

- **Decision**: `run_subagent` is stripped from every resolved tool set, whatever the file declares.
- **Rationale**: It is itself declared `read_only`, so read-only filtering alone would not exclude it; it needs its own check.
- **Evidence**: `_resolve_subagent_tools` (`if name == "run_subagent": continue`); `test_strips_run_subagent_even_if_explicitly_declared`.

### R12. Opposite defaults for an absent `domains` tag

- **Decision**: An untagged **skill** is visible to every product; an untagged **subagent** is declared only for Ecorp.
- **Rationale**: A skill's instructions are mostly tool-agnostic prose and the default predates tags. A specialist's declared `tools:` only make sense against one tool universe; one exposed to every product
  would mostly resolve to nothing useful.
- **Consequence recorded as B15**: the skill default is right for a generic skill but two shipped skills are Ecorp-specific and were never tagged.
- **Evidence**: `subagents.py` and `_subagent_declared_for_domain` docstrings; `TestSubagentDeclaredForDomain`.

### R13. A throwaway plugin lists exactly the resolved tools

- **Decision**: `_SubagentDomainPlugin` returns the already-narrowed list from `tools()` and reports each as `read_only`, instead of using a manifest `allowed_tools` filter.
- **Rationale**: `allowed_tools` treats an empty tuple as "no filter — expose everything", so a specialist legitimately left with zero tools would silently regain the full Ecorp set including the write tools —
  privilege escalation by omission. And an undeclared name defaults to `outward` in the top-level capability lookup, which in a nested run with no resume path would look like a silent failure.
- **Evidence**: `_SubagentDomainPlugin` docstring; `test_empty_declared_list_resolves_to_empty_not_everything`; `TestSubagentDomainPluginCapabilityFix`.

### R14. Budgets: loop counts are constants, dollars and time are settings

- **Decision**: `MAX_SUBAGENT_ITERATIONS = 6` and `MAX_SUBAGENT_TOKENS_PER_RUN = 4000` are constants in `graph.py`; `MAX_SUBAGENT_COST_USD_PER_RUN` and `SUBAGENT_TIMEOUT_SECONDS` are `Settings`. The graph-step limit is derived
  (`MAX_SUBAGENT_ITERATIONS * 2 + 15`).
- **Rationale**: A loop count is a safety net; a dollar ceiling is a policy knob. The timeout was moved to a setting after a real CI failure on a slow backend. The graph-step limit is coarser than the agent-step
  cap (pre/post nodes and two-step tool round trips), so a flat parent value tripped `GraphRecursionError` early.
- **Evidence**: comments at `graph.py` (the constants), `config.py` (the two settings), `_run_subagent_impl` (`recursion_limit`); `test_respects_its_own_smaller_iteration_ceiling_not_the_parents`.

### R15. A dedicated, leaner graph that shares the main graph's nodes

- **Decision**: `build_subagent_graph()` reuses the main graph's node functions through `_assemble_shared_graph_parts`, dropping five of 21 nodes — `check_semantic_cache`, `write_semantic_cache`, `suggest_followups`, `compact_history` and
  `context_window_exceeded` — each either a cross-talk risk or unreachable under the smaller budget. It keeps `human_approval` (unreachable here, because every resolved tool is read-only) and `use_skill_without_search`.
- **Rationale**: One implementation, so a fix to a shared node reaches both loops; a subagent's answer must never cross-serve a top-level query via the cache.
- **Evidence**: `TestBuildSubagentGraphTopology` (`…drops_exactly_the_five_overhead_nodes`, `…keeps_every_safety_and_agent_loop_node`), `test_never_touches_the_shared_semantic_cache`, `test_never_pays_for_a_discarded_suggest_followups_call`;
  `.claude/rules/runtime-reliability.md` ("a new node must be considered for both").

### R16. Compile once per (product, specialist); each run is still isolated

- **Decision**: `_subagent_graph_cache` stores the compiled graph and bound client; `use_cache=True` is passed only by the two real `run_subagent` closures, never inferred, so tests are automatically excluded. Each run gets a
  unique `nested_thread_id`.
- **Rationale**: Topology, model and tools are static per (product, specialist), so a `StateGraph` compile and a new client per call was waste. Inferring cache use from "were DI arguments passed" would have silently
  defeated caching for every non-Ecorp product.
- **Consequence recorded as B13**: the unique id that isolates runs is never paired with a delete, and the cached graph's store outlives every run.
- **Evidence**: `_run_subagent_impl` docstring; `TestSubagentGraphCache` (`…reuses_the_compiled_graph_across_calls`, `…different_domains_get_independent_cache_entries`, `…without_use_cache_the_graph_cache_is_never_touched`).

### R17. Spend folds into the parent through a concurrency-safe reducer that resets each turn

- **Decision**: The tool returns a `Command` carrying the answer message and a `(tokens, cost)` entry appended to `subagent_spend`; the reducer concatenates and treats `None` as an explicit reset that `validate_input` applies
  every turn.
- **Rationale**: Parallel delegations in one turn would race a read-modify-write on a plain total. A plain concatenating reducer made the per-turn reset a no-op, so earlier turns' spend counted against later ones (pattern 10).
- **Evidence**: pattern 46; `TestSubagentSpendBudget`; `test_subagent_spend_recorded_in_one_turn_does_not_survive_into_the_next`; `test_parallel_subagent_entries_within_one_turn_still_accumulate`;
  `test_invoke_returns_a_command_with_the_answer_and_its_spend`.

### R18. Tracing and stream isolation by threading callbacks and tagging metadata

- **Decision**: The parent's callbacks go into the nested config so its LLM and tool spans nest; metadata `{subagent_name, parent_thread_id, domain}` tags every nested event; the client stream ignores nested "agent" tokens
  (both graphs share that node name) but surfaces tagged tool activity.
- **Evidence**: `_run_subagent_impl` comments; `TestSubagentEventsDontLeakIntoTheMainStream`; `test_emit_message_false_skips_everything_for_the_nested_subagent_case`.

### R19. The subagent menu is a closed enum built at import

- **Decision**: `SubagentName` (and a per-product enum) is built from the registry when the module is first imported; the full menu is embedded in the schema, with no separate list tool.
- **Rationale**: A compile-time enum must exist before a call can validate; a one-line description is small enough to embed (unlike a skill body). **Disclosed cost**: a new subagent needs a restart; `reload_subagents()` cannot rebuild the enum.
- **Evidence**: `subagent_tools.py` comments; `TestRunSubagentTool::test_schema_lists_available_subagents_by_name_and_description`; `TestMakeDomainSubagentTool::test_two_domains_get_two_independent_enum_types`.

### R20. The nested prompt forbids `[n]` citation markers

- **Decision**: `_CITATION_MARKER_WARNING` is appended to every specialist's prompt.
- **Rationale**: The parent's grounding check cross-checks `[n]` markers against its own citations, which a specialist's retrieval never populates; without the notice a grounded specialist answer could be flagged ungrounded
  once folded into the reply.
- **Evidence**: the comment on `_CITATION_MARKER_WARNING`; `test_nested_system_prompt_has_its_own_prompt_and_the_citation_warning`.

---

## Part C — Findings (not decisions)

### R21. FINDING B13 — the cached nested graph never forgets a run

- **Observation**: `build_subagent_graph` compiles with `checkpointer or MemorySaver()`; `_run_subagent_impl` passes none, so each cached graph owns one in-process store. Every call uses `thread_id = <parent>:subagent:<name>:<uuid8>`.
  Nothing deletes a thread.
- **Reproduction** (temporary harness under `tests/agent/` so the autouse mocks apply; removed after): `reset_subagent_graph_cache()`, a registry entry for one specialist with no tools, a reusable fake model that always
  answers, and 100 calls to `_run_subagent_impl(..., use_cache=True)`; after each, `len(graph.checkpointer.storage)` for `_subagent_graph_cache[("ecorp", "researcher")]`.
  **Observed 2026-10-02**: `{1: 1, 10: 10, 50: 50, 100: 100}`. (The reusable fake is a harness detail: the cache pins the *first* run's model client.)
- **Consequence**: memory in a long-lived worker grows with delegations × conversation size, forever; the comments and pattern 46 still say "ephemeral/throwaway".
- **Verified for the fix**: the installed `MemorySaver` (langgraph-checkpoint 2.1.2) has both `delete_thread` and `adelete_thread`.

### R22. FINDING B14 — a failed run's tokens vanish

- **Observation**: in `_run_subagent_impl` the `await record_usage(...)` follows the `try`/`except` that re-raises on `TimeoutError` and on any other exception; the `except` blocks only update the outcome counter and histogram.
  The tool raises (it does not return a `Command`), so no `subagent_spend` entry reaches the parent either.
- **Reproduction**: a model that returned one step with `usage_metadata.total_tokens = 500` and a tool call, then slept past a shortened `SUBAGENT_TIMEOUT_SECONDS` (0.3 s), with `usage_ledger.record_usage` replaced by a recorder.
  **Observed 2026-10-02**: `TimeoutError` raised; the recorder was called **0** times. The existing `test_timeout_raises_and_is_recorded` asserts only the outcome counter.
- **Consequence**: a specialist that routinely times out burns real tokens that neither the tenant's daily budget nor any dashboard sees. The `error` path is the same code shape (read, not separately run).

### R23. FINDING B15 — two Ecorp-only skills are offered in every product

- **Observation**: of 8 shipped skills, 6 carry `domains:` and 2 do not: `onboarding-brief` (steps use `query_employees`, `search_docs`) and `expense-summary` (steps use `calculator`).
- **Reproduction** (read-only script over the real folders): visible set per product — ecorp: `expense-summary`, `onboarding-brief`; support: those two plus `support-log-triage`, `support-tier1-triage`; ops: those two plus
  `ops-incident-response`, `vendor-incident-postmortem`; sales: those two plus `deal-economics`, `sales-lead-qualification`. Each non-Ecorp product's resolved tool list was checked: **none** contains `query_employees` or `calculator`.
  `make_skill_tools("support")`'s `use_skill("onboarding-brief")` returned the full body.
- **A naive-scan caveat (feeds A7's test design)**: a generalized "every backticked tool name in a visible skill must exist in the product" scan also flagged `deal-economics` (sales) for `calculator` — which that skill names
  only to say it *cannot* do the job. A pinning test needs an explicit, reviewed exception list, not a bare name match (the same false-positive class as A5).
- **Consequence**: the support product can load a skill telling it to call a tool it does not have; the invalid-tool-call guard then rejects the call (bounded, not unsafe), but the turn is wasted and the model is misled.

### R24. FINDING B16 — the images ship no catalogs

- **Observation**: `Dockerfile` has exactly two `COPY` instructions — `requirements-lock.txt` and `app/ ./app/`; `docker-compose.yml`'s `api`, `agent-worker*` and `ingest-worker` services are `build: .` with no volume for `skills/` or `subagents/`;
  `docker-compose.prod.yml` runs `${APP_IMAGE}`; `.github/workflows/deploy.yml` builds the app image from the same `Dockerfile` and context; `.env.example` has no `SKILLS_DIR`/`SUBAGENTS_DIR`.
- **Inspection** (read-only; against the locally built `agent-core-demo-api:latest`, created after the Dockerfile's last change on 2026-09-19): `ls /app` → `app`, `requirements-lock.txt`; `ls -d /app/skills /app/subagents /app/scripts` →
  all three "No such file"; a Python one-liner printed `skills_dir skills catalog 0`, `subagents_dir subagents catalog 0`, and `run_subagent in TOOLS: False`.
- **Why nothing noticed**: `tests/live/conftest.py` starts `uvicorn` as a subprocess with `cwd` = the repository root and runs `scripts.index_skills` by module; CI's `docker-build` job runs `docker build` and nothing else.
- **Consequence**: the feature is absent, silently, in every containerized deployment; `scripts/` (so `index_skills` and `seed`) is also absent, which is A2's other half.
- **History**: the Dockerfile was added 2026-08-27; skills and subagents landed 2026-08-29; `git log -S` finds no `COPY skills`, `COPY subagents` or `COPY scripts` in its history — it has never shipped them.
- **Not verified**: that the production image *content* matches the local one (the release workflow uses the same Dockerfile, which is the basis for expecting it).

### R25. FINDINGS A1–A8

- **A1**: `SkillSearchArgs.query`, `UseSkillArgs.name`, `RunSubagentArgs.task` are bare `Field(...)`s (the last with a `_not_blank` validator); `AddNoteArgs` and `RememberArgs` use `max_length`.
- **A2**: `index-skills` is called from the `restart-all` recipe and nowhere else (Makefile, both compose files, workflows); `scripts/index_skills.py` builds point ids with `uuid.uuid4()` and calls `ensure_collection`
  (delete + create) first — which is *why* it recreates rather than upserts. The degrade branch in `_skill_search_impl` logs `skill search unavailable` and returns a message; no counter exists in `metrics.py` for it.
- **A3**: callers of `reload_skills`/`reload_subagents`/`reset_subagent_graph_cache` in `app/` and `scripts/`: only `index_skills.py` (its own process).
- **A4**: neither loader validates a tag, tool or alias against anything; `_resolve_subagent_tools` logs `subagent declares an unknown tool; dropping it` and counts nothing; `ChatOpenAI(model=record.model or CHAT_MODEL)` is built without a check;
  `litellm-config.yaml` serves `chat`, `chat-backup`, `vision`, `embed`.
- **A5**: no shipped `SKILL.md` contains `run_command_in_sandbox`; the name appears in app code and as sample text in a few node tests; no test exercises `_use_skill_impl`'s reminder branch.
- **A6**: `run_subagent` returns `ToolMessage(content=result.answer)`; the parent's retrieved-document framing is applied to pre-fetched context in `graph_agent_node.py`, not to tool messages.
- **A7**: no test in `tests/` calls `get_skills()`, `load_skills()`, `get_subagents()` or `load_subagents()` against the real folders; each product's domain test asserts its own specialist menu.
- **A8**: `grep -i 'skill\|subagent' .env.example` is empty; the `max_subagent_cost_usd_per_run` comment in `config.py` says spend is "NOT folded back into the parent turn's live total_cost_usd"; comments in `config.py` and `graph.py` cite `tools.py::run_subagent`.

---

## Deferred / unbuilt (carried to `tasks.md`)

| Id | Item | Why deferred |
|----|------|--------------|
| B13 | Delete a run's thread when it ends (or compile without a store, if provably unneeded); update the "ephemeral" wording | Test first; small. Order after B14's design because B14 wants the partial state first |
| B14 | Record a failed run's tokens; decide whether the tool returns spend instead of raising | Test first; one design decision |
| B15 | Tag the two skills `[ecorp]`; the pinning test | Test first; trivial |
| B16 | `COPY` both folders; image smoke in CI; empty-catalog log + metric | Test first (Dockerfile lines); decide whether `scripts/` joins the image |
| A1 | Length bounds on `query`, `name`, `task` | Small |
| A2 | Bootstrap step; metric on the degrade; deterministic ids + create-if-missing | One decision: how a deployment runs it |
| A3 | Make a running process see catalog edits, or document the restart | Decision: reload vs restart |
| A4 | Validate tags, tools, aliases in the pinning test | Pairs with A7 |
| A5 | One allowlist; remove or test the second branch | Small |
| A6 | Decide: frame a specialist's answer or record the choice | Decision |
| A7 | The catalog-pinning tests | Pairs with B15/B16 |
| A8 | `.env.example`, comments, Roadmap | Docs |
| — | Tenant-authored skills, writable or nested subagents, skill versioning | Out of scope |

# Implementation Plan: Skills and Subagents

**Branch**: `007-skills-and-subagents` | **Date**: 2026-10-02 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/007-skills-and-subagents/spec.md`

**Status**: Retrospective — describes the as-built implementation. Every path below exists today.

## Summary

Two bundled, file-based catalogs and the two tool pairs that expose them.

**Skills** (`app/agent/skills.py`, pattern 45). One directory per skill under `skills/`, each a `SKILL.md` (YAML header + markdown body). The
loader skips a malformed file and keeps the first of a duplicate name; the catalog is a lazy process-wide singleton. `scripts/index_skills.py`
embeds only `name: description` into a dedicated `skills` Qdrant collection (recreating it). `make_skill_tools(domain)` in `app/agent/tools.py`
builds a `(skill_search, use_skill)` pair per product: `skill_search` over-fetches from the index, filters on the **disk** catalog by product tag
and truncates to the top-k (default 1); `use_skill` loads the body from disk by exact name and applies the same visibility filter. Two guardrails in
`app/agent/graph_skills.py` — reject `use_skill` without a prior `skill_search` this turn, and remind/correct when a loaded skill names a required
tool — and the leading position of both tools in the bound list (`skill_tools_first`, pattern 50) complete it.

**Subagents** (`app/agent/subagents.py`, `subagent_tools.py`, `subagent_domain_tools.py`, pattern 46). One directory per specialist under
`subagents/`, each an `AGENT.md`. `_build_subagent_registry` resolves, per product, every declared specialist to a **read-only-only** tool set
(`_resolve_subagent_tools`: drop unknown and non-read-only names, always strip `run_subagent`, an empty result stays empty).
`run_subagent` (one Ecorp object; one closure per other product via `make_domain_subagent_tool`) takes a closed-enum name and a task, and
`_run_subagent_impl` runs a fresh `build_subagent_graph()` — the same node functions as the main graph in a leaner one-shot topology — with the
specialist's prompt and the task as its only messages, the caller's `SecurityCtx`, its own budgets (6 steps, 4000 tokens, $0.15, 45 s) and an
in-process checkpoint store. The compiled graph is cached per `(product, specialist)`; each run uses a unique nested thread id. The answer is
scrubbed and returned as a `Command` carrying the tool message and a `subagent_spend` entry the parent folds into its turn budget; usage is recorded
to the ledger; a counter and a histogram record outcome and duration; callbacks and tagged metadata thread through for tracing and stream isolation.

The plan records honestly that four defects — **B13** (the cached graph's store never frees a run), **B14** (a timed-out or failed run's spend is lost),
**B15** (two Ecorp-only skills are offered everywhere) and **B16** (the container images ship neither catalog) — and eight smaller gaps sit around a
design whose *safety* property — a specialist can only ever read — holds structurally and is well tested.

## Technical Context

**Language/Version**: Python 3.13

**Primary Dependencies**: `langgraph` 0.2.76 (`StateGraph`, `MemorySaver`, `Command`), `langchain-core` (`@tool`, `InjectedToolCallId`),
`langchain-openai` (`ChatOpenAI` through the LiteLLM proxy), `pydantic` (args schemas, closed `Enum`s), `pyyaml` (`safe_load` of the header),
`qdrant-client` 1.19.0 + `fastembed` (the skills collection's hybrid search; shared with feature 001).

**Storage**: Disk — `skills/*/SKILL.md` (8 shipped: 6 tagged to one product, 2 untagged) and `subagents/*/AGENT.md` (5 shipped: support 1, sales 1, ops 2, and the untagged
Ecorp-only `researcher`); Qdrant — the `skills` collection (`{text, name, description}`, hybrid dense+sparse);
Postgres — one usage-ledger row per run (feature 008); in-process — the compiled-graph cache and each graph's `MemorySaver`. No new tables.

**Testing**: pytest hermetic tier — `tests/agent/test_skills.py`, `test_subagents.py` (loaders), `test_tools.py` (`TestSkillSearch`, `TestUseSkill`,
`TestSkillVisibleToDomain`, `TestFilterSkillHitsByDomain`, `TestMakeSkillTools`, `TestSkillToolsFirst`, `TestResolveSubagentTools`,
`TestBuildSubagentGraphTopology`, `TestRunSubagentImpl`, `TestSubagentGraphCache`, `TestRunSubagentTool`, `TestSubagentDeclaredForDomain`,
`TestBuildSubagentRegistryDomainFilter`, `TestSubagentDomainPluginCapabilityFix`, `TestMakeDomainSubagentTool`), `test_routing.py`
(`TestShouldContinueSubagent`, `TestUseSkillCalledWithoutSearch`, `TestShouldContinueUseSkillWithoutSearch`), `test_nodes.py`
(`TestSkippedRequiredSandboxAfterSkill`), `test_agent_node.py`, `test_graph_integration.py` (`TestUseSkillWithoutSearchGate`),
`test_concurrent_turns.py` (`TestSubagentCallUnderConcurrency`), `test_safety_budgets.py` (`TestSubagentSpendBudget`),
`test_streaming_terminal_events.py` (`TestSubagentEventsDontLeakIntoTheMainStream`) and each product's `TestDomainScopedSubagent`. Live tier —
`tests/live/test_chat_ui.py::test_a_skill_is_found_and_followed`, `::test_a_subagent_delegates_and_returns_a_real_answer`. **Not tested**: the real
`skills/` folder, the shipped catalogs' tags and tool references, the container images, the cache's memory behavior, the failure path's accounting.

**Target Platform**: Linux containers and host-native processes (**the catalogs exist only host-native — B16**).

**Project Type**: Library modules + two LLM-facing tool pairs + a small indexing script.

**Performance Goals**: None asserted. The default top-k of 1 and the leading tool order were chosen from live runs on the small local model.

**Constraints**: `MAX_SUBAGENT_ITERATIONS = 6` and `MAX_SUBAGENT_TOKENS_PER_RUN = 4000` (constants in `graph.py`, deliberately not settings);
`MAX_SUBAGENT_COST_USD_PER_RUN = 0.15`, `SUBAGENT_TIMEOUT_SECONDS = 45`, `SKILLS_SEARCH_TOP_K = 1`, `SKILLS_DIR = skills`, `SUBAGENTS_DIR = subagents`,
`SKILLS_COLLECTION = skills` (settings; **none in `.env.example` — A8**); graph-step limit `MAX_SUBAGENT_ITERATIONS * 2 + 15`.

**Scale/Scope**: 2 catalogs, 2 tool pairs, 1 index, 1 nested-graph topology (5 of the main graph's 21 nodes dropped), 4 products.

**Unknowns**: none — every value is read from the repository, the tests or the built image.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design (end of section).*

| # | Principle | Touched? | Verdict | Evidence / gap |
|---|-----------|----------|---------|----------------|
| I | Fail-closed tenant isolation (NN) | Yes — delegation | **PASS for isolation; B14 is an *accounting* defect** | `_run_subagent_impl` refuses without a valid ctx (`test_refuses_without_ctx`) and hands the caller's ctx to the nested run unchanged, so every tool the specialist calls is scoped exactly as the parent's. The nested run never touches the shared semantic cache (`test_never_touches_the_shared_semantic_cache`). Skills carry no tenant data by design (`test_needs_no_ctx_it_is_a_bundled_capability_not_tenant_data`). Concurrent delegations never cross-wire (`TestSubagentCallUnderConcurrency`). **B14**: a run that times out or errors records no usage for its tenant. |
| II | Mandatory human approval (NN) | **Primary** | **PASS (structural)** | A specialist's tools are resolved at catalog-build time to **read-only only**: unknown and non-read-only names dropped, `run_subagent` always stripped, an empty set stays empty (`TestResolveSubagentTools`: `…drops_a_declared_mutating_tool`, `…strips_run_subagent_even_if_explicitly_declared`, `…empty_declared_list_resolves_to_empty_not_everything`). The nested plugin declares every resolved tool `read_only` by construction (`TestSubagentDomainPluginCapabilityFix`). `run_subagent` is a static `read_only` tool routed straight to execution with the approval flag set or not (`TestShouldContinueSubagent`). **Residual reliance, stated**: the nested run executes with `require_approval=False`, so the guarantee is *only* as strong as `_resolve_subagent_tools` — a regression there would be a silent bypass, which is why its tests are the load-bearing ones. |
| III | Fixed, typed tools | Yes | **PASS with A1** | `subagent_name` is a closed enum; `task` has a `_not_blank` validator; `tool_call_id` is injected. **A1**: `skill_search.query`, `use_skill.name` and `run_subagent.task` have no length bound (the note and memory tools do). |
| IV | Exactly-once side effects (NN) | Marginal | **n/a** | Nothing here writes business state. The one write is the usage-ledger row keyed by a unique nested thread id; a re-executed delegation after a crash records its tokens again, which is true spend, not a duplicate effect. |
| V | Bounded, observable failure | **Primary** | **PASS with 2 defects (B13, B14) and 1 advisory (A2)** | Bounded: 6 steps, 4000 tokens, $0.15, 45 s, a derived recursion limit, a one-turn spend reducer, `use_skill` gated behind search. Observable: `agent_subagent_run_total{outcome}`, `agent_subagent_duration_seconds`, `agent_use_skill_without_search_total`, `agent_skipped_required_tool_total`; logs carry metadata only. **B13**: the cached graph retains every run for the life of the process. **B14**: the failure paths skip the ledger. **A2**: a missing skills index degrades with a warning but no metric. |
| VI | Untrusted content is data | Yes | **PASS with A6** | The specialist's answer and every skill result pass credential scrubbing; skill and specialist files are repository-authored (trusted). The nested system prompt is a constant plus a constant notice — ctx-free (prompt-cache safe). **A6**: the answer is not framed as data when it re-enters the parent; bounded by Principle II. |
| VII | Test discipline | Yes | **PASS with the known gap (A7)** | 353 hermetic tests pass across the files listed above (plus the three products' domain tests), and two live tests drive a real skill and a real delegation. No test loads the shipped `skills/` folder, validates tags or tool references, or checks the images — B15 and B16 shipped through exactly that gap. |
| VIII | Why-first docs, honest gaps | Yes | **FAIL on drift (A8, B13's wording); PASS otherwise** | Module docstrings carry the reasoning and the live findings (list order, top-k of 1, the substring check's false positive). But pattern 46 and the code call the checkpoint store "ephemeral" — untrue once the graph was cached; a config comment says spend is not folded into the parent although it is; six settings are missing from `.env.example`; "Extending Further" lists none of B13–B16. |
| — | *Composition* constraint | **Primary** | **FAIL on B15** | "Skills and subagents are domain-tagged so they never leak across domains." Specialists default to Ecorp-only and every shipped one is correct. Two shipped skills are untagged and so reach three products that lack their tools. |
| — | *Configuration* constraint | Yes | **FAIL on A8** | "Tunables live in `Settings` with a matching `.env.example` entry." Six do not have one. |

**Gate result (pre-research)**: no violation of a NON-NEGOTIABLE principle. The read-only guarantee (II) is intact and structurally enforced. B13 and B14 are
Principle V / I-accounting defects; B15 breaks the *Composition* constraint; B16 is a deployment defect that makes the whole feature absent in containers without
any signal. They are *defects*, not justified exceptions; the plan proceeds because it describes shipped code.

**Post-design re-check (after `research.md`, `data-model.md`, `contracts/`)**: unchanged. Writing the registry-resolution contract made the Principle II
reliance explicit (the nested run's approval flag is *off*, so the resolver is the whole guarantee), and writing the lifecycle table in `data-model.md` §6 is what
made B13 and B14 visible as two halves of one gap: nothing owns what happens to a run's state *after* it ends, on any path.

## Project Structure

### Documentation (this feature)

```text
specs/007-skills-and-subagents/
├── plan.md
├── spec.md
├── research.md                    # Phase 0 — decisions + the live findings behind each; B13–B16 and A1–A8 as findings
├── data-model.md                  # Phase 1 — file shapes, registry, run lifecycle, thread ids, spend, settings
├── quickstart.md                  # Phase 1 — runnable checks per tier, incl. the B13–B16 reproductions
├── contracts/
│   ├── catalog-files.md           # SKILL.md / AGENT.md headers and the loader's accept/skip rules
│   ├── skill-tools.md             # skill_search / use_skill, visibility, the two guardrails
│   └── run-subagent.md            # the tool schema, registry resolution, the delegated run, outcomes
├── checklists/requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
app/agent/
├── skills.py                      # SkillRecord, load_skills, get_skills (lazy singleton), reload_skills
├── subagents.py                   # SubagentRecord, load_subagents, get_subagents, reload_subagents
├── subagent_tools.py              # _resolve_subagent_tools, _build_subagent_registry, _run_subagent_impl, graph cache, run_subagent (Ecorp)
├── subagent_domain_tools.py       # make_domain_subagent_tool (per-product run_subagent)
├── graph_skills.py                # use_skill_without_search node, _pending_skill_required_tool, _SKILL_REQUIRED_TOOL_MARKERS
├── graph_build_subagent.py        # build_subagent_graph (the leaner topology; compiles with a MemorySaver)
├── graph_routing.py               # should_continue: run_subagent never pauses; routes use_skill-without-search; folds subagent_spend into the budget check
├── graph_loop_guards.py           # _use_skill_called_without_search
├── graph_output_guardrails.py     # _skipped_required_sandbox_after_skill (the reactive check_output rejection)
├── graph.py                       # State.subagent_spend + _concat_or_reset reducer; MAX_SUBAGENT_* constants; _assemble_shared_graph_parts
└── tools.py                       # make_skill_tools, skill_tools_first, SkillSearchArgs/UseSkillArgs, _skill_visible_to_domain
scripts/index_skills.py            # make index-skills: embed name+description into the skills collection (recreates it)
skills/*/SKILL.md · subagents/*/AGENT.md                    # the bundled catalogs
app/core/metrics.py · app/core/config.py                    # the counters and the settings
Dockerfile                         # COPY app/ only — the cause of B16
tests/agent/ · tests/domains/*/test_domain.py · tests/live/test_chat_ui.py
postgres-init/                     # (none — no new tables)
```

**Structure Decision**: Disk is the single source of truth for both catalogs; the search index holds only a lookup key. Delegation is the same graph in a
leaner shape, never a second implementation, so a fix to a shared node reaches both loops. Product scoping lives in one place for each catalog
(`_skill_visible_to_domain`, `_subagent_declared_for_domain`) with opposite defaults, each justified in its docstring.

## Complexity Tracking

> Filled because the Constitution Check found four defects and several gaps. Defects are listed without a justification column: they are simply open.

| Violation / advisory | Why Needed | Simpler Alternative Rejected Because |
|----------------------|------------|-------------------------------------|
| **B13 (defect, open)** — the cached nested graph's in-process store keeps every delegated run's whole conversation. Reproduced: 100 runs → 100 threads. | Not needed — the graph cache was added to avoid recompiling per call, and the per-run unique thread id (needed for isolation) was never paired with a delete. | After the run (in a `finally`), read what is needed from the store and delete the run's thread — the installed store has `adelete_thread` (verified). Compiling the cached graph with no store at all is smaller still **if** nothing in the nested loop needs one; that must be shown by a test, and B14's fix wants the partial state first. |
| **B14 (defect, open)** — a run that times out or errors records no usage and returns no spend. Reproduced: a 500-token run that timed out → 0 ledger rows. | Not needed — the ledger write was placed on the success path only. | In the failure path, read the run's last checkpoint (`aget_state`) for tokens and cost, record them, then re-raise. Whether the tool should *return* a failure message plus spend (so the parent's live budget sees it) instead of raising is a decision for the fix. |
| **B15 (defect, open)** — `onboarding-brief` and `expense-summary` are untagged and so offered in support, ops and sales. Reproduced. | Not needed — the "no tag = everywhere" default is right for a generic skill; these two are not generic. | Tag both `domains: [ecorp]`, and add the catalog-pinning test (A7) so an untagged *shipped* skill, or one naming a tool its product lacks, fails a build. |
| **B16 (defect, open)** — the images contain no `skills/` or `subagents/` (and no `scripts/`); in a container the catalogs are empty and `run_subagent` is absent. | Not needed — the Dockerfile was written when only `app/` was code; the catalogs arrived later and only host-native runs were exercised. | `COPY skills/` and `COPY subagents/` in the Dockerfile; a CI smoke that asserts non-empty catalogs in the built image; a startup log + metric for an empty catalog. Mounting volumes instead would work but leaves the production image empty by default. |
| **A1** — `query`, `name` and `task` unbounded. | Added with the first version of the tools. | `max_length` on each (proposal: 500 / 100 / 4000 characters, the last well under the nested 4000-token budget), as module constants, with a failing test first. |
| **A2** — no deploy path builds the index; a missing index degrades without a metric; a re-index leaves a brief empty window. | The index was a dev-time convenience command. | A one-shot bootstrap step in the compose files and the release procedure; a counter on the degrade path; **deterministic point ids** (`uuid5` of the name — the script uses `uuid4` today) with create-if-missing, so a re-index is idempotent and never empties the collection. |
| **A3** — a running process keeps the old disk catalog after a re-index. | A lazily cached singleton avoids re-parsing on every call. | On a search hit whose name is absent from the cache, reload once (rate-limited); or a TTL; or document "restart after indexing" as the one step (cheapest, and honest). |
| **A4** — tags, tool names and model aliases are not validated. | The loaders are domain-agnostic on purpose (no import cycle with the tool registry). | Validate in the A7 pinning test, which can import the registry, rather than in the loader. |
| **A5** — substring-based required-tool detection; a dead `run_command_in_sandbox` branch in `use_skill`; two lists. | A pragmatic fix to a live failure (a skill said "don't estimate by hand" and was ignored). | One allowlist as the single source; delete or test the second branch. A real intent parser is rejected as larger than the problem. |
| **A6** — a specialist's answer re-enters the parent unframed. | It matches how other read-only tool output is handled. | Frame it in a delimiter plus a one-line "data" note — a decision, since it changes what the parent model sees; record it in pattern 46 either way. |
| **A7** — no test pins the shipped catalogs. | Loader tests use `tmp_path` deliberately, to be hermetic. | A test that loads the real folders and checks tags, tool references and the Dockerfile's `COPY` lines. |
| **A8** — six settings absent from `.env.example`; stale comments; "Extending Further" silent. | Written feature by feature. | Add the entries, correct the comments, add B13–B16 to the Roadmap. |

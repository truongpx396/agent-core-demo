# Contract: Skill Tools and Their Guardrails

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §3–§4](../data-model.md) | **Catalog files**: [catalog-files.md](./catalog-files.md)

**Status**: Retrospective — `app/agent/tools.py` (`make_skill_tools`, `skill_tools_first`, `SkillSearchArgs`, `UseSkillArgs`), `app/agent/graph_skills.py`,
`app/agent/graph_routing.py::should_continue`, `app/agent/graph_loop_guards.py::_use_skill_called_without_search`, `app/agent/graph_output_guardrails.py::_skipped_required_sandbox_after_skill`; tests in `tests/agent/test_tools.py`, `test_routing.py`, `test_nodes.py`, `test_agent_node.py`, `test_graph_integration.py`.

## Audience

The **model** (the tools' descriptions are its entire interface) and the graph (the guardrails). Each product has its **own** `(skill_search, use_skill)` pair, built by
`make_skill_tools(<product>)`; Ecorp's is `make_skill_tools("ecorp")`.

## `skill_search(query: str) -> str`

- **Arguments**: `query` — natural language describing the task, not a skill name. **No length bound (A1).**
- **Identity**: not required (a bundled capability).
- **Behavior**: hybrid search of the `skills` collection for `max(4 × top-k, 10)` hits → keep hits whose `name` is in the disk catalog **and** visible to the product → truncate to
  `SKILLS_SEARCH_TOP_K` (default 1).
- **Returns**: lines `- <name>: <description>` (from the hit payload); or `No matching skills found. Proceed using your other tools directly.`; or, if the collection query raises,
  `No skills catalog is available right now (has `make index-skills` been run?).` (a warning is logged with the error *class* only; **no metric — A2**).
- **Bounded** by the standard tool timeout; the result is credential-scrubbed.

## `use_skill(name: str) -> str`

- **Arguments**: `name` — the exact name from `skill_search`. **No length bound (A1).**
- **Behavior**: look `name` up in the disk catalog; unknown **or not visible to the product** → `No skill named '<name>' found. Call skill_search first to find the exact name of an available skill.`
  (the same text for both, so a hidden skill's existence is not revealed).
- **Returns**: the full body, unchanged — plus, if the body contains the text `run_command_in_sandbox`, an appended reminder to run a real script (a branch no shipped skill triggers — A5).
- **Not a side effect**; declared `read_only`; never needs approval.

## Guardrail 1 — `use_skill` without a search this turn is rejected

- **Trigger**: a model batch contains a `use_skill` call and no `skill_search` call exists in the current turn's messages (`_use_skill_called_without_search`; checked after the invalid-tool-name check, before approval).
- **Effect**: node `use_skill_without_search` appends **exactly one rejection tool message per pending call** — "You called use_skill without calling skill_search first this turn. Call skill_search now … if nothing
  matches well, don't guess a name; just answer directly" — increments `agent_use_skill_without_search_total`, and routes back to the agent. **No pause.**
- **After a real search this turn** the same call dispatches normally.
- **Why**: without it the model guessed a name, got "not found" and narrated the failure into its answer.

## Guardrail 2 — a required tool a loaded skill named must actually be called

- **Allowlist**: `_SKILL_REQUIRED_TOOL_MARKERS = ("run_python_in_sandbox",)`; matched as a **substring** of the `use_skill` tool message(s) in the current turn (A5).
- **Proactive**: before the agent's next generation, if such a skill was loaded and the tool has not been called this turn, a reminder is appended (`_pending_skill_required_tool`).
- **Reactive**: if the final answer arrives without the call, `check_output` rejects it for correction and `agent_skipped_required_tool_total` is incremented.
- **Scope**: only the allowlisted names; a skill naming any other tool is not enforced.

## Position in the tool list (pattern 50)

`skill_search`, `use_skill`, then `run_subagent` (when the product has one), then the product's action tools, then the rest. **Do not reorder**: list position, not wording, decided whether the small local model used them.

## Visibility contract

| Situation | `skill_search` | `use_skill` |
|-----------|----------------|-------------|
| Skill with no `domains` | listed in every product | loadable in every product |
| Skill tagged to other products | never listed | refused (same message as unknown) |
| Index entry for a skill no longer on disk | dropped | refused |
| Skill on disk, not yet indexed | not listed | loadable **by exact name** |

## Invariants a change must preserve

1. The body comes from disk, never from the index.
2. Both tools apply the same visibility test.
3. A rejection of a tool-call batch emits one tool message per pending call.
4. The skill tools stay first in the bound list.
5. Neither tool ever needs a tenant identity or an approval.

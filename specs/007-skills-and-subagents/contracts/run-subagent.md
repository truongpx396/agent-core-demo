# Contract: `run_subagent` — Delegation to an Isolated, Read-Only Specialist

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §4, §6–§9](../data-model.md) | **Constitution**: Principle II (read-only, no recursion), Principle V (bounds)

**Status**: Retrospective — `app/agent/subagent_tools.py` (`_resolve_subagent_tools`, `_build_subagent_registry`, `_run_subagent_impl`, Ecorp `run_subagent`),
`app/agent/subagent_domain_tools.py::make_domain_subagent_tool`, `app/agent/graph_build_subagent.py`, `app/agent/graph_routing.py`; tests in `tests/agent/test_tools.py`,
`test_routing.py`, `test_concurrent_turns.py`, `test_safety_budgets.py`, `test_streaming_terminal_events.py` and each product's `test_domain.py`.

## Audience

The **model** (the schema and docstring are its whole interface), the graph (routing, budget, streaming) and **operators** (metrics, ledger, traces).

## The tool

| Part | Contract |
|------|----------|
| Name | `run_subagent` — one tool object for Ecorp; **one new closure per other product**, built at that product's import time, or **none at all** if no specialist is declared for it |
| `subagent_name` | a **closed enum** of the specialists declared for that product, each named with its description in the schema (`- name: description`); a per-product enum *type* |
| `task` | string, must not be blank (`_not_blank`); **no length bound (A1)**. The docstring tells the model the specialist has no access to the conversation, so the task must be self-contained |
| `tool_call_id` | injected, hidden from the model; declared on the schema as well as the signature |
| Capability | `read_only` — a plain static declaration in `TOOL_CAPABILITIES`, with no special case in the approval gate |
| Returns | a `Command` updating `messages` (a tool message carrying the answer) and `subagent_spend` (one `(tokens, cost)` entry) |
| Position | in the leading tier right after the skill tools |
| Appears when | the product's registry is non-empty — **on a host-native start only today (B16)** |

## Routing

`should_continue` sends a `run_subagent` call straight to tool execution — never to `human_approval` — whether or not the optional approval flag is set
(`TestShouldContinueSubagent::test_run_subagent_call_never_needs_approval`, `…with_require_approval_unset_still_routes_to_tools`).

## Registry resolution (per product, at import)

See data-model §4. The contract: **a specialist's tool set is a subset of its product's read-only tools, never a superset of what it declared, never containing `run_subagent`,
and never "everything" when empty.** Dropped names are logged (`subagent declares an unknown tool; dropping it` / `…a non-read_only tool…`) and counted nowhere (A4).

## The delegated run — `_run_subagent_impl(name, task, config, …)`

| Aspect | Contract |
|--------|----------|
| Identity | refuse with the standard no-ctx message unless `config.configurable.ctx` is valid; otherwise pass **the same ctx** into the nested run |
| Unknown name | return `No subagent named '<name>' is registered.` (a message, not an exception) |
| Input | `[SystemMessage(<specialist prompt> + <citation-marker notice>), HumanMessage(<task>)]`; **no** parent history; `require_approval = False` |
| Model | the specialist's `model` alias, else `CHAT_MODEL`; `temperature = 0`; usage reporting on |
| Tools | exactly the resolved set, each declared read-only by a throwaway plugin (so an empty set is empty) |
| Graph | `build_subagent_graph()` — the main graph's nodes minus `check_semantic_cache`, `write_semantic_cache`, `suggest_followups`, `compact_history`, `context_window_exceeded` |
| Budgets | 6 agent steps · 4000 tokens · `MAX_SUBAGENT_COST_USD_PER_RUN` (0.15) · `SUBAGENT_TIMEOUT_SECONDS` (45) · graph-step limit `6 × 2 + 15` |
| Thread | `<parent thread>:subagent:<name>:<8 hex>`, unique per run, on the graph's **own in-process store** |
| Reuse | compiled graph + client cached per `(product, name)` when the real tool closures pass `use_cache=True`; tests never do |
| Tracing | parent callbacks threaded in; metadata `{subagent_name, parent_thread_id, domain}` tags every nested event |
| Answer | last AI message text, stripped, credential-scrubbed; empty → `Subagent '<name>' did not produce a final answer before hitting one of its own safety budgets.` (`budget_exceeded`) |

## Outcomes and what each leaves behind

| Outcome | Parent sees | `agent_subagent_run_total` | Duration observed | Ledger row | `subagent_spend` entry | Run's state in the store |
|---------|-------------|----------------------------|-------------------|------------|------------------------|--------------------------|
| completed | the answer | `completed` | yes | **yes** (if tokens > 0) | **yes** | **kept forever (B13)** |
| budget exceeded | the budget message | `budget_exceeded` | yes | **yes** | **yes** | **kept forever (B13)** |
| timeout | `Tool failed (TimeoutError: Tool call exceeded the 45s timeout.). Try a different approach.` | `timeout` | yes | **no (B14)** | **no (B14)** | **kept forever (B13)** |
| other exception | `Tool failed (<Type>: <text>). Try a different approach.` | `error` | yes | **no (B14)** | **no (B14)** | **kept forever (B13)** |
| no ctx / unknown name | the refusal / not-registered message | *(none)* | no | no | **yes** (0, 0.0) | — |

> **A consequence worth stating.** On a timeout the generic tool-error text invites "try a different approach", which may be a second delegation: another up-to-45 s run whose tokens are
> not recorded either (B14). The parent's own iteration, repeated-action and no-progress ceilings still bound it; its cost ceiling cannot see it.

## Streaming and concurrency

- The nested run's own reasoning tokens never reach the client's answer stream; its tool activity does, tagged with the specialist.
- Concurrent delegations (different turns, or two calls in one turn) never share a thread, state, answer or spend entry.

## Invariants a change must preserve

1. **Read-only, structurally.** The resolver is the whole guarantee, because the nested run executes with approval off. Any change to `_resolve_subagent_tools` or `_SubagentDomainPlugin` needs its tests first.
2. **Depth is exactly one.** `run_subagent` is stripped from every resolved set.
3. **The delegated run sees only its prompt and the task.**
4. **Identity is inherited, never replaced.**
5. **Every run is bounded** by steps, tokens, cost and time, and its outcome is counted.
6. **No shared cache.** The nested graph never reads or writes the semantic cache.
7. **A run's state has an owner after it ends** *(not yet true — B13, B14)*.

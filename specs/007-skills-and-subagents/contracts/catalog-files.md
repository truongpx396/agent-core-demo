# Contract: Catalog Files (`SKILL.md`, `AGENT.md`) and the Loaders

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §1–§2, §5, §8](../data-model.md)

**Status**: Retrospective — `app/agent/skills.py`, `app/agent/subagents.py`, `scripts/index_skills.py`; tests in `tests/agent/test_skills.py` and `tests/agent/test_subagents.py`
(all `tmp_path`-isolated — **none loads the real folders**, A7).

## Who this contract is for

An **author** adding a procedure or a specialist, and the code that loads them. It is the whole interface: there is no registration step, no API and no database.

## `skills/<slug>/SKILL.md`

```markdown
---
name: onboarding-brief                  # required, non-empty — the catalog key
description: Compose a new-hire …       # required, non-empty — what search embeds and the model reads
domains: [support]                      # optional — products this skill is offered in; absent = EVERY product
---

# Title
1. Numbered steps that name the tools to use …
```

## `subagents/<slug>/AGENT.md`

```markdown
---
name: ticket-researcher                 # required
description: Look up … Use for …        # required — shown in run_subagent's schema menu
tools: [search_docs, check_ticket_status]   # optional — absent = every read-only tool of the product
model: chat                             # optional — a proxy alias; absent = the parent's alias
domains: [support]                      # optional — absent = Ecorp ONLY
---

You are a focused research assistant … (this body is the specialist's system prompt)
```

## Loader behavior (both catalogs)

| Situation | Behavior |
|-----------|----------|
| Valid file | entry keyed by trimmed `name` |
| Missing/invalid front block, non-mapping header, blank `name`/`description`, empty body, wrong-typed `domains`/`tools`/`model` | **that file only** is skipped; a warning is logged; nothing is raised |
| Two files, one `name` | the first in sorted directory order wins; a warning is logged |
| Folder missing | empty catalog, no error |
| Subsequent reads | served from a process-wide cache; `reload_*()` re-reads — **nothing in a running server calls it** (A3) |

## Authoring rules the loaders do **not** enforce (today)

These are conventions; a violation passes every check and ships.

1. **Tag a skill that names a product-specific tool.** An untagged skill is offered everywhere (**B15** is two shipped files that broke this).
2. **A `domains` value must be a real product key** (`ecorp` is valid for a skill; `app/domains/registry.py` keys otherwise). A typo hides the file from everyone, silently (A4).
3. **A specialist's `tools` must exist and be read-only in each product it is declared for.** Unknown or non-read-only names are dropped with a log line only.
4. **A specialist's `model` must be an alias the proxy serves** (`litellm-config.yaml`: `chat`, `chat-backup`, `vision`, `embed`). An unknown one fails at the first call.
5. **A skill that needs a particular tool** is only enforced for the explicit allowlist in `graph_skills.py` (today `run_python_in_sandbox`); do not rely on prose alone.
6. **Do not name a listed required tool as a negative example** — the check is a substring match and will demand it anyway (A5).

## After editing a catalog file — what actually has to happen

| Change | Skill search sees it | `use_skill` serves it | Specialist menu |
|--------|----------------------|----------------------|-----------------|
| New skill | after `make index-skills` | **after a restart of every process** (cache) — until then the hit is dropped as "not on disk" | n/a |
| Edited skill body | n/a (index holds no body) | **after a restart** | n/a |
| Edited skill description | after `make index-skills` | n/a | n/a |
| New or edited specialist | n/a | n/a | **after a restart** (closed enum built at import) |
| Container deployment | **not at all today** — the image contains neither folder (**B16**) | — | — |

## `scripts/index_skills.py` (`make index-skills`)

- Reloads the disk catalog, **recreates** the `skills` collection (delete + create), and upserts one point per skill with a **random** id and payload `{text, name, description}`; never the body.
- Fails with a message and a non-zero exit when the catalog is empty or the stack is unreachable.
- During the recreate-then-upsert window a search finds no catalog (A2). Requires a running Qdrant and embedding model; not run by any deployment path except the host-native `restart-all`.

## Invariants a change must preserve

1. One bad file never removes another entry.
2. The index never holds a skill body; disk is the only authority.
3. A new product-scoping default must stay explicit and documented — the two catalogs' defaults are opposite on purpose.
4. A catalog edit is a content change, never a code change.

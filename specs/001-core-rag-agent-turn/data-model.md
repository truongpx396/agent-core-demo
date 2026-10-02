# Data Model: Core RAG Agent Turn Pipeline

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Research**: [research.md](./research.md)

**Status**: Retrospective — field names, types and lifecycles are read from the code. Where a
documented lifecycle does not match observed behavior, the table says so and points at the bug.

Persistent stores touched by this feature: the **checkpointer** database (graph `State`, schema
owned by the saver library), the **`appdata`** database (`chat_sessions`), **Qdrant** (documents,
read-only here), and **Redis** (semantic cache). Entities named in the spec map as follows.

| Spec entity | Concrete form | Where defined |
|-------------|---------------|---------------|
| Conversation | one LangGraph thread (`thread_id`) + one `chat_sessions` row | §1, §3 |
| Turn | the slice of `State` between one `HumanMessage` and its terminal outcome | §1, §5 |
| Source (Citation) | `{marker, doc_id, title, text, score}` record | §2 |
| Safety Budget | module constants + `Settings` fields | §4 |
| Quality Verdict | `last_retry_reason` + `retry_reason_repeat_count` | §1, §5 |
| Conversation Summary | `State.history_summary` | §1 |
| Error Envelope | `ErrorEnvelope{code, message, details}` | [contracts/error-envelope.md](./contracts/error-envelope.md) |
| Cached Answer | Redis JSON doc under `cache:<uuid>` | §3 |

---

## 1. Graph `State` (checkpointed)

`app/agent/graph.py::State` (a `TypedDict`). Persisted per `thread_id` by the checkpointer; the
shape is versioned by `STATE_SCHEMA_VERSION` (currently **1**).

**Lifecycle legend**: **R** = reset by `validate_input` at the start of every turn;
**A** = accumulates for the life of the thread; **W** = written once per turn by the named node;
**S** = set by the caller per call.

### Conversation content

| Field | Type | Lifecycle | Written by | Notes / validation |
|-------|------|-----------|------------|--------------------|
| `messages` | `list[BaseMessage]` (reducer `add_messages`) | **A** | callers, nodes | Seeded once with the system prompt; one `HumanMessage` per turn; trimmed only by `compact_history` via `RemoveMessage`. Multimodal content is a list of `{type: text\|image_url}` parts. |
| `history_summary` | `str` | **A** (never reset) | `compact_history` | Cumulative; bounded by `MAX_HISTORY_SUMMARY_CHARS = 4000`; exceeding it routes to `context_window_exceeded`. |
| `context` | `str` | W | `retrieve_context` | Numbered `[n] text` lines; framed in `<retrieved_document>` by `agent`. Not reset: only ever read after `retrieve_context` rewrote it in the same turn. |
| `context_anchor_index` | `int` | W | `retrieve_context` | Index of the turn's `HumanMessage` at that moment; `agent` splices summary + context here on every call so the prefix position is stable. |

### Turn counters and flow control

| Field | Type | Lifecycle | Written by | Notes / validation |
|-------|------|-----------|------------|--------------------|
| `iterations` | `int` | **R** | `agent` (+1) | Ceiling `MAX_ITERATIONS = 10`. Stays 0 only if the turn never reached `agent` (used to classify the outcome `rejected`). |
| `total_tokens` | `int` | **R** | `agent` | From `usage_metadata`; missing usage ⇒ 0 ⇒ ceiling never trips (fail-open, disclosed). |
| `total_cost_usd` | `float` | **R** | `agent` | `tokens/1000 × PRICE_PER_1K_TOKENS_USD[CHAT_MODEL]` (unlisted alias = $0). |
| `subagent_spend` | `list[tuple[int, float]]` (reducer `operator.add`) | **intended R — actually A (bug B1)** | `run_subagent` | One `(tokens, cost)` per completed delegation. Folded into the token/cost ceilings in `should_continue`. `validate_input` writes `[]`, which is a **no-op through `operator.add`**; reproduced: prior turns' entries survive. See spec *Known gaps* and `research.md` R10. |
| `run_id` | `str` (8 hex) | **R** | `validate_input` | Correlation id for logs/metrics; regenerated every turn, **not** on resume. |
| `require_approval` | `bool` | **S** | caller input | Opt-in gate; the mandatory gate does not read it (feature 003). |
| `approved` | `bool` | persists | `human_approval` | Not reset per turn; every `human_approval` outcome writes it explicitly. (Feature 003.) |
| `cancelled` | `bool` | persists — **never reset (bug, feature 003)** | `human_approval` | Set `True` on a cancel and not cleared by any later turn; see `specs/003-…/spec.md`. |
| `ctx` | `SecurityCtx \| None` | **R** | `validate_input` only | Stamped from `config["configurable"]["ctx"]`; read-only for every other node (feature 002). |
| `graph_version` | `str` | **R** | `validate_input` | `GRAPH_VERSION` env → short git SHA → `"unknown"`. Informational: a difference is *not* a resume error. |
| `state_schema_version` | `int` | **R** | `validate_input` | Compared with `STATE_SCHEMA_VERSION` by `resumability_error_async`. |

### Answer provenance and quality verdict

| Field | Type | Lifecycle | Written by | Notes |
|-------|------|-----------|------------|-------|
| `citations` | `list[dict]` (§2) | **R** | `retrieve_context`, `check_semantic_cache` | Every source *offered* this turn. |
| `used_citations` | `list[dict]` | **R** | `check_output` | `citations` ∩ markers present in the final text. What the `citations` event carries. |
| `ungrounded_claims_count` | `int` | **R** | `check_output` | `[n]` markers matching no real citation; computed independently of `used_citations`. |
| `likely_uncited_citations` | `list[dict]` | **R** | `check_output` | Offered but unreferenced, with heavy word overlap with the answer; almost always `[]` after auto-insert. |
| `likely_misattributed_citations` | `list[dict]` | **R** | `check_output` | Used marker whose citing sentences share no vocabulary with the source. |
| `deferred_instead_of_acting` | `bool` | **R** | `check_output` | Narrated intent / asked permission instead of acting. |
| `fabricated_tool_output` | `bool` | **R** | `check_output` | ≥2 markdown code fences with no real `tool_calls` behind them. |
| `skipped_required_tool` | `str \| None` | **R** | `check_output` | Name of a tool a loaded skill required but the answer bypassed. |
| `leaks_system_prompt` | `bool` | **R** | `check_output` | Long verbatim run of the seeded prompt in the answer. |
| `last_retry_reason` | `str \| None` ∈ {`leaked_prompt`,`too_short`,`fabricated`,`skipped_tool`,`deferred`,`uncited`,`misattributed`} | **R** | `check_output` | Fixed priority in that order. |
| `retry_reason_repeat_count` | `int` | **R** | `check_output` | 1 on first occurrence, +1 if the same reason repeats, reset to 1 on a different reason, 0 when no reason. `≥ MAX_CONSECUTIVE_SAME_RETRY_REASON (2)` ⇒ `retry_exhausted`. |
| `followups` | `list[str]` | **R** | `suggest_followups` | 2–3 items, or `[]` when the answer had no citations. |
| `cache_hit` | `bool` | **R** | `check_semantic_cache` | Read by `write_semantic_cache` (skip re-write) and `route_after_cache`. |
| `moderation_blocked` | `bool` | **R** | `moderate_input` | Read by `route_after_moderation`. |

---

## 2. Citation (Source) record

Built by `app/agent/tools.py::_citation_records`, numbered continuously across documents then
memories in one turn (`gather_context`).

| Field | Type | Rule |
|-------|------|------|
| `marker` | `str` | `"[n]"`, 1-based, unique within the turn; the only join key between answer text and source. |
| `doc_id` | `str` | The Qdrant point id (`str(hit.id)`). |
| `title` | `str` | `payload.title` → `payload.topic` → `payload.kind` → `"source"`. |
| `text` | `str` | `payload.parent_text` if present (child chunk matched, parent passage shown), else `payload.text`. |
| `score` | `float` | Cross-encoder logit for documents (floor −8.0); fused-rank score for memories (not re-ranked). |

Rules: child hits sharing a `parent_id` are de-duplicated to the highest-ranked one; memories are
never de-duplicated against each other; a marker in the final answer that is not in this set is
counted in `ungrounded_claims_count` and never appears in `used_citations`.

---

## 3. Persisted records

### 3.1 `chat_sessions` (Postgres `appdata`, `postgres-init/06-…`, `11-…`)

| Column | Type | Constraint |
|--------|------|------------|
| `thread_id` | `TEXT` | PRIMARY KEY |
| `tenant` | `TEXT` | NOT NULL |
| `principal` | `TEXT` | NOT NULL |
| `title` | `TEXT` | NOT NULL; first message truncated to 60 chars (`TITLE_MAX_CHARS`) + `…` |
| `domain` | `TEXT` | NOT NULL DEFAULT `'ecorp'`; set on first insert, immutable |
| `created_at`, `last_active_at` | `TIMESTAMPTZ` | default `now()`; the upsert refreshes only `last_active_at` |

Index `(tenant, principal, domain, last_active_at DESC)`. Written at the *start* of every turn
(best-effort, failure only logs) so a rejected turn still appears in the switcher. Scoping and
ownership semantics are feature 002's.

### 3.2 Checkpoint (Postgres `checkpointer`, library-owned)

Tables `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` created idempotently by
`AsyncPostgresSaver.setup()` on every process start; no hand-written DDL (`05-checkpointer-db.sql`
only creates the database). Keyed by `thread_id`; carries `State` (§1) and per-task pending
writes, which is what makes `astream_events_continue_turn` safe. Deliberately has **no**
tenant/principal column — ownership is checked through `chat_sessions`, by the caller.

### 3.3 Cached answer (Redis Stack JSON, prefix `cache:`, index `idx:semantic_cache`)

| Field | Index type | Notes |
|-------|-----------|-------|
| `tenant`, `principal` | TAG | both required for a hit (tag values escaped — `-` is query syntax) |
| `query` | TEXT | the *latest message text only* — no conversation context (gap G3) |
| `answer` | TEXT (no stem) | declared in the schema so it can be projected back on a hit |
| `citations` | TEXT (no stem) | JSON-encoded list of §2 records (the *used* subset) |
| `embedding` | VECTOR FLAT, FLOAT32, COSINE | dimension probed from the embed model at index creation |

Key `cache:<uuid4>` (a new key per write — no upsert); TTL `SEMANTIC_CACHE_TTL_SECONDS = 3600`;
hit iff cosine distance ≤ `1 − 0.95`.

### 3.4 Document point (Qdrant `docs`, read-only in this feature)

Payload fields this feature reads: `text`, `parent_id`, `parent_text`, `title`, `topic`, `kind`
(`document` | `memory`), `tenant`, `owner` (memories only), `created_at` (memories only). Named
vectors `dense` (cosine) and `sparse` (BM25, IDF modifier). Writers are ingestion and the
write tools (features 002/003).

---

## 4. Safety budgets (reference)

| Budget | Value | Owner |
|--------|-------|-------|
| Reasoning rounds / turn | 10 | `graph.py::MAX_ITERATIONS` (hardcoded safety net) |
| Tool calls / round | 5 | `MAX_TOOL_CALLS_PER_TURN` (hardcoded) |
| Identical consecutive batches | 3 | `MAX_REPEATED_ACTIONS` (hardcoded) |
| Same retry reason, consecutive | 2 | `MAX_CONSECUTIVE_SAME_RETRY_REASON` (hardcoded) |
| Tokens / turn | 16 000 | `MAX_TOKENS_PER_TURN` (hardcoded; fails open without usage metadata) |
| Cost / turn | $0.50 | `Settings.max_cost_usd_per_turn` |
| Tool timeout | 15 s | `tools.py::TOOL_TIMEOUT_SECONDS` |
| Turn timeout | 60 s | `Settings.request_timeout_seconds` |
| Graph recursion | `10×2+15 = 35` | `runtime.py::RECURSION_LIMIT` (derived) |
| History ceiling → floor | 24 000 → 4 000 est. tokens | `graph.py` constants |
| Summary | 4 000 chars | `MAX_HISTORY_SUMMARY_CHARS` |
| Cache | 0.95 similarity, 3 600 s | `Settings` |
| Relevance floor | −8.0 | `tools.py::MIN_RERANK_SCORE` |
| Sub-run (nested) | 6 rounds, 4 000 tokens, `max_subagent_cost_usd_per_run`, `subagent_timeout_seconds` | feature 007, listed for B1 context |

Tunables that are `Settings`-backed are mirrored in `.env.example`; the hardcoded ones are
deliberate loop-count safety nets, not policy knobs.

---

## 5. State transitions

### 5.1 A turn

```text
          ┌────────────────────────────── terminal outcomes ──────────────────────────────┐
 started ─┤ done            (answer delivered, incl. refusal / fallback / cache-served)  │
          │ approval_required (paused at human_approval — feature 003)                     │
          │ error           (timeout, cancelled, internal, pending_approval, …)            │
          └────────────────────────────────────────────────────────────────────────────────┘
```

Exactly one terminal outcome per streamed turn. `citations` / `followups` events immediately
precede `done` when non-empty.

### 5.2 Quality-verdict counter

```text
reason == None            → repeat_count = 0           → suggest_followups
reason == prior reason    → repeat_count = prior + 1   → ≥ 2 ? retry_exhausted : retry_output
reason != prior reason    → repeat_count = 1           → retry_output
```

`retry_exhausted` keeps the answer iff the reason ∈ {`too_short`, `uncited`} and the text is
non-blank; otherwise it replaces it with a category-level fallback (never quoting the content,
never confirming a leak).

### 5.3 Conversation lifecycle

```text
 (new) ──first turn──► running ──terminal done──► idle ──next turn──► running
                          │                          ▲
                          └─ human_approval interrupt ► paused ──resume──► running
                                                        │
                                          cancel ───────┴──► idle   (sets `cancelled`; see bug in 003)
```

"Paused" means `state.next` **and** some `task.interrupts` (not `state.next` alone). A resume
requires `paused ∧ state_schema_version == STATE_SCHEMA_VERSION`; otherwise `checkpoint_lost`
(not paused) or `checkpoint_incompatible` (schema differs).

### 5.4 History compaction

```text
raw (non-system) history ≤ 24 000 est. tokens → no-op
raw history > 24 000 → drop oldest WHOLE turns until ≤ 4 000 (never the system prompt or the current turn)
                     → summarize dropped turns into history_summary (LLM; failure ⇒ trim kept, summary unchanged)
                     → append a compaction breadcrumb message (role "system" in replays)
history_summary > 4 000 chars after that → context_window_exceeded (named terminal; thread is a dead end)
```

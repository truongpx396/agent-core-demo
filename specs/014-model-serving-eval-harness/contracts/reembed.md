# Contract: the embedding-identity guard and the re-embed procedure

Implements FR-018..020. Applies to the two vector collections that exist (`COLLECTION` — documents, notes, memories — and
`SKILLS_COLLECTION`) and to the answer cache. Subagents have no collection (`config.py`: their descriptions live in the tool schema).

## Scope and who may run it

`scripts/reembed.py` is an **operator-only maintenance script over a whole collection**. It reads every tenant's points because a collection is not
per-tenant, exactly as the existing sweep scripts do; it is not a tool, has no `ctx`, and nothing under `app/` may import it (a test enforces that).
What keeps Principle I intact is that it **writes only a verbatim copy**: every payload field, tenant and owner included, is carried unchanged, and a
test diffs every payload. It is run by a person, deliberately, during a window with writes paused.

## Naming

For logical name `L` and configured identity hash `H`:

| Thing | Name |
|---|---|
| physical collection | `L-H` (new) or `L` (legacy, pre-feature) |
| the alias every app read/write uses | `L-live` |
| answer-cache index / key prefix | `idx:semantic_cache:H` / `cache:H:` |

`qdrant_store` resolves `collection or COLLECTION` to `L-live` internally; callers pass no new argument.

## `qdrant_store` behavior

- `ensure_live(L)`: if `L-live` exists, return its target; elif a physical `L` exists, create `L-live → L` and mark **adopted**
  (warn once, count); else nothing yet. Safe to call from every process at start (creating an existing alias is caught).
- `ensure_collection(dim, L)` (used by `make ingest` / `make index-skills`, destructive by design): creates `L-H`, replacing an
  existing `L-H` of the same identity only, then repoints `L-live → L-H` atomically. It never touches another identity's collection.
- `check_identity(L, *, write)`: parses the hash from the target name (a legacy target is `adopted`, trusted) and compares with the
  configured hash; cached `EMBED_IDENTITY_CACHE_SECONDS`. An identity whose `resolved_model` is unknown hashes the alias name instead and is counted `unverified` (it is never refused for that). Raises
  `EmbeddingIdentityMismatch(collection, expected, found)` (an `AppError`-style exception with an `ErrorCode`), counted as
  `agent_embedding_identity_total{outcome}`.
- `hybrid_search`, `upsert`, `delete_by_filter`, `count_by_filter` call it; reads for `delete_by_filter`/`count_by_filter` are not
  refused (a data-subject delete must still work on a mismatched collection).

## `semantic_cache`

`_ensure_index` uses the identity hash in the index name and key prefix. A mismatch can't occur (a new identity is a new index);
`get` and `set` embed with `embed_query`. Old-identity entries expire through the existing TTL and are never read.

## Readiness

`GET /health/ready` response gains `warnings: list[str]` (default `[]`): `"embedding identity mismatch: documents"`,
`"embedding identity adopted (unverified): documents"`. **A warning never changes `status` or the HTTP code** (retrieval degrades,
turns run). `checks` is unchanged.

## `scripts/reembed.py` (`make reembed COLLECTION_NAME=<name>` / `python -m scripts.reembed <name> [--dry-run] [--page 256]`)

```
1. plan:   H' = configured identity. Print source (target of L-live), L-H', point count.
2. if L-H' exists and --resume: reuse; else create L-H' with the same dense+sparse schema, dim = embedding_dim().
3. copy:   scroll the source with payload + named vectors, page by page. For each point:
              dense'  = embed_documents([payload["text"]])   (document role, EMBED_BATCH_SIZE)
              sparse  = the source point's existing sparse vector (not recomputed)
              upsert (same id, payload verbatim, dense', sparse) into L-H'
   A point with no payload["text"] is reported and skipped; the run then cannot swap (parity fails) unless --allow-skips lists them.
4. verify: id-set parity with the source; for 100 sampled points, the nearest neighbour of the new dense vector in L-H' is itself.
5. swap:   re-count the source; if it changed since step 1, abort (exit 4) — the operator re-runs once writes are paused.
           else update_collection_aliases([DeleteAlias L-live, CreateAlias L-live → L-H']) in ONE request.
6. report: counts, durations, the old physical name (NOT deleted), the next steps (bake, then `--drop-old <name>` on request).
```

**Exit codes.** `0` swapped · `1` error (nothing swapped; the old collection still serves) · `2` usage · `3` embedding service unavailable
· `4` source changed during the run, or parity failed (nothing swapped).

**Guarantees.** Before step 5 the live collection is untouched, so a failure at any earlier step leaves the old collection serving
and no mixed-identity moment exists (FR-020). Payloads are copied verbatim, so tenant and owner scoping fields are untouched
(Principle I; a test diffs every payload). Re-running is safe (step 2's reuse, content-derived ids). Writes during the run are not
supported and are detected, not lost (step 5); the residual is the milliseconds between step 5's recount and its alias update
(disclosed in the docs).

**Logging.** Counts, ids' count, durations only — no text, no payload (Principle V).

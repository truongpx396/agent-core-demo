---

description: "Task list for feature 006 — Document Ingestion (retrospective)"
---

# Tasks: Document Ingestion

**Input**: Design documents from `/specs/006-document-ingestion/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (all present)

**Tests**: INCLUDED. Principle VII requires a regression test for every bug fix and the write path has no real-backend test at all. All four of this
feature's defects (B9–B12) were *missed by the existing tests*, which assert each stage against a fake and never ask "what does the person see?" or "what
else is in the collection?"; the open test tasks below are the most valuable work in this file.

**Organization**: Grouped by user story so each can be implemented and verified independently.

## Reading this file (retrospective conventions)

- **`[x]`** = built and present in the repository on 2026-10-02; the path is where it lives. Nothing `[x]` needs doing.
- **`[ ]`** = a **disclosed gap that is not built**. Where it fixes a defect the **failing test is written first** (CLAUDE.md working rules): write it, watch it
  fail on current code, then fix.
- Open ids (see plan.md *Complexity Tracking* / research.md *Deferred*): **B9** zero-text document reported as indexed · **B10** raw exception text reaches the
  uploader · **B11** an edited re-upload leaves the old text searchable, and no document can be removed · **B12** `make ingest` recreates the whole collection and
  is described as an upsert · **A1** no real-backend test of the write path · **A2** no document listing · **A3** originals never deleted from storage · **A4** `topic`
  unbounded · **A5** `ingest_url` buffers before bounding · **A6** library text in `ExtractionFailed` · **A7** no encrypted-PDF test.
- **No defect here weakens tenant isolation or approval**: ingestion is a person's own action and every record is tenant-stamped. B11 and B12 are *correctness and
  data-loss* defects; B12 deletes data.
- Tasks needing Docker say `integration`. Paths are repo-relative.

## Format: `[ID] [P?] [Story] Description *(requirements it serves)*`

- **[P]**: parallelizable (different files, no dependency on an incomplete task)
- **[Story]**: US1…US6 from spec.md; Setup / Foundational / Polish carry no story label

---

## Phase 1: Setup (Shared Infrastructure)

- [x] T001 [P] Ingestion tunables in the central settings object — `minio_*`, `max_upload_size_mb` (25), `max_upload_files_per_request` (5), `ingest_worker_max_concurrency` (10), `ingest_first_response_deadline_seconds` (30), `ingest_worker_reclaim_idle_seconds` (900) — in `app/core/config.py` (**example-environment entries are missing for most — feature 004 A3**) *(FR-024)*
- [x] T002 [P] Services — `minio` and `ingest-worker` in `docker-compose.yml`; `ingest-worker` (no MinIO container; an S3-compatible service) in `docker-compose.prod.yml`; the `ingest-worker` run target in `Makefile` *(FR-007)*
- [x] T003 [P] Counters (`agent_ingest_total`, `agent_ingest_refused_total`, `agent_upload_rejected_total`, `agent_upload_failed_total`) in `app/core/metrics.py` and the `IngestUploadFailing` rule in `observability/prometheus/alerts.yml` *(FR-025)*
- [x] T004 [P] The sample corpus for seeding in `scripts/sample_docs.py` *(FR-023)*

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The pieces every story uses.

- [x] T005 Parent/child chunking — paragraph-packed ~1200-char parents, 600-char children with 150 overlap — `chunk_text` in `app/ingestion/chunking.py` *(FR-019)*
- [x] T006 [P] In-memory text extractors and `ExtractionFailed` — `extract_pdf_text`, `extract_docx_text`, `EXTRACTORS_BY_SUFFIX` — in `app/ingestion/extractors.py` *(FR-008)*
- [x] T007 [P] The object-storage wrapper — `upload_bytes`, `download_bytes`, best-effort `delete_object`, lazy `ensure_bucket` — in `app/ingestion/object_store.py` *(FR-004, FR-005)*
- [x] T008 [P] The ingest stream and group, `publish_ingest_request`, `publish_result`, `read_results` with the first-event deadline, `delete_results_stream` — in `app/ingestion/ingest_queue.py` *(FR-004, FR-012, SC-008)*
- [x] T009 [P] `build_point`, the batched `upsert` (≤ 300 points per call) and `delete_by_filter` in `app/retrieval/qdrant_store.py` *(FR-020)*
- [x] T010 [P] Batched dense and local sparse embedding — `EMBED_BATCH_SIZE = 200`, `embed_texts`, `embed_sparse_batch` — in `app/retrieval/embeddings.py` *(FR-020)*
- [x] T011 [P] The shared SSRF guard in `app/core/url_safety.py` *(FR-022)*

**Checkpoint**: Foundation ready — text can be chunked, embedded, stored, queued and extracted.

---

## Phase 3: User Story 1 — A person uploads documents and they become searchable, citable knowledge (Priority: P1) 🎯 MVP

**Goal**: Fire-and-forget upload with per-file independence and a progress stream.

**Independent Test**: Upload a valid, an unsupported and an oversized file in one request; watch the valid one finish.

### Tests for User Story 1

- [x] T012 [P] [US1] Per-file validation, the request cap, bounded reads, the pointer job, the key shape, a stripped path, the orphan delete — in `tests/api/test_api.py` (`TestIngestUpload`) *(FR-001, FR-002, FR-003, FR-004, FR-005, SC-001, SC-010)*
- [x] T013 [P] [US1] The stream relays worker events and deletes its result stream once terminal — in `tests/api/test_api.py` (`TestIngestStream`) *(FR-012)*
- [x] T014 [P] [US1] The worker downloads, extracts, ingests and publishes `started`/progress/`done`; a docx dispatches to its extractor — in `tests/ingestion/test_ingest_worker.py` (`TestProcessJob`) *(FR-007)*

### Implementation for User Story 1

- [x] T015 [US1] `ingest_upload` and `_read_bounded` in `app/api/main.py` *(FR-001, FR-002, FR-003, FR-004, FR-006)*
- [x] T016 [P] [US1] `ingest_stream` (SSE relay with the first-event deadline) in `app/api/main.py` *(FR-012)*
- [x] T017 [US1] `process_job` — events, offloaded download and extraction, always ack — in `app/ingestion/ingest_worker.py` *(FR-007, FR-011)*
- [x] T018 [P] [US1] The page's upload panel — file picker capped client-side, one status line and progress bar per file, `trackIngestJob` — in `app/api/static/index.html` *(FR-001)*

### Open follow-ups for User Story 1 (not built) — **B9**

- [ ] T019 [US1] **B9 — write the failing test first**: in `tests/ingestion/test_ingest_worker.py` (`TestProcessJob`) add a test that an extractor returning `""` makes the job's terminal event an `error` naming "no extractable text" (not `done {chunks: 0}`). Fails today (reproduced — quickstart *Scenario B9*) *(FR-009, SC-006)*
- [ ] T020 [US1] **B9 — fix**: in `process_job` in `app/ingestion/ingest_worker.py` treat a 0-chunk result as a failure with that message, count it (a new reason on `agent_ingest_refused_total` or a new counter in `app/core/metrics.py`) and keep `ingest_text`'s own "blank is not an error" contract. Say in the message that scanned PDFs are not OCR'd *(FR-009)*

**Checkpoint**: US1 is *verified* from the person's point of view only after T019–T020.

---

## Phase 4: User Story 2 — Ingested content is always owned (Priority: P1)

**Goal**: Tenant and principal on every record; refusal without an identity; tenant-scoped storage keys.

**Independent Test**: Ingest as one tenant, query as another; ingest with no identity.

### Tests for User Story 2

- [x] T021 [P] [US2] A refusal without a ctx; every point carries the parent fields; tenants never collide on an id — in `tests/ingestion/test_ingestor.py` (`TestIngestText`) *(FR-013, FR-014, FR-015, SC-002)*

### Implementation for User Story 2

- [x] T022 [US2] The ctx refusal (`reason="no_ctx"`) and the `tenant`/`ingested_by`/`kind` stamping in `ingest_text` in `app/ingestion/ingestor.py` *(FR-013, FR-014)*
- [x] T023 [P] [US2] The `<tenant>/<job id>-<file name>` storage key in `ingest_upload` in `app/api/main.py` *(FR-004)*

**Checkpoint**: US2 stands on its own; reads are scoped by features 001/002.

---

## Phase 5: User Story 3 — Ingesting twice is safe, and a corrected version is correct (Priority: P1)

**Goal**: Repeats converge; edits replace.

**Independent Test**: Ingest identical text twice; kill a worker mid-job; re-upload an edited document.

### Tests for User Story 3

- [x] T024 [P] [US3] Identical content → identical ids; different content, tenant or source → different ids — in `tests/ingestion/test_ingestor.py` (`TestIngestText`) *(FR-016, SC-003)*
- [x] T025 [P] [US3] A reclaimed job is republished, dead-lettered past the cap, an unreadable payload still acks, the loop survives a Redis error — in `tests/ingestion/test_ingest_worker.py` (`TestHandleReclaimedJob`, `TestReclaimLoop`) *(FR-017, SC-004)*

### Implementation for User Story 3

- [x] T026 [US3] Content-addressed point ids — `_content_point_id` — in `app/ingestion/ingestor.py` *(FR-016)*
- [x] T027 [P] [US3] The recovery loop and `_handle_reclaimed_job` in `app/ingestion/ingest_worker.py` *(FR-017)*

### Open follow-ups for User Story 3 (not built) — **B11, A2, A3**

- [ ] T028 [US3] **B11 — write the failing test first**: in `tests/ingestion/test_ingestor.py` add a test with a recording store (upsert-by-id) that ingests "…within 90 days…" then "…within 30 days…" under one source and asserts only the "30 days" record remains for it. Fails today: both remain (reproduced — quickstart *Scenario B11*) *(FR-018, SC-005)*
- [ ] T029 [US3] **B11 — decide, then fix**: decide whether `source` is stable across a rename, then, **after** the upsert succeeds (so a crash never leaves a gap), delete the records of the same `(tenant, source)` whose ids are not in the new set — a `delete_by_filter` with an id exclusion in `app/retrieval/qdrant_store.py`, called from `ingest_text` in `app/ingestion/ingestor.py` *(FR-018)*
- [ ] T030 [US3] **B11/A2 — a way to remove and list a person's documents**: a delete-by-source function and a listing by `(tenant, source)` (a payload scroll) in `app/ingestion/ingestor.py`, exposed by the API in `app/api/main.py` with the ownership/authorization decision recorded in research.md; pairs with feature 002's A3 for memories *(FR-026)*
- [ ] T031 [P] [US3] **A3 — stop keeping originals forever**: delete the stored object after a successful ingest, and on dead-letter, in `app/ingestion/ingest_worker.py` via `delete_object` in `app/ingestion/object_store.py` — or set a bucket lifecycle rule; decide whether to keep originals for re-processing; test in `tests/ingestion/test_ingest_worker.py` *(FR-004)*

**Checkpoint**: after T028–T029 SC-005 holds; after T030 FR-026.

---

## Phase 6: User Story 4 — Documents are split for precise retrieval and surrounded for good answers (Priority: P2)

**Goal**: Small children matched, large parents shown; overlap; graceful sparse fallback; bounded batches.

**Independent Test**: Chunk a multi-paragraph text; force the sparse step to fail.

### Tests for User Story 4

- [x] T032 [P] [US4] Blank text, short text, paragraph packing, hard-splitting, overlap, the guard on overlap ≥ child size — in `tests/ingestion/test_chunking.py` *(FR-019)*
- [x] T033 [P] [US4] A sparse failure degrades to dense-only; one batched call each; progress ticks — in `tests/ingestion/test_ingestor.py` (`TestIngestText`) and `tests/ingestion/test_ingest_worker.py` *(FR-020)*

### Implementation for User Story 4

- [x] T034 [US4] The batched `ingest_text` core — one sparse call off the loop, dense batches with `on_progress`, one point per child, batched upsert — in `app/ingestion/ingestor.py` *(FR-019, FR-020)*

**Checkpoint**: US4 stands on its own.

---

## Phase 7: User Story 5 — A failed or stuck upload fails visibly and cleans up (Priority: P2)

**Goal**: One visible error per file, no orphan, no leaked internals.

**Independent Test**: Break each stage in turn.

### Tests for User Story 5

- [x] T035 [P] [US5] A publish failure deletes the stored file, counts the failure and reports that file's error — in `tests/api/test_api.py` (`TestIngestUpload`; a failing storage *write* is not tested separately) *(FR-005, SC-010)*
- [x] T036 [P] [US5] The storage wrapper — lazy bucket, upload, download raising on a missing key, delete never raising — in `tests/ingestion/test_object_store.py` *(FR-005)*
- [x] T037 [P] [US5] A download, extraction or refusal failure publishes an error and still acks; an unsupported type is reported — in `tests/ingestion/test_ingest_worker.py` (`TestProcessJob`) *(FR-008, FR-011)*

### Implementation for User Story 5

- [x] T038 [US5] The compensating delete and the rejected/failed counters in `ingest_upload` in `app/api/main.py` *(FR-005, FR-025)*
- [x] T039 [P] [US5] `ExtractionFailed` for an encrypted or unparseable file in `app/ingestion/extractors.py` *(FR-008)*

### Open follow-ups for User Story 5 (not built) — **B10, A4, A6, A7**

- [ ] T040 [US5] **B10 — write the failing test first**: in `tests/ingestion/test_ingest_worker.py` (`TestProcessJob`) add a test that a download raising an error naming an internal host does not put that text in the published event, while an `ExtractionFailed("PDF is password-protected")` still reaches the uploader verbatim. Fails today: the host is in the event (reproduced — quickstart *Scenario B10*) *(FR-010, SC-007)*
- [ ] T041 [US5] **B10 — fix**: in `process_job` in `app/ingestion/ingest_worker.py` catch `ExtractionFailed` and `IngestRefused` explicitly (their text is written for the uploader) and answer every other exception with a generic error carrying a code (reuse `internal_error_envelope` from `app/core/errors.py`), logging the class; update the page's `✕` rendering in `app/api/static/index.html` if it should switch on `code` *(FR-010)*
- [ ] T042 [P] [US5] **A6 — drop library text**: keep the category ("corrupt", "encrypted") and drop the parser's message from `ExtractionFailed("could not parse PDF: …")` in `app/ingestion/extractors.py`; adjust `tests/ingestion/test_extractors.py` *(FR-010)*
- [ ] T043 [P] [US5] **A7 — test a password-protected PDF**: in `tests/ingestion/test_extractors.py` generate an encrypted PDF with pypdf and assert `ExtractionFailed("PDF is password-protected")` *(FR-008)*
- [ ] T044 [P] [US5] **A4 — bound `topic`**: a length limit and character set on the `topic` form field in `ingest_upload` in `app/api/main.py` (refused per request, counted), with a test in `tests/api/test_api.py` *(FR-001)*

**Checkpoint**: after T040–T041 SC-007 holds.

---

## Phase 8: User Story 6 — The same pipeline serves scripts, other formats and seeding (Priority: P3)

**Goal**: One pipeline for every input kind; a seeding command that does not destroy.

**Independent Test**: Ingest a `.txt` and a `.md`; refuse another suffix; refuse a private address.

### Tests for User Story 6

- [x] T045 [P] [US6] `.txt`/`.md` ingest and a refusal of other suffixes — in `tests/ingestion/test_ingestor.py` (`TestIngestFile`) *(FR-021)*
- [x] T046 [P] [US6] The SSRF guard — scheme, private, loopback, mixed records, unresolvable — and `ingest_url` — HTML stripped, no redirects, status/transport/size refusals — in `tests/ingestion/test_ingestor.py` (`TestAssertSafeUrl`, `TestIngestUrl`) *(FR-022)*

### Implementation for User Story 6

- [x] T047 [P] [US6] `ingest_file`, `ingest_url`, `_assert_safe_url` and the HTML text extractor in `app/ingestion/ingestor.py` *(FR-021, FR-022)*
- [x] T048 [P] [US6] The seeding script through the shared pipeline in `scripts/seed.py` (`make ingest` in `Makefile`) *(FR-023)*

### Open follow-ups for User Story 6 (not built) — **B12, A1, A5**

- [ ] T049 [US6] **B12 — correct the description first (docs-only)**: the `ingest:` help text in `Makefile` ("upsert") and the README's command table and "real data to probe" suggestion — say that `make ingest` **recreates the collection and erases every tenant's documents, notes and memories**; add the warning to `CLAUDE.md`'s list of commands not to run unasked *(FR-023)*
- [ ] T050 [US6] **B12 — write the failing test first**: new `tests/scripts/test_seed.py` asserting that a seed run against a collection that already holds points it did not write leaves them in place (stub `qdrant_store`; assert `delete_collection`/`recreate_collection` is not called when the collection exists). Fails today *(FR-023, SC-009)*
- [ ] T051 [US6] **B12 — fix**: in `scripts/seed.py` create the collection only if it does not exist (`collection_exists`/`create_collection` in `app/retrieval/qdrant_store.py`, which also retires the deprecated `recreate_collection` for this path), and add an explicit, loudly named reset target (`make ingest-reset`) for the destructive case in `Makefile`; keep `scripts/index_skills.py`'s own collection decision separate (feature 007) *(FR-023)*
- [ ] T052 [P] [US6] **A5 — bound the URL fetch or delete it**: stream the body with a byte counter and a total deadline in `ingest_url` in `app/ingestion/ingestor.py`, or remove `ingest_url`/`ingest_file` (no production caller), updating `tests/ingestion/test_ingestor.py` *(FR-022)*
- [ ] T053 [US6] **A1 — an integration test of the write path (`integration`; Docker)**: new `tests/integration/test_ingestion_real_stack.py` using `tests/containers.py` (`ensure_redis`, `ensure_qdrant`, and a new `ensure_minio` helper in `tests/containers.py`): upload a small PDF through the real handlers, run `process_job`, assert the points are searchable, that a second upload adds none, that an edited re-upload leaves none of the old text (B11) and that the stored object exists/doesn't per A3. The only way to turn the mocks' assumptions into proof *(FR-027)*

**Checkpoint**: after T049–T051 SC-009 holds and `make ingest` no longer destroys data.

---

## Phase 9: Polish & Cross-Cutting Concerns

- [ ] T054 [P] **Disclose every open gap in the project docs now (docs-only)** — Principle VIII: add one entry each for **B9**–**B12** and **A1**–**A7** to `GRAPH_PATTERNS.md` *Extending Further* and a short list to the README *Roadmap*, each stating how it was established (reproduced vs. read) and, for B12, the data-loss warning. Land this with T049, before any fix
- [ ] T055 Re-run `specs/006-document-ingestion/quickstart.md` Tiers 1–3 and all four scenarios after the fixes; delete each resolved row from plan.md *Complexity Tracking* and each resolved gap from spec.md *Known gaps*
- [ ] T056 [P] After B11 lands, update pattern 24 in `GRAPH_PATTERNS.md` (it describes the ingestor but not replacement or removal) and the README's upload-pipeline text

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup → Foundational → stories.** Foundational blocks every story.
- **US1, US2, US3** (P1) need only Phase 2; US3's idempotence (T026) is what makes US1's retry safe.
- **US4** (P2) is the core inside US1 and US3; **US5** (P2) needs US1's endpoint and worker; **US6** (P3) shares US4's core.
- **Polish** last — except **T054** and **T049**, which land first.

### Open follow-ups — independence and PR boundaries

CLAUDE.md: one logical change per PR, ≤ ~400 hand-written lines.

| PR | Tasks | Touches | Notes |
|----|-------|---------|-------|
| 1 | T054, T049 | `GRAPH_PATTERNS.md`, README, `Makefile`, `CLAUDE.md` | docs-only; do first — it stops people running the destructive command unknowingly |
| 2 | T050–T051 (B12) | `scripts/seed.py`, `qdrant_store.py`, `Makefile`, new test | test-first; data-loss fix |
| 3 | T019–T020 (B9) | `ingest_worker.py`, one test file, `metrics.py` | test-first; small |
| 4 | T040–T041 (B10) | `ingest_worker.py`, `index.html`, one test file | test-first; per-class split |
| 5 | T028–T029 (B11 replace) | `ingestor.py`, `qdrant_store.py`, one test file | needs the source-stability decision |
| 6 | T030 (B11/A2 delete + list) | `ingestor.py`, `main.py`, tests | needs the authorization decision |
| 7 | T043, T042, T044 (A7, A6, A4) | extractors, `main.py`, tests | small, independent |
| 8 | T031 (A3) | worker, object store, test | retention decision |
| 9 | T053 (A1) | new integration test, `containers.py` | Docker; after PR 5 so it covers B11 |
| — | T052 (A5) | `ingestor.py` | decide: bound or delete |

PRs 2, 3, 4, 7 are mutually independent; run them in parallel.

### Parallel opportunities

- Setup T001–T004 and Foundational T006–T011 are [P].
- After Phase 2, US1/US2/US3 in parallel; within a story every test task is [P].

## Parallel Example: User Story 5

```bash
# Tests together (different files):
Task: "T035 Orphan delete in tests/api/test_api.py"
Task: "T036 Storage wrapper in tests/ingestion/test_object_store.py"
Task: "T037 Worker failures in tests/ingestion/test_ingest_worker.py"
# Implementation together:
Task: "T038 Compensation and counters in app/api/main.py"
Task: "T039 ExtractionFailed in app/ingestion/extractors.py"
```

## Implementation Strategy

### As-built order (what happened)

A general ingestor (text/file/URL) with parent/child chunking came with the first reorganization (2026-08-28) → the production upload pipeline (object storage, a queue, a worker) →
batching of embeddings and upserts after a real 9.7 MB PDF both took ~13 minutes and was rejected by the store at ~94 MB (09-09) → per-process worker concurrency and the per-request cap
(09-11) → the child chunk widened while the parent stayed (09-14) → native async I/O (09-18) → content-addressed ids when a retried job was found to duplicate every chunk, which is also what
made safe auto-retry of reclaimed ingest jobs possible (09-25/26, feature 003) → the compensating blob delete (09-29). The four defects sit at *seams the happy path never crosses*: an empty
extraction, a store outage, an edit, and a command written for a schema change.

### Closing the open follow-ups (what to do next)

1. **PR 1 (docs)** now — someone will run `make ingest` against a stack with real uploads.
2. **PR 2 (B12)** — the only defect that destroys data.
3. **PRs 3–4 (B9, B10)** — the two the uploader sees.
4. **PRs 5–6 (B11)** — replacement, then removal and listing.
5. **PRs 7–9** — small hardening, retention, and the integration test that would have caught B11.
6. Re-run quickstart, then delete each resolved row from plan.md *Complexity Tracking*.

### MVP scope

US1 + US2 + US3's idempotence (T001–T027) is the minimum that ingests a document safely and owns it correctly. None of B9–B12 weakens tenant isolation, so the system is *safe* to run as it
stands — but not **fully correct**: B9 reports success for nothing, B10 leaks internals to the uploader, B11 keeps superseded text answerable, and B12 will erase a deployment's data if run.

## Notes

- `[x]` means "present", not "re-verified today" — only Tier 1 (90 passed) was re-run on 2026-10-02, plus the reproduction scenarios.
- Tier 2/3 and the real-store idempotence claims are **not** verified by this batch.
- Features 001 (retrieval and citation), 002 (tenant scoping, the global/ownership rules and memory erasure), 003/004 (the queue, workers and crash recovery) and 009 (the crawler) own behavior this
  feature relies on.
- Do not run `make ingest`, `make clean`, `clear-*` or `restart-all` while working these tasks.

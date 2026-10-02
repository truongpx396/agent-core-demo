# Feature Specification: Document Ingestion

**Feature Branch**: `006-document-ingestion`

**Created**: 2026-10-02

**Status**: Implemented (retrospective) — with four reproduced defects, see *Known gaps* B9–B12

**Input**: User description: "Document ingestion (retrospective spec of the as-built system): a person uploads PDF or Word documents; each file is stored, handed to a background worker and turned into small, overlapping, tenant-stamped, searchable and citable pieces with a larger surrounding passage kept for answer quality; every file reports its own progress and outcome; the same content ingested twice converges on the same records; a failed or stuck upload fails visibly and cleans up after itself; plain text, markdown and web pages can be ingested by script through the same pipeline."

> **Retrospective note.** Written after the system was built (Spec Kit adopted 2026-10-01) from the code,
> `WORKER_CONCURRENCY.md`, `GRAPH_PATTERNS.md` patterns 20 and 24 and the README's upload pipeline. It
> describes what the system does today. **Boundaries.** How an ingested passage is *retrieved*, ranked, framed
> as untrusted data and cited is feature 001; who may see it (tenant scoping) is feature 002; the worker
> process model, the queue protocol and crash recovery it shares with chat are features 004 and 003; the web
> crawler it can borrow is feature 009. This feature is the **write path**: from an uploaded file to stored,
> searchable passages. Four defects were found by *reproducing* behavior with hermetic harnesses while writing
> it (B9–B12); seven further gaps were found by reading the code, tests and deployment files. All are in
> *Known gaps*.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A person uploads documents and they become searchable, citable knowledge (Priority: P1)

A person drops one or more PDF or Word files on the page (or posts them to the HTTP endpoint). Each file is
accepted or refused on its own, stored, and handed to a background worker; the person watches a progress bar per
file and ends with either "indexed (N chunks)" or an error naming what went wrong. Once indexed, the content is
part of that organization's corpus and appears — with its own title and a citation — in answers to later
questions. One bad file never loses the good ones in the same submission.

**Why this priority**: This is how the assistant learns an organization's own material. Without it the corpus is
whatever the seeding script loaded.

**Independent Test**: Upload a valid PDF, an unsupported file and an oversized file in one request; confirm three
independent outcomes, a progress stream for the valid one, and (after it finishes) a question answered with a
citation to it.

**Acceptance Scenarios**:

1. **Given** up to the per-request file limit of PDF/DOCX files each under the size cap, **When** they are
   uploaded, **Then** each gets its own job id and the response returns immediately (the work is not done in the
   request).
2. **Given** a file of another type, **When** it is uploaded, **Then** that file alone is refused with a message
   naming the supported types and the rest proceed.
3. **Given** a file over the size cap, **When** it is uploaded, **Then** it is rejected as soon as the running
   total crosses the cap — the whole file is never held in memory first — and the rest proceed.
4. **Given** more files than one submission allows, **When** they are uploaded, **Then** the whole request is
   refused before any file is touched.
5. **Given** a job, **When** its progress is streamed, **Then** the person sees `started`, zero or more progress
   ticks (chunks embedded of total) and one terminal `done` with the chunk count or `error`.
6. **Given** no worker is running, **When** a job's stream is opened, **Then** it ends with an error naming the
   cause within a short deadline rather than hanging.
7. **Given** a document with no extractable text (for example a scanned, image-only PDF), **When** it is processed,
   **Then** *(intended)* the person is told it produced nothing searchable. **As built the job reports success
   with zero chunks — see B9.**

---

### User Story 2 - Ingested content is always owned, and nothing is ingested without an owner (Priority: P1)

Every stored passage is stamped with the tenant and the person who ingested it and is restricted to that tenant on
every later read. A request with no valid identity is refused before anything is read or written — content is never
stored as public or ownerless. Documents are an organization resource: any member of the tenant can retrieve them
(feature 002), not only the uploader. Uploaded files are kept apart per tenant in storage.

**Why this priority**: Isolation is non-negotiable; the write path is where a record first receives its scope, and a
record stored without one can never be scoped later.

**Independent Test**: Ingest as tenant A; query as tenant B and confirm nothing returns; try to ingest with no
identity and confirm a refusal and a counter increment.

**Acceptance Scenarios**:

1. **Given** a valid tenant and principal, **When** content is ingested, **Then** every stored passage carries the
   tenant, the ingesting principal and the kind "document".
2. **Given** no valid identity, **When** ingestion is attempted by any path, **Then** it is refused, nothing is
   stored and the refusal is counted by reason.
3. **Given** two tenants ingesting identical content, **When** both are stored, **Then** their records never
   collide (the tenant is part of every record's identity).
4. **Given** an uploaded file, **When** it is stored, **Then** its storage key begins with the tenant and the job
   id, so two uploads of the same file name never overwrite each other.

---

### User Story 3 - Ingesting the same thing twice is safe, and ingesting a corrected version is correct (Priority: P1)

A retry after a crash, a double-clicked upload and a deliberate re-upload of the same file all converge on the same
records instead of duplicating them: every record's identity is derived from the content and its position, not drawn
at random, so a repeat overwrites itself. A worker that dies mid-job has its job retried automatically (a bounded
number of times) and then archived with the uploader told to try again. Re-uploading a file whose content has
*changed* should replace what the earlier version contributed.

**Why this priority**: Background work is delivered at least once, and an assistant that cites a superseded policy
beside the corrected one is wrong in a way users cannot see.

**Independent Test**: Ingest the same text twice and confirm identical record ids and no growth; kill a worker
mid-job and confirm the retry completes without duplicates; re-upload an edited document and confirm the old
wording is no longer retrievable.

**Acceptance Scenarios**:

1. **Given** byte-identical content, **When** it is ingested again (a retried job or a double submit), **Then**
   the same record ids are written and nothing is duplicated.
2. **Given** content that differs, or the same text from a different source, **When** it is ingested, **Then** the
   records are distinct.
3. **Given** a worker killed mid-job, **When** the job is reclaimed, **Then** it is re-run (not dead-lettered) up to
   the retry cap, converging on the same records; past the cap the uploader gets an error and the job is archived.
4. **Given** a document re-uploaded with edited content under the same name, **When** it finishes, **Then**
   *(intended)* only the new content is retrievable. **As built the superseded passages remain searchable and
   citable — see B11.**

---

### User Story 4 - Documents are split for precise retrieval and surrounded for good answers (Priority: P2)

Text is cut into larger "parent" passages on paragraph boundaries, and each parent into smaller overlapping "child"
windows. Only the children are embedded and matched — small passages match precisely — while the parent is stored
alongside and shown to the model and in citations, because a bare fragment often reads as ambiguous. Overlap means a
fact that straddles a cut is still captured whole by at least one child. The keyword-style (sparse) half of the
search is optional: if it fails, the document is stored with dense vectors only rather than failing.

**Why this priority**: It decides retrieval quality, but a different chunking would still *work*; it ranks below
correctness and ownership.

**Independent Test**: Chunk a multi-paragraph text and confirm parents break between paragraphs, children overlap by
the configured amount, and each child's parent text is carried. Force the sparse step to fail and confirm the
document is still stored.

**Acceptance Scenarios**:

1. **Given** multi-paragraph text, **When** it is chunked, **Then** paragraphs are packed into parents without being
   split, and a paragraph longer than a parent is hard-split.
2. **Given** a parent, **When** it is windowed, **Then** consecutive children overlap by the configured number of
   characters and only the last may be short.
3. **Given** blank text, **When** it is chunked, **Then** nothing is produced and no error is raised.
4. **Given** the sparse embedding step fails, **When** a document is ingested, **Then** it is stored dense-only and the
   degradation is logged.
5. **Given** a large document, **When** it is embedded, **Then** embedding proceeds in batches with a progress
   checkpoint after each, and storage is written in bounded batches so no request exceeds the store's size limit.

---

### User Story 5 - A failed or stuck upload fails visibly and cleans up after itself (Priority: P2)

Each failure is reported against the file that caused it, with a message the uploader can act on, and never aborts the
others. A file that reaches storage but cannot be queued is removed again. A job whose worker fails publishes an error
and is still acknowledged (it is not redelivered to repeat work). Rejections and post-acceptance failures are counted
separately, and the latter has an alert. An expected, caller-facing failure — an encrypted or corrupt PDF, an
unsupported type — is worded for the uploader; an unexpected one must not expose how the system is wired.

**Why this priority**: It is last of the correctness stories because it concerns the unhappy paths, but they are where
the system's recurring real bugs (silent hangs, orphans, leaked internals) have been.

**Independent Test**: Break each stage in turn (storage write, queue publish, download, extraction, embedding) and
confirm one visible error per file, no orphan blob, the other files unaffected and the right counter incremented.

**Acceptance Scenarios**:

1. **Given** the queue publish fails after a file was stored, **When** the endpoint handles it, **Then** the stored file
   is deleted, the failure is counted and that file's result carries an error.
2. **Given** a password-protected or corrupt PDF/DOCX, **When** it is processed, **Then** the uploader receives a clear,
   specific message.
3. **Given** a worker failure that is not one of the expected ones (the store is unreachable, an embedding call
   fails), **When** it is reported, **Then** *(intended)* the uploader receives a generic message without internal
   host names or driver text. **As built the raw exception text is forwarded — see B10.**
4. **Given** a handler failure, **When** it is processed, **Then** the job is acknowledged and not redelivered.
5. **Given** the upload endpoint, **When** a tenant exceeds its per-minute budget, **Then** it receives a 429.

---

### User Story 6 - The same pipeline serves scripts, other formats and seeding (Priority: P3)

Plain text and markdown files, a web page fetched by address, and pasted text all go through the same chunk, embed and
store core, so there is one pipeline that cannot drift. Fetching an address is guarded against being pointed at
internal systems: only secure addresses, every resolved address checked, redirects not followed, size bounded. The
sample-corpus seeding command uses it too. Throughput is tuned by per-process concurrency and per-request caps that are
distinct knobs.

**Why this priority**: It serves developers and operators rather than the upload flow, and two of its entry points have no
production caller.

**Independent Test**: Ingest a text file and a markdown file; confirm a refusal for an unsupported suffix; confirm an
address resolving to a private range is refused before any fetch.

**Acceptance Scenarios**:

1. **Given** a `.txt` or `.md` file, **When** it is ingested, **Then** it is chunked and stored like any other source;
   **Given** another suffix, **Then** it is refused naming the supported ones.
2. **Given** an address that is not secure, resolves (in any record) to a private, loopback or reserved range, or
   cannot be resolved, **When** it is ingested, **Then** it is refused before any request is made and counted.
3. **Given** a fetched page, **When** it is ingested, **Then** markup is stripped (scripts and styles skipped), redirects
   are not followed, and a failing status, a transport error or an oversized body is refused and counted by reason.
4. **Given** the seeding command, **When** it runs, **Then** it ingests the sample documents under a fixed
   script identity through the shared pipeline. *(It first erases the whole collection — see B12.)*

---

### Edge Cases

- Text from a PDF is best-effort and layout-approximate; a DOCX contributes paragraphs only (no tables, headers,
  footers or embedded objects).
- A scanned PDF has no text layer and yields nothing (no OCR) — see B9.
- A very large document is embedded in batches and stored in batches of at most 300 records; a single oversized request
  once cost ~13 minutes of embedding work when the store rejected it.
- A file name's directory components are stripped; two uploads of the same name are distinguished by job id.
- The progress stream's first-event deadline bounds only the wait for a worker to *pick the job up*, never the job's real
  duration.
- A reader that disconnects deletes the job's result stream; an unread one expires after five minutes.
- A sparse-embedding outage makes new documents slightly less findable by exact keyword until they are re-ingested.
- The job stream is not ownership-checked: the job id is an unguessable random value (feature 002 R14).

## Requirements *(mandatory)*

### Functional Requirements

**Upload and acceptance**

- **FR-001**: The upload endpoint MUST accept PDF and DOCX files, and MUST validate each file independently: an unsupported
  type or an oversized file MUST be reported against that file and MUST NOT stop the others.
- **FR-002**: A request with more files than the per-request cap MUST be refused whole, before any file is touched; the cap is a
  per-request guard distinct from worker throughput.
- **FR-003**: Each file MUST be read in bounded chunks and rejected once the running total crosses the per-file size cap, without
  materializing the whole file first.
- **FR-004**: An accepted file MUST be written to object storage under a key that begins with the tenant and a fresh job id, and a
  job carrying a *pointer* (never the bytes) MUST be queued; the request MUST return one result per file with either a job id or an
  error.
- **FR-005**: A file stored but not queued MUST be removed again (best-effort), counted and reported as that file's error.
- **FR-006**: The endpoint MUST be rate-limited per tenant (feature 001) and MUST NOT do parsing or embedding in the request.

**Processing**

- **FR-007**: A worker MUST download the file, extract text by file type, ingest it and publish `started`, progress and one terminal
  `done` (with the chunk count) or `error`; blocking download and extraction MUST be offloaded from the event loop.
- **FR-008**: A file that claims to be a PDF or DOCX but cannot be parsed, or is password-protected, MUST fail with a specific
  message worded for the uploader.
- **FR-009**: A document that yields no extractable text MUST be reported as such rather than as success. *(Not met — B9.)*
- **FR-010**: An unexpected failure MUST be reported to the uploader without internal host names, driver text or other wiring
  details. *(Not met — B10.)*
- **FR-011**: A job MUST be acknowledged whether it succeeds or fails, so a deterministic failure is not redelivered.
- **FR-012**: A job's stream MUST end with an error within a deadline if no worker ever picks the job up; the deadline MUST NOT bound
  the job's own duration.

**Ownership and scoping**

- **FR-013**: Ingestion MUST be refused, by every entry point, without a valid tenant and principal; the refusal MUST be counted by reason.
- **FR-014**: Every stored passage MUST carry the tenant, the ingesting principal and the kind "document", and every read MUST restrict to
  the caller's tenant (feature 002).
- **FR-015**: A stored record's identity MUST include the tenant, so identical content from two tenants never collides.

**Idempotence and recovery**

- **FR-016**: A record's identity MUST be derived from the tenant, the source, the chunk's position and a hash of its text, never drawn at
  random, so re-ingesting identical content overwrites itself.
- **FR-017**: A reclaimed ingest job MUST be re-run (up to the retry cap), and beyond the cap the uploader MUST be told to upload again
  and the job archived to a dead-letter store, counted by outcome.
- **FR-018**: Re-ingesting a *changed* document under the same source MUST replace the earlier version's passages. *(Not met — B11.)*

**Chunking and embedding**

- **FR-019**: Text MUST be split into parent passages on paragraph boundaries (a longer paragraph hard-split) and each parent into
  overlapping child windows; only children are embedded; each child's parent text MUST be stored with it.
- **FR-020**: Dense embedding MUST proceed in batches with a progress checkpoint after each; the sparse leg MUST degrade to dense-only on
  failure; storage MUST be written in bounded batches.

**Other entry points**

- **FR-021**: `.txt` and `.md` files and pasted text MUST be ingested through the same pipeline; any other suffix MUST be refused.
- **FR-022**: Fetching an address MUST be guarded: secure scheme only, every resolved address checked against private, loopback and reserved
  ranges, redirects not followed, a failing status or transport error or oversized body refused and counted by reason.
- **FR-023**: The seeding command MUST use the shared pipeline under a fixed script identity and MUST NOT erase content it did not write.
  *(Not met — B12.)*

**Operation**

- **FR-024**: Per-process job concurrency MUST be bounded and tunable, and the thread pool used for blocking work MUST be sized to it
  (feature 004); upload caps MUST be tunable separately.
- **FR-025**: Post-acceptance upload failures MUST be counted separately from pre-write rejections and MUST have an alert.
- **FR-026**: A person MUST be able to remove a document they ingested. *(Not met — no such path exists, B11.)*
- **FR-027**: The write path SHOULD be covered by a test against real object storage, queue and vector store. *(Not met — A1.)*

### Key Entities *(include if feature involves data)*

- **Upload / Job**: One file's submission — a job id, a storage key, a file name, a content type, the identity, an optional topic.
- **Stored File**: The original bytes in object storage, keyed `<tenant>/<job id>-<file name>`.
- **Parent Passage / Child Chunk**: A paragraph-packed passage and its overlapping windows; the child is what is embedded.
- **Document Record**: One stored child — dense and optional sparse vectors plus the child text, parent text and id, title, source,
  ingesting principal, kind, tenant and optional topic.
- **Job Result Stream**: The per-job ordered events `started`, progress, `done` or `error`, expiring after five minutes.
- **Ingest Job Delivery / Dead Letter**: The shared queue mechanics (features 003/004) on a separate stream and group.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In one submission mixing valid, unsupported and oversized files, every file gets its own outcome and the valid ones are
  indexed; no file's failure changes another's.
- **SC-002**: 100% of ingestion attempts without a valid identity are refused before any read or write, by every entry point.
- **SC-003**: Ingesting byte-identical content N times yields exactly the records of one ingest (same ids, no growth).
- **SC-004**: A worker killed mid-job leaves no duplicate records after the retry completes.
- **SC-005**: A re-uploaded, edited document leaves none of the superseded passages retrievable. *(Not met — B11.)*
- **SC-006**: A document with no extractable text is reported to its uploader as having produced nothing searchable. *(Not met — B9.)*
- **SC-007**: No failure message reaching an uploader contains an internal host name or driver text. *(Not met — B10.)*
- **SC-008**: With no worker running, an upload's stream ends with an error within 30 s.
- **SC-009**: Running the sample-seeding command never removes content it did not write. *(Not met — B12.)*
- **SC-010**: A file stored but not queued leaves no orphaned object (best-effort) and increments the failure counter.

## Assumptions & Known Gaps

**Assumptions**

- Documents are an organization (tenant) resource: any member may retrieve them; the ingesting principal is recorded for attribution,
  not enforced for reads (feature 002).
- A real deployment puts an authenticating gateway in front of the endpoint; the identity headers are trusted-layer values.
- Object storage is MinIO locally and an S3-compatible service in production; the production compose file defines no storage container.
- Extracted text quality is "good enough to retrieve and cite", not a faithful conversion.

**Out of scope for this feature**

- Retrieval, ranking, reranking, citation formatting and framing of retrieved text as data (feature 001).
- The worker process model, concurrency sizing, graceful stop and crash-recovery protocol (features 004 and 003).
- The headless-browser crawler and sandbox (feature 009).
- OCR, table and layout extraction, other file formats, per-user document ownership, a document library UI.

**Known gaps (disclosed, with how each was established)**

- **Bug B9 — a document with no extractable text is reported as indexed (reproduced at function level).** Blank text makes the
  ingest write nothing and return 0 by design ("not an error"), and the worker publishes `done` with `chunks: 0`; the page then shows
  "✓ indexed (0 chunks)". Reproduced with an extractor returning an empty string (what a scanned, image-only PDF produces): the
  events were `started`, `done {chunks: 0}`. The uploader believes the document is searchable; it is not. Not fixed here.
- **Bug B10 — an unexpected worker failure forwards the exception's own text to the uploader (reproduced at function level).** The
  worker's catch-all publishes `{"type": "error", "content": str(exc)}` with no error code, and the page renders it as `✕ <content>`.
  Reproduced with an object-store client raising a connection error naming `minio.internal.example:9000`: the uploader's event carried
  that text verbatim. The same defect class as the chat path's, which now answers an unexpected failure with a generic envelope
  (`internal_error_envelope`); the two catch-alls differ in that this worker also raises *deliberate* uploader-facing messages (unsupported type, an unparseable PDF), so it needs a
  per-exception-class decision rather than a blanket swap. Not fixed here.
- **Bug B11 — re-uploading an edited document leaves the superseded text searchable, and no document can ever be removed
  (reproduced at function level).** Record ids are derived from content, so unchanged chunks overwrite themselves and *changed*
  chunks get new ids — and nothing deletes the old ones. Reproduced with a fake store that upserts by id: ingesting "Refunds are
  accepted within 90 days…" and then the corrected "…30 days…" under the same source left **both** in the collection. A retrieval that
  returns the old wording is a wrong answer the user cannot see. There is also no delete path for documents at all (the only
  deletion function is the memory-erasure one, and nothing calls it — feature 002 A3), and uploaded originals are never removed from
  object storage (A3). Not fixed here.
- **Bug B12 — the seeding command erases the entire collection, including every tenant's uploaded documents, notes and memories, and
  is described as an "upsert" (established by reading and by the installed client).** `make ingest` runs `scripts/seed.py`, which
  calls `ensure_collection`, which calls the vector store client's `recreate_collection` — in the installed client (1.19.0) a
  `delete_collection` followed by `create_collection`. The collection holds documents, notes and memories for all tenants. The
  Makefile's help says "Embed sample docs and upsert them into Qdrant", the README's command table says "Embed sample docs → Qdrant",
  and the README suggests running it so there is "real data to probe" before a security scan. The script's own comments call it
  "destructive"; nothing prompts. `recreate_collection` is also deprecated in the installed client. Not fixed here.
- **A1 — the write path has no real-backend test.** The tests mock object storage, the queue and the vector store; no integration or live
  test drives upload → worker → store → search. Principle VII's known gap: nothing proves the real bucket, stream or collection behaves
  as the mocks assume. *(Read: grep of the integration, live and deepeval tiers finds no ingest path.)*
- **A2 — nothing records which documents exist.** There is no listing of a tenant's documents, no job directory and no per-document id;
  the job stream is deliberately unauthenticated beyond its random id. *(Read.)*
- **A3 — uploaded originals are never deleted from object storage.** The only delete is the compensating one when a publish fails; a
  successfully ingested file, a failed job and a dead-lettered job all keep their blob forever. There is no retention setting.
  *(Read.)*
- **A4 — the `topic` form field is unbounded and unvalidated** and is copied onto every chunk's payload of the file(s) in the request.
  *(Read.)*
- **A5 — the URL entry point buffers the whole response before checking its size and bounds each timeout phase, not the total.**
  `ingest_url` reads the full body, then compares its length to the 2 MB limit; an oversized or slow-drip response is held in memory.
  `ingest_url` and `ingest_file` have no production caller (only tests). *(Read.)*
- **A6 — a corrupt-document error path embeds library error text.** `extract_pdf_text` includes the parser's own message in
  `ExtractionFailed("could not parse PDF: …")`, which is then published to the uploader; it is expected and caller-facing, but its text is
  not controlled by this codebase. *(Read.)*
- **A7 — a password-protected PDF has no test.** `extract_pdf_text` raises `ExtractionFailed("PDF is password-protected")` for an encrypted file, and
  FR-008 depends on it; the extractor tests cover real text, multi-page joining, garbage bytes and empty bytes, but no encrypted document. *(Read from the tests.)*

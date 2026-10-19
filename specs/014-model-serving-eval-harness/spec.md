# Feature Specification: Model Serving Seams and an Evaluation Harness

**Feature Branch**: `014-model-serving-eval-harness`

**Created**: 2026-10-17

**Status**: Implemented. The specification is #175; the implementation is #177 (the harness, its data and baselines), #176 (the embedding roles, identity guard and `/v1/embeddings`), #178 (the classifier endpoint and the routing seam) and a closing change (the head trainer, labeller and classifier runner, and the closing docs). Three tasks are human or operator acts and stay open by design: T045 (a person reviews the generated dataset items), T058's decision (adopt the embedding prefixes or not) and T076 (a person accepts a head). With no accepted head the router stays at "no advice" and every turn runs locally, which is the specified default.

**Input**: User description: "Model-serving seams and an evaluation harness for the small models (classification, embedding, reranker, moderation). Three parts, one feature: (1) query classification for model routing, served by the ML service with BAAI/bge-small-en-v1.5 primary and sentence-transformers/all-MiniLM-L6-v2 as automatic fallback, plus a new gateway alias so a stronger model can be attached by configuration only; (2) embedding models done properly: separate query and document embedding with the task prefixes the current model requires, and an OpenAI-compatible embedding endpoint on the ML service so the `embed` alias can point at an ONNX model served there; (3) a model-evaluation harness, built the way published benchmarks and practitioners build them, with recorded baselines for the current models, so that the next model decision is made by running candidates against it."

> **Why one spec.** The three parts share one dependency chain: choosing a router head, an embedding model, or
> a reranker are all *model decisions*, and until now each was made from a blog post or a one-off script (the numbers
> that motivated this spec came from a throwaway benchmark on one laptop). The harness (Story 1) is what makes the
> other two judgeable, the embedding work (Story 2) is the first thing it will be asked to judge, and the routing
> seam (Story 3) is the first consumer of a classifier that has to be *evaluated*, not trusted.
> **Delivery.** This is more than one reviewable change (CLAUDE.md: ≤ ~400 hand-written lines per PR, ~1,000 ceiling).
> The suggested boundaries, to be refined by `/speckit-plan`, are in *Assumptions*.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A maintainer judges any candidate model against a recorded baseline (Priority: P1)

A maintainer is considering a different embedding model, a different reranker, a different prompt-injection screen, or a
routing classifier. They declare the candidate, run one command for that kind of model, and get a report that puts the
candidate next to the recorded baseline of the model in use today: how good it is on this repository's own domains
(ecorp, support, ops, sales) and on public reference sets where the licence allows, how fast it is (typical and worst-case
latency, cost per chunk), how big it is, what licence it carries — and, for every quality number, whether the difference
from the baseline is real or inside the noise of a small evaluation set. They decide from the report, not from a blog post.

**Why this priority**: Every other story in this feature ends in "…and we should confirm it is actually better". Without
this, the embedding swap (Story 2) and the routing head (Story 3) are judged by taste, and a worse model can ship
because it was faster or newer. It is also deliverable alone: it changes no runtime behavior.

**Independent Test**: With only the current models available, run the full evaluation: a baseline report exists for each
kind of model; run the same command for a candidate identical to the baseline and see "no difference" on every metric;
run it for a deliberately useless candidate and see it reported worse; run it with the network cut after the first fetch
and see the same result.

**Acceptance Scenarios**:

1. **Given** the recorded baseline for the embedding model in use and the repository's retrieval evaluation set, **When**
   the maintainer evaluates a candidate embedding model, **Then** one report shows both side by side: recall at the
   candidate-list sizes the app actually uses (20 before rerank, 5 final), reciprocal rank and NDCG@10, each with its
   uncertainty; typical and worst-case query latency; milliseconds per chunk at the app's ingest batch size; on-disk
   size; vector dimension; licence; and a per-metric verdict of *better*, *worse* or *inconclusive*.
2. **Given** a candidate that is the baseline model under another name (a control), **When** it is evaluated, **Then**
   every quality metric is reported as *no difference* and latency within normal run-to-run noise.
3. **Given** a deliberately degenerate candidate (random vectors, random scores, a classifier that always says one
   label), **When** it is evaluated, **Then** it is reported clearly worse than the baseline on every quality metric.
4. **Given** the reranker, **When** it is evaluated, **Then** it is scored on fixed first-stage candidate lists of a stated
   size (not on whatever the current embedding model happens to retrieve), by NDCG@10 and MRR@10, with per-query latency
   at that list size.
5. **Given** the prompt-injection screen, **When** it is evaluated, **Then** the report gives the detection rate at a
   fixed 1% false-positive rate and the false-positive rate on benign prompts written in the style of the four domains
   (including benign prompts that *talk about* instructions and policies), at the threshold the app actually uses.
6. **Given** a routing classifier and an evaluation set where each prompt has a recorded outcome for the local model and
   for the stronger model, **When** it is evaluated, **Then** the report gives accuracy, macro-F1, calibration error and
   a cost–quality curve: how much of the quality gap between the two models is recovered at each share of turns
   escalated — alongside a random router and a simple prompt-length router at the same shares.
7. **Given** a public dataset whose licence is unrecorded or forbids this use, **When** the maintainer tries to include
   it, **Then** the harness refuses and says why; every dataset in a report appears with its version and licence.
8. **Given** data used to train or tune a classifier head or a decision threshold, **When** an evaluation set overlaps it
   (exactly or near-duplicate), **Then** the overlap is reported and the affected result is marked contaminated.
9. **Given** a dataset or model that has not been fetched and no network, **When** a run starts, **Then** it stops
   before measuring anything and names exactly what to fetch; it never prints a partial report as if complete.
10. **Given** a machine with no models and no services, **When** the default test suite runs, **Then** the metric
    definitions, the report format, the baseline comparison and the dataset checks are verified against hand-computed
    values, and none of that needs a model.

---

### User Story 2 - Embeddings are used correctly, and an ONNX model can be served and swapped in safely (Priority: P2)

Today the app sends the same raw text to the embedding model whether it is a question or a stored passage, although the
model in use is documented as needing different prefixes for each. A maintainer fixes this by making "embed a query" and
"embed a document" two distinct operations with configurable prefixes. They also add an embedding endpoint to the ML
service, speaking the same request shape as the hosted embedding APIs, so that pointing the `embed` alias at it — by
configuration, with no application change — runs a small ONNX model locally. When they later swap the embedding model or
change its prefixes, the system makes it impossible to mix two models' vectors in one collection, and gives them one
documented procedure to rebuild everything under the new model.

**Why this priority**: It is a latent quality bug on the main retrieval path and it is the first decision the harness
will be asked to judge. It is behind Story 1 because *whether* a prefix or a new model helps must be measured, not
assumed; it is ahead of Story 3 because it has no policy implications.

**Independent Test**: With the defaults unchanged, run the existing ingestion and question flows and see identical
behavior. Then configure prefixes, run the re-embed procedure, and see retrieval work under the new scheme; try to
write a vector from the old scheme into the rebuilt collection and see it refused. Point the `embed` alias at the ML
service and repeat ingest and question answering with no code change.

**Acceptance Scenarios**:

1. **Given** the defaults, **When** the app embeds any text, **Then** the vectors are the same as before this feature
   (no prefix is applied until an operator configures one), and nothing needs re-embedding.
2. **Given** a configured query prefix and document prefix, **When** the app embeds a user question, a stored passage, a
   note, a skill description or a seed document, **Then** each uses the form that matches its role; no call site is left
   using an operation that does not say which role it plays.
3. **Given** the semantic answer cache, which compares questions with earlier questions, **When** it embeds, **Then**
   both sides of the comparison use the same form.
4. **Given** a vector collection (and the answer cache) built under one embedding identity — model, vector size and
   prefix scheme — **When** the configured identity differs, **Then** the difference is detected on first use, writes to
   that collection are refused, the condition is visible in the health report and a metric, retrieval from that
   collection degrades the way a retrieval outage does today rather than returning wrong-space results, and the error
   names the documented procedure.
5. **Given** the documented re-embed procedure, **When** an operator runs it for a collection, **Then** the collection is
   rebuilt under the new identity from the text already stored (no re-upload), every point keeps its tenant and owner
   scoping and its identifier, answers cached under the old identity are never served again, a failure part-way leaves the
   old collection serving, and running it twice is safe.
6. **Given** the ML service serving an embedding model, **When** the gateway's `embed` alias is pointed at it by
   configuration, **Then** ingestion and question answering work end to end with no application code change, and the
   vectors agree with the model's own reference implementation within a stated tolerance.
7. **Given** a request naming a model the service does not serve, a batch above the stated ceiling, or an input above
   the stated length, **When** it arrives, **Then** it is rejected with a clear, bounded error — not truncated silently
   or run unbounded.
8. **Given** one model in the service fails to load or run, **When** other endpoints are called, **Then** they keep
   working; the failure is visible in the service's health report and metrics, and loading is retried with a bound.

---

### User Story 3 - A turn can be routed to a stronger model without weakening any guarantee (Priority: P3)

An operator attaches a stronger model through a new gateway alias — by configuration only, any OpenAI-compatible API,
no code change. A lightweight classifier in the ML service reads the user's current message and advises whether the
local model can answer it or the stronger model should. When the operator has configured the alias and accepted a
trained head, the advised turns run on the stronger model; every other turn, and every turn when the alias or head is
absent, runs exactly as it does today. The classifier only advises which model answers: it never decides what needs
approval, what a tenant may see, or whether a side effect is repeated.

**Why this priority**: It is the only story that changes what answers users see, and the only one that sends tenant
content to a model the operator may not host; it must come last, be inert by default, and be judged by Story 1's
harness before anyone turns it on. The constitution currently forbids a cloud model as a target model, so this story
begins with the amendment that makes the optional alias lawful.

**Independent Test**: With the alias unconfigured, run the existing turn tests unchanged and see no difference. Configure
a stand-in stronger model and a head; send one turn the head sends up and one it does not, and see which model served
each in the usage ledger; stop the classifier and see every turn still answered by the local model.

**Acceptance Scenarios**:

1. **Given** the constitution, **When** this story is implemented, **Then** its "LLM access" rule has first been amended —
   through the constitution workflow, in its own change with a sync-impact report — to permit an optional,
   operator-configured escalation alias while still requiring that the core run fully offline against the local model
   when the alias is unset; and no change that lets a turn use the alias merges before it.
2. **Given** the alias is unset, or no accepted head exists, or the router switch is off, **When** any turn runs,
   **Then** the model that serves it, the usage-ledger row, the spend and every existing test outcome are identical to
   today.
3. **Given** an alias, a head, and an operator-set threshold, **When** a turn's current message scores above it,
   **Then** that turn runs on the stronger model, the ledger records the concrete model that served it, and the turn is
   subject to the same per-turn, per-person and per-tenant spend caps as any other.
4. **Given** the primary classifier model cannot load, errors, or misses its deadline, **When** a turn asks for advice,
   **Then** the fallback classifier model, with its own head, answers; the response says it was the fallback; and the
   service returns to the primary after a bounded recovery interval.
5. **Given** both classifier models are unavailable, **When** a turn asks for advice, **Then** the answer is "no advice"
   and the turn runs on the local model, with the condition counted; no turn fails because of routing.
6. **Given** the stronger alias is unreachable or errors before producing output, **When** an escalated turn starts,
   **Then** it degrades to the local model and the degradation is counted.
7. **Given** a turn that pauses for human approval and resumes (or is continued after a crash), **When** it continues,
   **Then** it continues on the model it started on; the classifier is not asked again.
8. **Given** the same tool call under either routing outcome, **When** it runs, **Then** its capability tier, approval
   requirement, tenant scoping and duplicate protection are identical — proved by a test that runs both outcomes.
9. **Given** a turn that would be answered from the answer cache, or that the input screen refused, **When** it runs,
   **Then** the classifier is not called; it never sees content that moderation has not already passed.
10. **Given** a head evaluated by Story 1's harness on held-out data, **When** it is proposed for use, **Then** it beats
    both a random router and a prompt-length router at the same escalation share, and the chosen threshold is recorded
    with the curve that justifies it.

---

### Edge Cases

- **Prefix change with existing vectors.** Turning on prefixes changes the embedding space even though the model is
  unchanged; the identity check covers the prefix scheme, not just the model name.
- **A head is bound to its model.** The two classifier models share a vector size but not a vector space; a head trained
  on one must be refused when loaded against the other (and against another revision of the same model).
- **Empty, very long and non-English messages** reach the classifier. Length is bounded by the model's own limit, with
  truncation recorded; an empty message yields "no advice". The head is validated on English only and its report says so;
  how it behaves on other languages is unmeasured, which is a disclosed gap, not a claim.
- **Embedding dimension differs from the collection** (a swap to a different vector size): detected by the identity
  check before any write, not by the vector store's error.
- **Concurrent ingestion while re-embedding**: writes during the procedure are not supported. The procedure notices that the
  source changed and stops without switching, so no write is silently lost; the milliseconds between its final check and the
  switch are a disclosed gap.
- **A re-embed that is interrupted**, and **a re-embed of a collection that is empty**, are both safe and re-runnable.
- **The ML service restarts mid-run** while the gateway's `embed` alias points at it: embedding calls fail the way a
  gateway outage does today (retried, then surfaced), not silently with zero vectors.
- **A benchmark dataset changes upstream** under the same name: the pinned version/checksum no longer matches, the run
  stops, and a baseline is never compared against a different dataset version.
- **Latency measured on different hardware** is never compared with a baseline recorded on other hardware; quality
  metrics, which do not depend on hardware, still are.
- **A tiny evaluation set** makes a real improvement look inconclusive; the report says "inconclusive" and how many more
  examples would be needed, rather than a confident wrong verdict.
- **Cached answer and routing**: the cache is keyed to the question, not the model; a cached answer produced by the local
  model may be served to a question that would now be escalated. This is accepted (see *Deliberate gaps*).

## Requirements *(mandatory)*

### Functional Requirements

**Evaluation harness (Story 1)**

- **FR-001**: There MUST be one repeatable command per kind of model — embedding, reranker, prompt-injection/input
  screening, query classifier — and one that runs all of them. Each MUST write a report in a uniform human-readable form
  and a machine-readable form.
- **FR-002**: Every report MUST record: the model's identity and exact revision, its kind, licence, on-disk size and
  resident memory while loaded; each dataset's identity, version and licence; a hardware profile (processor, core count,
  thread setting, runtime library versions); the repository revision; and the date.
- **FR-003**: Quality metrics MUST follow the practice for each kind. *Embedding*: recall at the list sizes the app uses,
  MRR and NDCG@10 over a frozen set of questions each mapped to the passages that answer it. *Reranker*: NDCG@10 and
  MRR@10 over fixed candidate lists of a stated size. *Input screening*: detection rate at a fixed 1% false-positive
  rate, ROC-AUC, and precision/recall plus false-positive rate on domain-style benign prompts at the app's operating
  threshold. *Classifier*: accuracy, macro-F1, expected calibration error, and a cost–quality curve (share of turns
  escalated against share of the quality gap recovered) with a random router and a prompt-length router as controls.
- **FR-004**: Efficiency metrics MUST be reported for every model: typical (p50) and worst-case (p95) latency for a single
  request, milliseconds per item at the app's real batch size, on-disk size and resident memory; for embedding models
  also vector dimension and the storage that dimension implies per million chunks.
- **FR-005**: Every quality metric MUST carry a confidence interval from resampling the evaluation set, and a comparison
  against the baseline MUST label each delta *better*, *worse* or *inconclusive* (interval spans zero), stating how
  large an evaluation set would be needed to resolve an inconclusive one.
- **FR-006**: The repository MUST record, versioned, a baseline for each current model: the embedding model in use
  through the gateway path the app uses, the cross-encoder reranker, the prompt-injection screen, and the classifier
  (primary and fallback) once their heads exist. Quality baselines are hardware-independent; latency baselines are keyed
  by hardware profile and compared only within a profile.
- **FR-007**: A candidate MUST be addable by declaring it — identity, source, and how input text must be prepared for it
  (prefixes, pooling, length limit) — without editing metric, dataset or report code.
- **FR-008**: The harness MUST include control candidates (the baseline itself, and degenerate ones), and a test MUST
  assert their expected verdicts so that a broken metric cannot silently pass a bad model.
- **FR-009**: Datasets: for each kind there MUST be a small, frozen, versioned set built from this repository's four
  domains, with recorded provenance (how generated, what was reviewed by a person) and disjoint development and test
  splits; public sets MUST be pinned by version and checksum, fetched on demand into an untracked cache, and recorded
  with their licence; a set with an unrecorded or incompatible licence MUST be refused (Scenario 7).
- **FR-010**: Held-out discipline: anything used to train a head, tune a threshold or choose a candidate MUST come from
  the development split; reported results MUST come from the test split; exact and near-duplicate overlap between them
  MUST be detected and the affected result flagged.
- **FR-011**: A run MUST work offline once its models and datasets are cached, and MUST fail early, naming what is
  missing, when they are not; it MUST NOT emit a partial report presented as complete.
- **FR-012**: The evaluation runs are a manual tier (they need models and, for the gateway path, running services) and
  MUST NOT run in the default test suite; the metric definitions, report format, baseline comparison, dataset
  verification and control verdicts MUST be covered by the default hermetic suite, which needs no model.
- **FR-013**: Where the app reaches a model through a path (the gateway's `embed` alias; the ML service's rerank,
  input-screen and classification endpoints), the harness MUST measure through that path, and MAY additionally measure a
  candidate in-process before it is served; the report states which.
- **FR-014**: A comparison MUST end in a plain summary of the trade-offs (quality, speed, size, licence, and for an
  embedding model the cost of re-embedding), and MUST NOT itself change any setting.

**Embedding (Story 2)**

- **FR-015**: Embedding a query and embedding a document MUST be two distinct operations; every existing caller (retrieval
  search, the answer cache, notes and memories, skill indexing, seeding, bulk ingestion) MUST use the one that matches
  its role, and a test MUST fail if a caller uses neither.
- **FR-016**: A prefix for each role MUST be configurable through the application settings and the example environment
  file, defaulting to empty so that default behavior is unchanged; the recommended values for the model in use MUST be
  documented next to them with the re-embed requirement.
- **FR-017**: A comparison between like items (a question against earlier questions) MUST use one role on both sides.
- **FR-018**: Each vector collection and the answer cache MUST carry its embedding identity — the resolved model, the
  vector size and the prefix scheme — recorded when it is created and checked on use.
- **FR-019**: On an identity mismatch the system MUST refuse writes, MUST degrade reads as a retrieval outage degrades
  today, MUST show the condition in the health report, MUST count it in a metric covered by an alert rule, and MUST name
  the re-embed procedure in the error. It MUST NOT place a vector from one identity into a collection of another.
- **FR-020**: A documented re-embed procedure MUST rebuild a collection under a new identity from its stored text; keep
  every point's identifier, tenant and owner scoping (Principle I); make the answer cache unreadable under the old identity;
  be safe to repeat; leave the old collection serving if it fails; stop without switching if the source changed during the
  run; and leave no moment in which the live collection holds both identities.
- **FR-021**: The ML service MUST offer an embedding endpoint with the request and response shape of the hosted
  embedding APIs (one or many inputs in, vectors in the same order and a token count out), serving the model named in
  the request from a configured list, with explicit ceilings on batch size and input length, and with concurrent calls
  coalesced into fewer model passes as the service's other endpoints are.
- **FR-022**: The service MUST be able to serve BAAI/bge-small-en-v1.5 and gte-modernbert-base (quantized) by
  configuration, and any further model by declaring it (FR-007's declaration is reused); no model that needs a heavy
  numerical framework added to the image.
- **FR-023**: The application's default embedding model MUST remain the one it uses today; switching the gateway's
  `embed` alias to the ML service MUST be a configuration-only change, followed by the re-embed procedure.
- **FR-024**: A model that fails to load or run MUST NOT prevent the service's other endpoints from working; the service
  MUST report each model's state in its health report and retry loading with a bound.
- **FR-025**: The service's served vectors MUST be checked against the model's reference implementation on a sample,
  within a stated tolerance, in a marker-gated test.
- **FR-026**: The embedding endpoint MUST count requests, items, errors and load failures and observe latency and batch
  size, and a sustained failure MUST raise an alert.

**Classification and routing (Story 3)**

- **FR-027**: The ML service MUST offer a classification endpoint that, for each text given, returns a decision (local
  model suffices / stronger model advised), a score, the classifier model that produced it (primary or fallback) and the
  head version.
- **FR-028**: The primary classifier model MUST be BAAI/bge-small-en-v1.5 and the fallback sentence-transformers/
  all-MiniLM-L6-v2, each with its own trained head; a head MUST be refused unless it was trained against the exact model
  revision it is loaded with.
- **FR-029**: Falling back MUST be automatic on failure to load, on error and on a missed deadline, MUST be flagged in the
  response, and MUST return to the primary after a bounded recovery interval; when neither is available the answer MUST
  be "no advice", never an error that fails a turn.
- **FR-030**: A head MUST be produced offline by a documented, reproducible procedure from versioned labelled data
  (FR-009, FR-010), and MUST carry its provenance — data version, model revision, held-out metrics and date; a missing or
  invalid head means "no advice".
- **FR-031**: The app MUST ask for advice at most once per turn, only when a model call will actually be made (not on a
  cache hit, not on a refused input, not when continuing after an approval pause), only after input moderation, and only
  with the user's current message — no tenant identifiers, no other user's content — under an explicit deadline.
- **FR-032**: A new gateway alias for the stronger model MUST exist in both the local and the hosted gateway
  configurations, inert unless configured, and attachable to any OpenAI-compatible API by configuration only; the
  contract the target must meet (the chat request shape including tool calls) MUST be documented. No vendor is chosen.
- **FR-033**: A turn MUST be escalated only if the alias is configured, an accepted head exists, the router switch is on,
  the head's score meets the operator-set threshold, and the alias has a known price whenever unpriced models are blocked;
  otherwise it runs as today. (A turn whose spend allowance is already exhausted is refused before routing is reached, as
  it is today, so routing adds no separate cap check.) One setting MUST turn the router off.
- **FR-034**: The model chosen for a turn MUST hold for the whole turn, including across approval pause and resume and
  crash continuation.
- **FR-035**: Routing MUST be advisory only: it MUST NOT change any tool's capability tier, the approval requirement,
  tenant scoping, duplicate-call protection or moderation (Principles I, II, IV, VI); this MUST be proved by a test that
  runs the same scenario under both outcomes.
- **FR-036**: An escalated turn MUST be metered with the concrete model that served it and held to the existing caps; an
  alias with no known price MUST be treated by the existing rule for unpriced models.
- **FR-037**: Failure policy (Principle V): a classifier error, deadline miss or absence → the local model, counted; a
  stronger-alias failure before output → the local model, counted; sustained fallback use or sustained degradation MUST
  raise an alert; routing's own logs and metrics MUST carry only metadata (decision, score band, model, head version,
  latency), never message content.
- **FR-038**: A head MUST NOT be recommended for use until Story 1's harness shows, on held-out data, that it beats a
  random router and a prompt-length router at the same escalation share; the chosen threshold and its curve MUST be
  recorded with the head.
- **FR-039**: Training and evaluation data for any head MUST be authored or derived from this repository's own content
  and public sets with a compatible licence; real tenant traffic MUST NOT be used.

**Governance and documentation**

- **FR-040**: The constitution MUST be amended first, through the constitution workflow in its own change, with a
  sync-impact report and a minor version bump: an optional, operator-configured escalation alias is permitted as a model
  target; the core MUST still run fully offline against the local model when it is unset; cloud use for evaluation and
  red-team grading is unchanged; no other principle is weakened. Guidance files that restate the old rule MUST be
  corrected in the same change.
- **FR-041**: Each new behavior MUST be documented with the failure that motivated it in the pattern file that matches its
  subject, the README, the runbook and the example environment file; every gap left open MUST be named (Principle VIII).
- **FR-042**: Beyond FR-012's harness tier, the new turn-path call to the ML service MUST be mocked by default in the
  default test suite, and any other test that needs a real model or service MUST carry the existing markers and skip, not
  fail, when it is unreachable (Principle VII).

### Key Entities

- **Model candidate**: a model under consideration — identity, exact revision, kind, licence, size, and how text must be
  prepared for it (prefixes, pooling, length limit).
- **Benchmark dataset**: a frozen, versioned set for one kind of model — version/checksum, provenance, licence, and
  disjoint development and test splits.
- **Baseline record**: the stored result of the model in use today on a dataset version — quality metrics with
  intervals, and latency/size keyed by hardware profile.
- **Evaluation report**: one candidate against one baseline — metrics, intervals, per-metric verdicts, trade-off summary,
  and everything needed to reproduce the run.
- **Embedding identity**: the model, vector size and prefix scheme under which a collection's vectors were made; recorded
  with the collection, compared on use.
- **Classifier head**: a small trained component bound to one classifier model revision — provenance, held-out
  metrics, chosen threshold and curve.
- **Routing decision**: for one turn, "local", "escalated", or "no advice", with the classifier model that produced it;
  fixed for the turn.
- **Escalation alias**: the gateway name behind which an operator attaches a stronger model; inert until configured.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A maintainer can evaluate a candidate model of any one kind against its baseline and read one comparison
  report in under 10 minutes of run time on the reference machine for the repository's own datasets (one-time downloads
  excluded), using one command.
- **SC-002**: Re-running the same candidate on the same machine reproduces every quality metric exactly and median
  latency within 15%.
- **SC-003**: The control candidates behave as designed in 100% of test runs: the baseline against itself is *no
  difference* on every metric, and every degenerate candidate is *worse* on every quality metric.
- **SC-004**: 100% of datasets in any report show a version and a recorded licence, and none with an unrecorded or
  incompatible licence can be included.
- **SC-005**: With defaults unchanged, 100% of the existing automated tests pass unmodified and every ingestion and
  question flow produces the same vectors as before this feature.
- **SC-006**: Across the swap and re-embed procedures, zero attempts to place a vector of one embedding identity into a
  collection of another succeed (a test attempts each of: different model, different size, different prefix scheme), and
  100% of such attempts are visible in the health report within one health-check interval.
- **SC-007**: Pointing the gateway's `embed` alias at the ML service, then re-embedding, lets an ingestion and a question
  complete end to end with zero application source changes.
- **SC-008**: With the stronger alias unset, or the head missing, or the router off, 100% of turns are served by the same
  model, and write the same usage-ledger rows, as before this feature.
- **SC-009**: With both classifier models stopped, 0 turns fail because of routing, and routing adds no more than 50 ms at
  the 95th percentile to the start of a turn when the classifier is healthy.
- **SC-010**: On held-out routing data, an accepted head escalates fewer turns than a random router needs to recover the
  same share of the quality gap, and fewer than a prompt-length router does.
- **SC-011**: The same tool call under both routing outcomes shows identical approval, tenant-scoping and duplicate-call
  behavior in 100% of cases tested.
- **SC-012**: A maintainer with only this repository and a changelog entry can attach a different stronger model, or a
  different embedding model, using documented configuration steps and no source edit.

## Assumptions

- **Decisions already taken** (from the clarification round): (a) the constitution amendment is part of this feature and
  lands first; (b) the ML service serves bge-small-en-v1.5 and gte-modernbert-base by configuration, but the app's default
  embedding model **stays** the current one until the harness shows a candidate wins on this repository's data — flipping
  it is a later, data-driven configuration change plus the re-embed procedure; (c) escalation is controlled by one
  operator-wide setting; per-tenant control is a deliberate gap.
- **Prefix defaults to empty.** Enabling the current model's documented prefixes changes its embedding space, so the safe
  default is unchanged behavior with an explicit, measured, re-embed-backed opt-in; fresh deployments may adopt the
  recommended values from the example environment file. The harness is what quantifies whether the opt-in is worth it.
- **What "classification for routing" decides.** Two outcomes — *local model suffices* or *stronger model advised* — for the
  user's current message, once per turn. Deciding per tool round, or switching model mid-turn, is out of scope.
- **The shipped head may be inert.** A head needs labelled outcomes from *both* models, which need a configured stronger
  model to produce. This feature delivers the means to produce, evaluate and ship a head, and a head built from
  repository-domain prompts if the build environment can label them; otherwise the router stays at "no advice" until an
  operator produces and accepts one. Either way the out-of-the-box behavior is today's.
- **Stored text is sufficient to re-embed.** It is assumed (to be verified in planning) that each collection's payload
  keeps the passage text, so rebuilding needs no original files; where it does not, that collection is listed with its
  limits.
- **Retrieval-outage degradation** is assumed to already exist and be reusable for an identity mismatch (FR-019); planning
  confirms how it behaves today.
- **Metering of an unpriced alias** follows the existing cost-governance rule; planning reads that document and states the
  exact behavior.
- **Public datasets are candidates, not commitments.** Which are included depends on a licence check done in planning
  (retrieval and reranking sets such as the BEIR collection, prompt-injection sets such as the public Check Point PINT
  example set, intent sets such as BANKING77 and CLINC150 for classifier sanity checks); the prior-art notes in
  `prior-art.md` list what was found and what remains unverified.
- **The harness evaluates; it does not decide.** It never changes a setting (FR-014). Replacing the reranker or the
  input-screening model, choosing a stronger-model vendor, and changing the local chat model are out of scope.
- **Reference hardware** for SC-001, SC-002 and SC-009 is the maintainer's development machine; the container's
  smaller CPU allocation will be slower, and the baselines are keyed by hardware profile for exactly that reason.
- **Suggested delivery boundaries** (planning refines): (1) the constitution amendment, documentation only; (2) the
  harness and its baselines (Story 1); (3) query/document embedding, identity checks and the re-embed procedure
  (Story 2a); (4) the ML service's embedding endpoint (Story 2b); (5) the classification endpoint, heads and the
  gateway alias, and the turn-path seam (Story 3), possibly split at the endpoint/seam boundary.
- **Deliberate gaps, to be disclosed in the docs** (Principle VIII): no per-tenant control of escalation; escalated turns
  send the prompt and retrieved context to whatever the operator attached; the cache is not keyed by serving model; the
  harness does not verify that a stronger model's tool-call quality matches the local model's; the first head's quality
  is bounded by the size and representativeness of repository-domain labelled prompts; the classifier is validated on
  English only.

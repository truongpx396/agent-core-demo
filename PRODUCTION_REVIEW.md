# Production review: findings of 2026-11-13, written as work orders

A review of the whole repository at commit `495b35a` (`main`), asking one question: is this what a professional team
building agents today would ship, and if not, what exactly is missing. This file is the answer and the work that follows
from it. Each finding is a **work order** written so that someone who was not part of the review (a person, or a smaller
model) can implement it without re-deriving the reasoning.

It is a work list, not a pattern document. When a work order is merged, its lesson moves to the pattern file that owns
it (`GRAPH_PATTERNS.md`, `ENGINEERING_PATTERNS.md`, `COST_GOVERNANCE.md`) and its row in the table below is marked done.
When every row is done this file can be deleted, like `AGENT_GRAPH_FIXES.md` before it.

## Contents

1. [Verdict](#1-verdict)
2. [How to use this file](#2-how-to-use-this-file)
3. [The work orders at a glance](#3-the-work-orders-at-a-glance)
4. [P0: correctness, leaks and unbounded growth](#4-p0-correctness-leaks-and-unbounded-growth) (R01 to R07)
5. [P1: scale, security and operability](#5-p1-scale-security-and-operability) (R08 to R21)
6. [P2: smaller items](#6-p2-smaller-items) (R22 to R28)
7. [The cost of a turn, and the code's own structure](#7-the-cost-of-a-turn-and-the-codes-own-structure) (R29 to R35)
8. [Reviewed and deliberately left alone](#8-reviewed-and-deliberately-left-alone)
9. [What this review did not cover](#9-what-this-review-did-not-cover)
10. [Sources](#10-sources)

## 1. Verdict

**The design is sound and does not need re-architecting.** The parts that are hardest to retrofit are already built the
way a careful team builds them, and most are enforced by tests rather than by convention:

- ports and adapters with import-scan tests, business domains as modules, one graph builder for every domain;
- a mandatory human approval gate that fails closed, read-only subagents, tools as closed typed operations;
- exactly-once side effects in layers (a ledger keyed by `tool_call_id`, row-level uniqueness, and crash recovery that
  continues a checkpointed run instead of restarting it);
- tenant isolation inside the store's own predicate, an edge service that verifies tokens and signs a short-lived identity;
- spend metered at one choke point, with holds, caps and a gateway backstop;
- an API tier and a worker tier that scale separately over a queue, a per-conversation lock, submission dedup;
- embedding identity with an atomic alias swap, hybrid retrieval that degrades stage by stage;
- hermetic default tests, real-backend tiers, `mypy --strict`, contract gates on the OpenAPI documents, digest-pinned and
  hardened images with SBOM and provenance.

**What is missing is of a different kind:** the cost of the hot path, things that grow without bound, a few timing
relations that hold only by comment, production settings copied from development, and a dependency ladder that is two
major versions behind. None of it is architectural. Listed by what it would cost you first:

| If you ran this under real load today | Work order |
|---|---|
| A model stream that drops and is retried shows the person the first half of the answer twice | R01 |
| Every worker's memory grows with each delegated (subagent) run and never shrinks | R02 |
| Every vector search builds a new Qdrant client, a new connection and a new thread | R03 |
| Redis fills up: the request streams are never trimmed, and Redis has a 256 MB limit in production | R04, R15 |
| A long turn can outlive its conversation lock or be "reclaimed" while it is still running | R05 |
| A stalled model call has no HTTP timeout of its own, and one provider outage is retried up to 27 times | R06 |
| A Redis blip kills the worker process and every turn it was running | R07 |
| The gateway keeps every prompt and answer in its database, in production, forever | R16 |
| Conversations (checkpoints) are kept forever and nobody can delete one | R11 |

**The turn itself is built for correctness, not for cost.** Every turn runs the same pipeline whatever was asked:
moderation, an answer-cache lookup, routing, and two retrieval searches, one after another, before the model starts;
then a second model call for follow-up suggestions before `done`. Old tool results are re-sent on every later call.
Much of the output checking exists to correct small local models and is pure overhead on a strong one. And the
runtime that turns graph events into stream events recognises nodes by name. Section 7 (R29 to R35) is the work for
these. **Measure first:** R17's first-token and queue-wait metrics say which of these dominates on your traffic.

**Scale, stated honestly.** The production stack is one droplet running Docker Compose with single instances of Postgres,
Redis and Qdrant. That is a reasonable pilot or small-production shape and the code is ready for more (stateless API and
workers, PgBouncer, per-domain queues). It is not highly available: any of the three stores going down takes the service
down, and nothing in this file changes that. Moving the stores to managed services is an operations decision, not a code
change, and is noted in R26.

### How each finding was established

Every work order says how its evidence was obtained, in the same words `GRAPH_PATTERNS.md` uses:

- **reproduced**: a script against the pinned library versions showed the behaviour (the scripts are quoted in the work order);
- **verified in the installed source**: read in the pinned version of the third-party library, not assumed;
- **read from code**: read in this repository; not executed;
- **from the field**: a current practice or upstream change, with a source in section 10. Check it again before acting.

The repository's own test suite was **not run** for this review (section 9). A work order's "Tests" section is therefore
part of the work, not a formality.

## 2. How to use this file

**Before starting any work order**

1. Read `CLAUDE.md` and the constitution (`.specify/memory/constitution.md`). Principles I, II and IV are non-negotiable;
   no work order here asks you to weaken one, and if your change seems to need it, stop and ask.
2. Read the work order to the end, then open every file it names. The code is the truth: this file describes commit
   `495b35a`, and names may have moved since. If a named function is gone, find where it went (`git log --follow`,
   `grep`) before changing anything, and say so in the PR.
3. One work order is one PR, on a topic branch off `main`. Where a work order says "split", each part is its own PR.
   The repository's limit is about 400 hand-written lines per PR, 1,000 at most.

**While doing it**

- Write the regression test first and watch it fail for the reason the work order predicts. A test that passes before
  the fix proves nothing.
- A new setting goes in `app/core/config.py` (`Settings`, plus the module-level constant) and in `.env.example`; a test
  compares the two.
- A new path that degrades and continues needs a metric in `app/core/metrics.py`; if a person could be left unaware of
  it, also an alert in `observability/prometheus/alerts.yml`.
- A new or changed HTTP route or body field needs `make openapi`, a raised `info.version` (`make openapi-version`), and
  its request in `bruno/`. A new route also needs an entry in `services/authlimit/routes.json`
  (`tests/adapters/inbound/api/test_authlimit_routes_cover_api.py` fails otherwise).
- The application ring (`app/agent`, `app/domains`, `app/core`) imports no infrastructure client. Redis, Qdrant,
  psycopg and httpx code belongs in `app/adapters/` or `app/platform/`. `tests/core/test_architecture_layers.py` fails
  the build otherwise.
- A claim about a third-party library is checked against the installed source or a real run. Several items below
  include the check that was made; repeat it if the pinned version has changed.

**Before opening the PR**

- `make lint`, `make typecheck`, `make test`. For a store, queue, worker or SQL change also `make test-integration`
  (needs Docker).
- Update the pattern file named in the work order's "Docs" line, and mark the row in section 3 as done in the same PR.
- The PR body states the failure mode, the root cause, and anything deliberately left undone.

**Sizes used below:** S is under about 150 lines, M is 150 to 400, L needs a spec first (`/speckit-specify`; the next
spec number is 023) and more than one PR.

**Decisions that are the owner's, not the implementer's.** Some work orders contain a line starting **Decide:**. Do not
pick silently. If the owner has not answered, implement the recommended default and say so in the PR.

## 3. The work orders at a glance

| ID | What | Priority | Size | Established | Depends on | Status |
|---|---|---|---|---|---|---|
| R01 | A retried model stream leaves its first attempt's tokens on screen | P0 | S | reproduced | | open |
| R02 | The subagent graph's in-memory checkpointer keeps every run | P0 | S | reproduced | | open |
| R03 | A new Qdrant client, connection and thread per call | P0 | S | verified in the installed source | | open |
| R04 | Request streams are never trimmed; consumer names accumulate | P0 | M | read from code | | open |
| R05 | Request timeout, lock lifetime and reclaim idle are related only by comment | P0 | M | read from code | | open |
| R06 | The chat client has no HTTP timeout; three layers each retry | P0 | S | verified in the installed source | | open |
| R07 | A Redis error in the worker's read loop ends the process | P0 | S | read from code | | open |
| R08 | Three Redis round trips for every token | P1 | S, then M | read from code | | open |
| R09 | No payload index on the fields every vector search filters by | P1 | S | read from code | R03 | open |
| R10 | One HTTP client per ML-service call; the same question embedded up to four times a turn | P1 | M | read from code | | open |
| R11 | Checkpoints are kept forever; no way to delete a conversation | P1 | M | read from code | | open |
| R12 | An approval approves whatever is pending, not what was shown; decisions are not recorded | P1 | L | read from code | | open |
| R13 | A dropped connection loses the rest of the answer; no keepalive | P1 | M | read from code | | open |
| R14 | No size limit on a message, a conversation id or attached images | P1 | S | read from code | | open |
| R15 | Production Redis: one instance for queue and cache, no persistence policy, floating image tags | P1 | M | read from code | R04 | open |
| R16 | The production gateway stores every prompt and answer | P1 | S | read from code | | open |
| R17 | No queue depth, queue wait or first-token metric; no traces in production | P1 | M, then L | read from code | | open |
| R18 | A re-uploaded document leaves its old chunks; no way to delete a document | P1 | M | read from code | R09 | open |
| R19 | The dependency ladder: Langfuse 2, LangChain 0.3, LangGraph 0.2 | P1 | L | from the field | R17 | open |
| R20 | Web fetches: rebinding window, whole body read before the size check, crawler egress | P1 | M | read from code | | open |
| R21 | Five statements that are no longer true | P1 | S | read from code | | open |
| R22 | A hung worker is never restarted | P2 | S | read from code | | open |
| R23 | The answer cache ignores the domain; its expiry is set in a second call | P2 | S | read from code | | open |
| R24 | Two error events carry no `code` | P2 | S | read from code | | open |
| R25 | A pool hands out a connection that died while idle | P2 | S | read from code | | open |
| R26 | Backups are a paragraph, not a job; no restore drill | P2 | M | read from code | | open |
| R27 | A transcript is read whole | P2 | S | read from code | | open |
| R28 | Nothing learns from production answers | P2 | M | from the field | R17 | open |
| R29 | Retrieval runs on every turn, after the router instead of beside it | P1 | M | read from code; reproduced (parallel join) | R17 | open |
| R30 | A second model call for follow-ups holds back `done` | P1 | S | read from code | | open |
| R31 | Old tool results are re-sent to the model on every later call | P1 | S | read from code | | open |
| R32 | Output checks built for small models also run on strong ones | P2 | M | read from code | R17 | open |
| R33 | The answer cache can answer a follow-up from another conversation | P1 | S | read from code | | open |
| R34 | Every change to the history stores the whole history again | P2 | M | reproduced | R19 | open |
| R35 | Code structure: node names in the runtime, repeated entry points, patch-shaped modules | P2 | S to L | read from code; reproduced (custom events) | | open |

Suggested order: R01, R02, R03, R06, R07 (five small, independent fixes), then R04 and R05 (the queue), then R16 and
R21 (two small ones), then R17 part 1 (the measurements), then R33 and R30 and R31 (small, and each cuts work from every
turn), then R29, then the rest in whatever order the owner's priorities set.

**R29 to R33 change what the model is sent or what a turn runs.** The repository's rule applies to each of them: run
`make eval` and `make promptfoo` before and after, and put both results in the PR.

## 4. P0: correctness, leaks and unbounded growth

### R01. A retried model stream leaves its first attempt's tokens on screen

**Established:** reproduced against `langgraph==0.2.76` and `langchain-core==0.3.86`.

**Problem.** The `agent` node has a retry policy (`app/agent/graph/core.py::AGENT_RETRY_POLICY`, three attempts).
`app/agent/runtime/stream.py::_run_graph_stream` forwards each `on_chat_model_stream` chunk to the client as a `token`
event the moment it arrives. When the model's stream fails part-way (a dropped connection, a 5xx mid-stream) LangGraph
runs the node again and the second attempt's tokens are appended after the first attempt's. The stored message is
correct; what the person read is not. The OpenAI SDK's own retries do not cover this case: they retry opening a
request, not a stream that broke.

The reproduction (a fake chat model whose first stream raises `ConnectionError` after two chunks, in a node with
`RetryPolicy(max_attempts=3)`, read through `astream_events(version="v2")`):

```
model calls: 2
streamed to client: 'The answer The answer is 42.'
stored answer     : 'The answer is 42.'
```

**Change.** In `_run_graph_stream`, recognise a restarted model call and tell the client to discard, with the `retry`
event the client already understands.

A failed model call emits `on_chat_model_start`, some `on_chat_model_stream`, and **no** `on_chat_model_end`. A call
that ends normally always emits `on_chat_model_end`. So: a model call on the `agent` node that starts while the previous
one is still open, and that previous one streamed something, is a retry. Checked against both sequences:

```
failed then retried : start stream stream start stream stream stream stream end   -> one retry event, then the answer once
two normal calls    : start stream ... end start stream ... end                    -> no retry event
```

Add two locals next to `final_answer` and two branches to the event loop. Apply the same two guards the token branch
already uses (`metadata.langgraph_node == "agent"` and no `metadata.subagent_name`):

```python
model_call_open = False       # an agent model call has started and not ended
open_call_streamed = False    # ...and it has streamed at least one token

# on_chat_model_start (agent node, not a subagent):
if model_call_open and open_call_streamed:
    final_answer.clear()
    metrics.agent_stream_restarted_total.inc()
    yield {"type": "retry"}
model_call_open, open_call_streamed = True, False

# on_chat_model_stream (the existing branch): also set open_call_streamed = True when content is non-empty

# on_chat_model_end (agent node, not a subagent):
model_call_open = False
```

Add the counter `agent_stream_restarted_total` to `app/core/metrics.py` ("a model stream failed after sending tokens and
was retried").

**Tests.** In `tests/agent/runtime/test_streaming_terminal_events.py`, add
`test_a_model_stream_that_fails_midway_and_is_retried_does_not_show_its_first_attempt`: build a graph with a fake chat
model that fails its first stream after two chunks (`GraphDeps(llm=...)`), collect the events, and assert that the
tokens after the last `retry` event join to exactly the stored answer, and that exactly one `retry` was emitted. Add a
second test that an ordinary tool loop (two model calls that both end) emits no `retry`.

**Docs.** A "Bug:" bullet under pattern 7 (error recovery) in `GRAPH_PATTERNS.md`. The `RetryEvent` docstring in
`app/adapters/inbound/api/events.py` gains "or the model's stream was restarted". No new event type, so the OpenAPI
schema does not change; run `make openapi` to confirm only the description moved.

**Do not** remove the retry policy or buffer the whole answer before sending it; streaming is the product.

### R02. The subagent graph's in-memory checkpointer keeps every run

**Established:** reproduced against `langgraph==0.2.76` (the library behaviour); the application path is read from code.

**Problem.** `app/agent/tools/run_subagent.py::run_subagent_impl` caches one compiled nested graph per
`(domain, subagent)` in `_subagent_graph_cache`. `app/agent/graph/build.py::build_graph` compiles with
`checkpointer or MemorySaver()`, so each cached graph owns a `MemorySaver` that lives as long as the process. Every
delegated run uses a fresh `nested_thread_id` (it contains a `uuid4`), and nothing ever removes a thread from that
saver. Each run therefore leaves its checkpoints (the task, the retrieved context, every message) in the worker's
memory for good. Reproduced with a graph compiled once and invoked under 200 new thread ids:

```
threads held by the MemorySaver after 200 one-shot runs: 201
```

The agent worker has a 768 MB limit in production, so this ends in an out-of-memory kill whose frequency depends on how
much the subagents are used.

**Change.** Delete the run's thread when the run is over. `MemorySaver.adelete_thread` exists in the pinned
`langgraph-checkpoint==2.1.2` (verified). In `run_subagent_impl`, wrap the `arun_with_timeout(nested_graph.ainvoke, ...)`
call so that every exit (success, timeout, error) runs:

```python
finally:
    await nested_graph.checkpointer.adelete_thread(nested_thread_id)
```

Keep the existing `except TimeoutError` / `except Exception` metric branches as they are; the `finally` goes around
them. Read `result_state` before the delete (it is the return value of `ainvoke`, not a later read of the saver, so the
order is already safe).

**Tests.** In `tests/agent/tools/test_subagents.py`:
`test_a_finished_subagent_run_leaves_nothing_in_its_checkpointer`: run the same cached subagent three times
(`use_cache=True`, then `reset_subagent_graph_cache()` in teardown) and assert the cached graph's
`checkpointer.storage` is empty afterwards. Add the same assertion for a run that times out.

**Docs.** A "Bug:" bullet under pattern 46 in `GRAPH_PATTERNS.md`.

**Do not** compile the nested graph with no checkpointer: `human_approval` is kept in that graph as defence in depth and
an `interrupt()` without a checkpointer raises instead of pausing.

### R03. A new Qdrant client, connection and thread per call

**Established:** verified in the installed source (`qdrant-client==1.19.0`).

**Problem.** `app/adapters/outbound/qdrant/qdrant_store.py::get_client` is:

```python
def get_client() -> AsyncQdrantClient:
    return AsyncQdrantClient(url=QDRANT_URL)
```

and it is called for every operation (eleven call sites). In 1.19.0, constructing the client with the default
`check_compatibility=True` starts a daemon `Thread` that makes a synchronous `httpx.get` to the server
(`AsyncQdrantRemote.__init__`, `qdrant_client/common/version_check.py::get_server_version`), and builds a new HTTP
connection pool. So every search costs a thread, an extra request, a new TCP connection, and a client that is never
closed. One turn makes at least two searches (documents and memories, in parallel), more when the model calls
`search_docs`.

**Change.** One client per event loop, closed at shutdown.

- Keep the name and signature of `get_client()` (tests and `scripts/tools/reembed.py` call it). Make it return a cached
  client for the running loop. Key the cache by loop, because a connection belongs to the loop that opened it and the
  test suite and the one-shot scripts run more than one loop per process:

  ```python
  _clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncQdrantClient]" = weakref.WeakKeyDictionary()

  def get_client() -> AsyncQdrantClient:
      loop = asyncio.get_running_loop()
      client = _clients.get(loop)
      if client is None:
          client = _clients[loop] = AsyncQdrantClient(url=QDRANT_URL)
      return client

  async def close_client() -> None:
      client = _clients.pop(asyncio.get_running_loop(), None)
      if client is not None:
          await client.close()
  ```

- Call `close_client()` from `app/bootstrap.py::close_adapters`, which every long-lived entrypoint already awaits on its
  own loop at shutdown.
- `get_client()` now needs a running loop. Every call site is already inside `async def`; confirm with
  `grep -n "get_client()" app scripts`.

**Tests.** In `tests/adapters/outbound/qdrant/test_qdrant_store.py`:
`test_the_client_is_built_once_per_loop_not_once_per_call` (two calls return the same object; patch
`AsyncQdrantClient` with a counting stand-in so no network is touched) and
`test_close_client_closes_and_forgets_it`. Check that the autouse guard `no_real_qdrant` in `tests/conftest.py` still
does what it did.

**Docs.** One sentence in pattern 20's notes in `GRAPH_PATTERNS.md`.

### R04. Request streams are never trimmed; consumer names accumulate

**Established:** read from code.

**Problem.** A job is added with `client.xadd(requests_stream_key(domain), {"payload": payload})`
(`app/adapters/outbound/redis/queue.py::publish_request`, `publish_resume_request`, `publish_cancel_request`,
`app/adapters/inbound/workers/streams.py::republish_job`, and `app/adapters/outbound/redis/ingest_queue.py` for
uploads). A worker acknowledges it with `XACK`, which removes it from the group's pending list and **not** from the
stream. Nothing calls `XDEL` or `XTRIM` on a requests stream (the only `maxlen` in the code is the dead-letter stream's).
Every message ever sent, with its text and any attached images as data URIs, stays in Redis. Production Redis has
`mem_limit: 256m`.

Separately, each worker start registers a new consumer name (`CONSUMER_NAME` ends in a random suffix) and never removes
it, so the group's consumer list grows by one per restart.

**Change.**

1. In `app/adapters/inbound/workers/streams.py` add:

   ```python
   async def ack_and_delete(client: redis.Redis, stream: str, group: str, entry_id: str) -> None:
       """Acknowledge a finished job and remove it from the stream. XACK alone leaves the entry there for good."""
       async with client.pipeline(transaction=True) as pipe:
           pipe.xack(stream, group, entry_id)
           pipe.xdel(stream, entry_id)
           await pipe.execute()
   ```

   Replace each of the six `client.xack(...)` calls with it: three in
   `app/adapters/inbound/workers/agent_worker.py` (`process_request`, `_handle_reclaimed_job`, `_dead_letter_reclaimed`)
   and three in `app/adapters/inbound/workers/ingest_worker.py`.

2. A safety net for entries that were acknowledged before this change, or by a process that died between the two
   commands. In each worker's reclaim loop, once per pass per stream:
   `await client.xtrim(stream, minid=<now minus REQUESTS_STREAM_MAX_AGE_SECONDS, as a millisecond id>, approximate=True)`.
   New setting `requests_stream_max_age_seconds`, default `86400`. An entry that old was never going to be served: the
   API stops waiting for a first event after `CHAT_FIRST_RESPONSE_DEADLINE_SECONDS` (30 s). Say so in the setting's
   comment.

3. On graceful shutdown in `agent_worker.py::run` and the ingest worker's equivalent, after the in-flight jobs have
   finished: `await client.xgroup_delconsumer(stream, CONSUMER_GROUP, CONSUMER_NAME)` for each stream served.

**Pitfalls.**
- Do **not** bound the stream with `XADD ... MAXLEN`. Under a backlog it deletes jobs nobody has run yet.
- `XGROUP DELCONSUMER` discards the consumer's pending entries. Call it only after `in_flight` has drained. For names
  left by crashed workers, a later pass may delete a consumer only when `xinfo_consumers` shows `pending == 0` and it
  has been idle for more than a day; this part is optional.
- The reclaim path re-adds a job with a new entry id (`republish_job`) and then acknowledges the old one. Keep that
  order.

**Tests.** `tests/adapters/inbound/workers/test_agent_worker.py` and `test_ingest_worker.py`: after a job is processed
the entry is gone from the stream (extend the fake Redis the worker tests use so it implements `xdel`, `xtrim` and
`pipeline`). In `tests/integration/test_queue_real_redis.py`:
`test_a_processed_job_is_removed_from_the_requests_stream_on_real_redis` (after `process_request`, `XLEN` is 0 and the
pending list is empty) and `test_reclaim_still_finds_an_unacknowledged_job_after_trimming`.

**Docs.** A "Bug:" bullet under pattern 43 in `GRAPH_PATTERNS.md`, and the new setting in `WORKER_CONCURRENCY.md`.

### R05. Request timeout, lock lifetime and reclaim idle are related only by comment

**Established:** read from code; the arithmetic is below.

**Problem.** Three numbers must stay in order for the queue to be correct, and only comments keep them there:

| Number | Where | Default |
|---|---|---|
| a turn's wall-clock limit | `request_timeout_seconds` | 60 s |
| the conversation lock's lifetime | `app/ports/outbound/jobs.py::THREAD_LOCK_TTL_SECONDS` = 2 x the above | 120 s |
| idle time after which a job is presumed abandoned | `agent_worker_reclaim_idle_seconds` | 240 s, a fixed default |

Three ways this breaks:

1. **An operator raises `REQUEST_TIMEOUT_SECONDS`** (a slower model: 300 s is ordinary). The lock follows (600 s); the
   reclaim idle does not (240 s). A healthy turn is then "reclaimed" after four minutes, and
   `agent_worker.py::_classify_reclaimed_turn` republishes it as `turn_continue` while the original is still running.
   Nothing refuses that configuration at start-up.
2. **The lock is never renewed.** An unattended turn runs up to `1 + UNATTENDED_MAX_DECLINE_ROUNDS` streams
   (`app/agent/runtime/stream.py::_decline_every_pause`), each with its own 60 s limit: 240 s under a 120 s lock. After
   the lock expires a second job for the same conversation can take it while the first is still writing checkpoints,
   which is the fork the lock exists to prevent. With the defaults, 240 s is also exactly the reclaim idle.
3. **A worker claims jobs it cannot start.** `agent_worker.py::run` asks `XREADGROUP` for up to
   `AGENT_WORKER_MAX_CONCURRENCY` entries and then waits on the semaphore for each. A claimed entry's idle clock runs
   while it waits for a slot, so under saturation "idle" measures the queue, not a dead worker.

**Change.** Three parts; one PR if it stays under the size limit, otherwise (a)+(b) then (c).

(a) **Refuse a configuration that cannot work.** A `model_validator` on `Settings`:

```python
longest_job = self.request_timeout_seconds * (1 + self.unattended_max_decline_rounds)
if self.agent_worker_reclaim_idle_seconds < longest_job + 30:
    raise ValueError("AGENT_WORKER_RECLAIM_IDLE_SECONDS must exceed REQUEST_TIMEOUT_SECONDS x "
                     "(1 + UNATTENDED_MAX_DECLINE_ROUNDS) by at least 30 s: ...")
```

Raise the default of `agent_worker_reclaim_idle_seconds` from 240 to 300 so the shipped defaults pass (60 x 4 + 30 =
270). The message names variables, never values (pattern 59). Check `tests/live/conftest.py` and
`tests/integration/test_worker_scaling.py`, which override the request timeout for slow CI models: they must set the
reclaim idle to match.

(b) **Renew the lock while the job runs.** In `streams.py` add `renew_thread_lock(client, thread_id, token)`: a Lua
script like `_RELEASE_LOCK_SCRIPT` that calls `PEXPIRE` only when the stored value equals `token`. In
`agent_worker.py::process_request`, after the lock is acquired, start a task that renews every
`THREAD_LOCK_TTL_SECONDS / 3` seconds, and cancel it in the same `finally` that releases the lock (cancel first, then
release). If a renewal finds the lock is no longer ours, log it and count `agent_thread_lock_lost_total`; do not kill
the turn. With renewal in place the lifetime no longer has to outlast the longest turn, so set
`THREAD_LOCK_TTL_SECONDS = 60` as a constant: a crashed worker's lock then frees after a minute instead of two, and the
comment in `_handle_reclaimed_job` ("by the time a job is reclaimed its lock has already self-expired") stays true
with room to spare. `app/agent/cost/budget_holds.py` refers to the old value in a comment; read it and correct it.

(c) **Read only what can start.** In `run()`, wait for one free slot, count the free slots, and ask for that many:

```python
await semaphore.acquire()                    # at least one slot is free
free = 1
while free < _MAX_CONCURRENCY and not semaphore.locked():
    await semaphore.acquire(); free += 1     # take the other free slots without waiting
response = await client.xreadgroup(..., count=max(1, free // len(streams)), block=_BLOCK_MS)
taken = sum(len(entries) for _stream, entries in response or [])
for _ in range(max(0, free - taken)):
    semaphore.release()                      # give back what this read did not use
# Start one task per entry. The first `free` entries use the slots taken above, so do NOT acquire again for them;
# each task releases its slot when it ends, as today (`_process_with_limit`).
# COUNT is per stream, so with several streams a read can return more than `free`:
# for each entry beyond `free`, acquire a slot before starting its task, as the loop does today.
```

`asyncio.Semaphore.locked()` is true when no slot is free, so the inner loop never blocks. If the stop event is set
while the loop holds slots it did not use, release them before leaving. Do the same in `ingest_worker.py`.

**Tests.**
- `tests/core/` settings test: `REQUEST_TIMEOUT_SECONDS=300` with the default reclaim idle fails to load, and the error
  text names the variables and contains no value.
- `tests/adapters/inbound/workers/test_agent_worker.py`: `test_the_thread_lock_is_renewed_while_a_turn_runs` (a fake
  clock or a short TTL; the key still exists after more than one TTL) and
  `test_a_worker_with_no_free_slot_does_not_read_from_the_queue`.
- `tests/integration/test_worker_scaling.py` must still pass; it is the test that measured the lock-release ordering.

**Docs.** A new numbered pattern in `GRAPH_PATTERNS.md` ("three timeouts that must stay in order are a validator, not a
comment") and the sizing section of `WORKER_CONCURRENCY.md`.

### R06. The chat client has no HTTP timeout; three layers each retry

**Established:** verified in the installed source (`langchain-openai==0.3.35`, `openai==2.54.0`); the gateway's values
are read from `deploy/litellm/litellm-config.prod.yaml`.

**Problem.** `app/adapters/outbound/litellm/chat_model.py::OpenAiChatModels` builds
`ChatOpenAI(model=..., base_url=..., api_key=..., temperature=0, stream_usage=True)`. With no `timeout` and no
`max_retries`:

```
ChatOpenAI defaults -> request_timeout: None | max_retries: None
openai client      -> timeout: None        | max_retries: 2
```

- **No timeout at the HTTP layer.** Inside a turn the 60 s wall-clock limit in `_iterate_with_timeout` still ends it,
  but a stalled call uses the whole turn, so the retry policy never gets a second attempt. Outside a turn (the cron
  jobs in `scripts/jobs/` that call the model) nothing bounds the call at all.
- **Retries multiply.** The SDK retries twice, the graph's `AGENT_RETRY_POLICY` makes three attempts, and the gateway
  has `num_retries: 2`. One logical call can reach the provider 3 x 3 x 3 = 27 times during an outage, which is how a
  brownout becomes a retry storm. The gateway's own `request_timeout: 600` also lets it keep working on a request for
  ten minutes after the application gave up at 60 s.

**Change.**

1. Two settings: `llm_read_timeout_seconds: float | None = None` and `llm_sdk_max_retries: int = 0`. `None` means
   "the same as `request_timeout_seconds`", so nothing changes for a turn until an operator lowers it; a validator
   refuses a value above `request_timeout_seconds`. The cron jobs that call the model outside a turn
   (`scripts/jobs/ops_digest.py`, `scripts/jobs/followup_sweep.py`) get a bound for the first time.
2. In `OpenAiChatModels.build`, pass
   `timeout=httpx.Timeout(connect=5.0, read=<llm_read_timeout_seconds>, write=10.0, pool=5.0)` and
   `max_retries=<llm_sdk_max_retries>`. On a streamed response the read timeout is the longest silence between chunks,
   not the length of the answer, which is the right meaning: "no bytes for N seconds is a stall". The retry that
   remains in the application is the graph's, which already knows what must not be retried (`_retry_agent_on`).
3. In both `deploy/litellm/*.yaml`: `request_timeout` to 120, and a comment that the three layers were counted. Leave
   `num_retries: 2`: the gateway is closest to the provider and sees `Retry-After`.
4. `.env.example` and `deploy/env/prod.env.example`: for a hosted model, suggest `LLM_READ_TIMEOUT_SECONDS=30`, which
   leaves room for one retry inside a 60 s turn. A cold local model can take longer than that to send its first token,
   which is why the default is not 30.

**Tests.** `tests/adapters/outbound/litellm/`: `test_the_chat_client_has_a_read_timeout_and_no_sdk_retries` (build a
client, read `root_async_client.timeout` and `.max_retries`). A settings test for the validator.

**Docs.** Pattern 7 in `GRAPH_PATTERNS.md`: which layer retries what, and the product of the three.

**Decide:** whether to add a fallback model at the gateway (`fallbacks:` in the LiteLLM config). It is listed under
"Not built" today. If added, it must be priced, and the app records the alias that answered; see the note in
`app/agent/graph/agent_node.py` about why the routed tiers do not use gateway fallbacks.

### R07. A Redis error in the worker's read loop ends the process

**Established:** read from code.

**Problem.** In `agent_worker.py::run` the main loop awaits `client.xreadgroup(...)` with nothing around it, and
`app/platform/redis_client.py::get_client` builds the client with no retry. A Redis restart or a dropped connection
raises out of `run()`. `asyncio.run` then cancels every in-flight turn, the container restarts, and each of those
turns waits out the reclaim idle (minutes) before it is continued. The API side has the same shape:
`app/adapters/outbound/redis/queue.py::read_results` does a bare `XREAD`, so the same blip ends every open SSE response.

**Change.**

1. Around the `xreadgroup` call in both workers, catch `redis.ConnectionError`, `redis.TimeoutError` and `OSError`:
   log (class only), count `agent_worker_queue_read_failed_total{queue}`, sleep with a capped backoff (0.5 s doubling
   to 5 s, reset after a successful read), and continue the loop. The stop event must still end the loop during a
   backoff: wait on `stop_event` with a timeout instead of `asyncio.sleep`.
2. In `read_results`, retry the `XREAD` itself up to three times with a short pause before giving up. It is safe: the
   read is by `last_id` and changes nothing.
3. An alert on `increase(agent_worker_queue_read_failed_total[5m]) > 0` in `observability/prometheus/alerts.yml`.

**Do not** turn on redis-py's client-wide retry (`retry=Retry(...)`, `retry_on_error=[...]`) for this shared client.
It would also re-send an `XADD` whose connection dropped after the server applied it, and a second copy of a `turn` job
runs as a second turn once the first has finished. Retrying is safe for reads only; `XREADGROUP` with `>` is safe
because an entry delivered but lost on the wire stays pending and is reclaimed.

**Tests.** `test_agent_worker.py`: `test_a_redis_error_while_reading_the_queue_is_survived_and_counted` (the fake raises
`ConnectionError` once, then returns a job; the job is processed). `tests/adapters/outbound/redis/test_queue.py`:
`test_read_results_survives_one_failed_read`.

**Docs.** Pattern 43 in `GRAPH_PATTERNS.md`; `.claude/rules/runtime-reliability.md`, "Queue and workers".

## 5. P1: scale, security and operability

### R08. Three Redis round trips for every token

**Established:** read from code.

**Problem.** For each event of a turn the worker awaits, one after another:
`cancel_check()` (a `GET`, in `stream.py::_iterate_with_timeout`), then in `streams.py::publish_result` an `XADD` and
an `EXPIRE`. An answer of 500 tokens is 1,500 sequential round trips, and the model's stream is held back by each one.

**Change, step 1 (S, do this).**
- `publish_result`: send `XADD` and `EXPIRE` in one pipeline (`client.pipeline(transaction=False)`), one round trip.
  It also closes a small leak: today a worker that dies between the two leaves a results stream with no expiry.
- `_iterate_with_timeout`: poll `cancel_check` at most every 0.25 s (keep a `last_checked` monotonic time; the first
  iteration always checks). The docstring of `astream_events_resume` relies on the check running before the first
  event; keep that. A stop now takes effect within a quarter of a second, at an event boundary, as before.

After step 1: one round trip per event instead of three.

**Step 2 (M, only with a measurement).** Batch what is waiting. Put events on an `asyncio.Queue` in
`agent_worker.py::_publish_events`; a publisher task takes one event, then everything else already queued
(`get_nowait` until empty), joins adjacent `token` events into one, and writes the batch in one pipeline. No timer, so
nothing is delayed when Redis keeps up. Two rules must survive: `release_lock()` is awaited before a batch that
contains a terminal event is written (see the measurements in `process_request`'s docstring), and a failed publish
closes the event generator (`aclosing`) exactly as today. Measure before and after with `make loadtest-queued-headless`
and put both numbers in the PR; if the gain is small, do not merge step 2.

**Tests.** `tests/agent/runtime/test_cancellation.py`: the check is polled once per interval, not once per event, and
still before the first event. `tests/adapters/inbound/workers/`: one pipeline execution per published event.

**Docs.** `WORKER_CONCURRENCY.md` (a paragraph on per-token cost).

### R09. No payload index on the fields every vector search filters by

**Established:** read from code (no `create_payload_index` anywhere in `app/` or `scripts/`).

**Problem.** Every tenant search filters on `tenant` and `kind`, a memory search also on `owner` and a `created_at`
range, and a topic search on `topic` (`qdrant_store.py::scope_filter`, `_build_filter`). The collection is created
with vectors only (`create_physical`). Without payload indexes Qdrant evaluates the filter by reading payloads and its
filtered vector search degrades as the collection grows; for many tenants in one collection, Qdrant's own guidance is
a keyword index on the tenant field marked `is_tenant`, which also groups each tenant's data on disk.

**Change.**
- In `qdrant_store.py` add `ensure_payload_indexes(physical: str)`: `create_payload_index` for
  `tenant` (`KeywordIndexParams(type="keyword", is_tenant=True)`), `kind`, `owner`, `topic` (keyword), and `created_at`
  (`PayloadSchemaType.DATETIME`). Names verified in `qdrant-client==1.19.0`. Creating an index that exists is not an
  error; creating one on a live collection is an online operation.
- Call it at the end of `create_physical`, so `make ingest` and `make reembed` produce indexed collections.
- For a collection that already exists: a `--indexes-only` flag on `scripts/tools/reembed.py`, or a small new script
  `scripts/tools/qdrant_indexes.py` (NEW) with a `make qdrant-indexes COLLECTION_NAME=docs` target. State in
  `deploy/README.md` that an existing deployment runs it once.

**Tests.** `tests/adapters/outbound/qdrant/test_qdrant_store.py`: `create_physical` requests each index (the fake
records the calls).

Also write the test the code already claims to have. The docstring of `qdrant_store.py::scope_filter` says
`tests/integration/test_tenant_isolation_real_qdrant.py` "proves the result against a real Qdrant". That file does not
exist (NEW), so the translation Principle I rests on is pinned only against a fake. Create it in the `integration`
tier, with no model: build a collection with `create_physical`, upsert points for two tenants and two owners with
fixed small vectors (`build_point`), and query with `client.query_points(..., query_filter=scope_filter(scope))`.
Assert that a tenant sees only its own points, that a memory scope sees only its owner's, that a point without
`created_at` is excluded by a retention cutoff, and that the collection's `payload_schema` lists `tenant`. Use
`tests/containers.py::ensure_qdrant`; it must self-skip without Docker.

**Docs.** Pattern 17 or 20 in `GRAPH_PATTERNS.md`.

### R10. One HTTP client per ML-service call; the same question embedded up to four times a turn

**Established:** read from code.

**Problem.**
- `app/adapters/outbound/embeddings/embeddings.py::rerank`, `app/adapters/outbound/mlservice/prompt_guard.py` and
  `app/adapters/outbound/mlservice/classification.py` each open `httpx.AsyncClient(...)` inside the call: a new TCP
  connection for the moderation check, the routing decision and each rerank of every turn. The comment in `rerank`
  explains it by calls arriving from different event loops (tools that ran in worker threads). No tool does that any
  more: every tool body awaits `arun_with_timeout` on the turn's own loop, and nothing in `app/` calls the synchronous
  `app/core/tool_runtime.py::run_with_timeout`. Confirm with `grep -rn "[^a]run_with_timeout(" app` before relying on it.
- A turn embeds the person's question in `check_semantic_cache`, again in each of the two searches of
  `app/agent/tools/builtin.py::gather_context` (documents and memories, which run at the same time), and again in
  `write_semantic_cache`. That is up to four identical calls to the embedding model where one would do.

**Change.**
1. A shared client per event loop for the ML service, in a new module
   `app/adapters/outbound/mlservice/http.py` (NEW): `ml_client() -> httpx.AsyncClient` with the same per-loop cache as
   R03, limits `httpx.Limits(max_keepalive_connections=20, max_connections=100)`, and `close_ml_client()` called from
   `app/bootstrap.py::close_adapters`. The three call sites keep their own per-request `timeout=` argument
   (`client.post(..., timeout=...)`).
2. In `embeddings.py::embed_query`, a small cache with single-flight: at most 512 entries, 300 s lifetime, keyed by the
   exact text sent (prefix included). Two concurrent calls for the same text share one request:

   ```python
   _query_cache: "OrderedDict[str, tuple[float, list[float]]]" = OrderedDict()
   _in_flight: dict[str, asyncio.Future[list[float]]] = {}
   ```

   Provide `reset_embed_query_cache()` and call it from the test fixtures that already reset the embedding dimension
   (`reset_embedding_dim_cache`). The cached vector is only as valid as the embedding identity; the identity changes
   only with configuration, which needs a restart, so no extra key is needed. Say that in the comment.

**Tests.** `tests/adapters/outbound/embeddings/`: `test_the_same_question_is_embedded_once_within_a_turn` (count calls
on the underlying client through `gather_context`-like concurrent use) and
`test_two_concurrent_identical_queries_share_one_call`. `tests/adapters/outbound/mlservice/`: the client is reused.
`tests/adapters/outbound/embeddings/test_embedding_roles.py` must still pass (no new caller of the raw client).

**Docs.** Pattern 20 in `GRAPH_PATTERNS.md`.

### R11. Checkpoints are kept forever; no way to delete a conversation

**Established:** read from code; `AsyncPostgresSaver.adelete_thread` verified in `langgraph-checkpoint-postgres==2.0.25`.

**Problem.** Every step of every turn writes a checkpoint, and each change to `messages` stores the list again, so a
long conversation costs far more than its text. Nothing deletes any of it: there is a sweep for the tool-call ledger
and for usage events, none for checkpoints or for `chat_sessions`. There is also no way for a person (or an operator
answering a deletion request) to remove a conversation: the API has no delete route.

**Change.** Two PRs.

*PR 1: retention.*
- Port: `app/ports/outbound/checkpointing.py` gains `async def delete_thread(self, thread_id: str) -> None`; the
  adapter (`app/adapters/outbound/postgres/checkpointing.py::PgCheckpointing`) keeps the saver it opened and calls
  `adelete_thread`. `SessionStore` (`app/ports/outbound/sessions.py`, adapter
  `app/adapters/outbound/postgres/sessions.py`) gains `list_inactive(before: datetime, limit: int)` and
  `delete(thread_id: str)`. Update the fakes in `tests/fakes/` (the conformance test compares them to the Protocols).
- Setting `checkpoint_retention_days: int = 0` (0 is off, so nothing is deleted until an operator chooses a number).
- Job `scripts/jobs/checkpoint_sweep.py` (NEW), modelled on `scripts/jobs/tool_call_dedup_sweep.py`: in batches, for
  each conversation whose `last_active_at` is older than the retention, delete the checkpoints first and the
  `chat_sessions` row second (a crash between the two leaves an empty conversation that the next run removes). It
  supports `--dry-run`, counts `agent_checkpoint_sweep_deleted_total`, and calls `wire()` first
  (`tests/core/test_entrypoints_wire.py`). Make target `checkpoint-sweep` in `make/admin.mk`.
- `adelete_thread` uses a psycopg pipeline; run the integration test through PgBouncer
  (`tests/integration/test_pgbouncer.py` shows how).

*PR 2: deletion by the owner.*
- `DELETE /chat/sessions/{thread_id}` in `app/adapters/inbound/api/routers/chat.py`: `sessions.session_belongs_to`
  first (404 otherwise, as the GET routes), refuse with 409 and `ErrorCode.THREAD_BUSY` while a turn holds the
  conversation's lock, then the same two deletes. Bruno request, `make openapi`, version, `routes.json`.

**Not in scope, say so in the PR:** trimming old checkpoints *inside* a live conversation (it needs the saver's schema
and an integration test of its own), the copies in the gateway's spend logs (R16) and in Langfuse.

**Tests.** `tests/agent/runtime/test_sessions.py`, a new `tests/scripts/` test for the sweep (dry run deletes nothing;
a conversation newer than the retention is kept), and an `integration` test against real Postgres that a deleted
thread's rows are gone from `checkpoints`, `checkpoint_blobs` and `checkpoint_writes`.

**Docs.** "Known gaps" in `GRAPH_PATTERNS.md` already says nothing schedules the other sweeps; add this one to the same
sentence and to the README's list. `deploy/README.md`: the cron line.

### R12. An approval approves whatever is pending, not what was shown; decisions are not recorded

**Established:** read from code. The second half is already listed under "Known gaps" ("Approvals are unattributed").

**Problem.** `ResumeRequest` is `{thread_id, approved}`. The `approval_required` event and
`GET /chat/sessions/{thread_id}/pending_approval` list each pending call as `{name, args}` with no identifier
(`app/agent/graph/hitl.py::make_human_approval_node`). So a client cannot say *which* calls it approves, and the server
cannot check. A stale approve (a second tab, a retry of the request, a click that arrives after the conversation was
cancelled and paused again on a different action) approves a call the person never saw. The Telegram channel already
binds a button to one pause (pattern 92); the HTTP API does not. And no record is kept of who decided what.

**Change.** This is L: write a spec first (`/speckit-specify`). Its content:

1. **Identify the calls.** The interrupt payload and both read paths carry each call's `id` (the provider's
   `tool_call_id`). For a conversation paused before this change the payload has no ids; read them from the last
   `AIMessage`'s `tool_calls` in the checkpoint instead (`paused_approval_async` already has the state). No
   `STATE_SCHEMA_VERSION` bump: no `State` key changes.
2. **Bind the decision.** `ResumeRequest` gains `tool_call_ids: list[str] | None`. It travels in the job
   (`TurnQueue.publish_resume_request`, the Redis adapter, the fakes) to
   `app/agent/runtime/stream.py::astream_events_resume(expected_tool_call_ids=...)`. After `resumability_error_async`
   and before `Command(resume=...)`, compare as sets with what is pending. A mismatch ends with a new
   `ErrorCode.APPROVAL_STALE` and leaves the conversation paused. `None` keeps today's behaviour for one release so old
   clients work; the built-in web page and the CLI send the ids from the event they displayed.
3. **Record the decision.** A table `approval_decisions` (`postgres-init/26-approval-decisions.sql`, NEW): `tenant`,
   `principal` (who decided), `thread_id`, `domain`, `decision` (approved, rejected, cancelled, declined_unattended),
   `tool_calls` as JSON of names and ids (not the arguments, which can hold personal data), `decided_at`. Written from
   the worker when it processes a resume or a cancel, through a new port.

**Decide:** if the record cannot be written, does the approval still go through? The recommended default follows the
rest of the repository: continue, count `agent_approval_audit_failed_total`, and alert (a person would otherwise be
unaware that a committed action has no record). A regulated deployment would choose to refuse.

**Constitution.** Principle II is strengthened, not changed: nothing here can approve without a person. State it in
the plan's Constitution Check.

### R13. A dropped connection loses the rest of the answer; no keepalive

**Established:** read from code.

**Problem.** The response to `POST /chat/stream/queued` is the only way to read a turn. The turn itself runs on a
worker and its events sit in a Redis stream for five minutes, each with an id, but the client is never told the
request id and there is no route to read from a position. A phone that changes network mid-answer loses the rest, and
the turn still runs and is still paid for. Also, while a tool runs (up to 15 s, a subagent 45 s) or while waiting for a
worker (up to 30 s) the response sends no bytes at all, which some proxies and load balancers treat as dead.

**Change.**
1. In `app/adapters/inbound/api/routers/chat.py::_queued_sse_response`: add the response header `X-Request-Id`, write
   each frame as `id: <redis entry id>\ndata: <json>\n\n` (the port's `read_results` must yield the entry id with each
   event; change the Protocol in `app/ports/outbound/jobs.py`, the adapter and the fakes together), and send the SSE
   comment `: keepalive\n\n` when 15 s pass with no event. Do not cancel the pending read to do it:

   ```python
   pending = asyncio.ensure_future(anext(events))
   try:
       while True:
           done, _ = await asyncio.wait({pending}, timeout=15)
           if not done:
               yield ": keepalive\n\n"
               continue
           try:
               entry_id, event = pending.result()
           except StopAsyncIteration:
               break
           yield f"id: {entry_id}\ndata: {json.dumps(event)}\n\n"
           pending = asyncio.ensure_future(anext(events))
   finally:
       pending.cancel()
   ```

2. `GET /chat/stream/{request_id}`: reads the same results stream from the `Last-Event-ID` header (or from the start).
   Authorisation: when a job is published, store `agent:request_thread:{request_id}` = the thread id with the results
   stream's lifetime; the route looks it up, then applies `_require_conversation_owner` to that thread. An unknown or
   expired id is a 404.
3. `app/adapters/inbound/api/events.py` says a frame has no `id:` field; correct it. Bruno, `make openapi`, version,
   `routes.json`.

Teaching the web page and the CLI to reconnect is a follow-up; say so in the PR. Clients that ignore `id:` lines and
comments (all standard SSE parsers) are unaffected; check the two in this repository:
`app/adapters/inbound/channels/door.py` and the page's script in `app/adapters/inbound/api/static/`.

**Tests.** `tests/adapters/inbound/api/test_api.py`: frames carry ids; a keepalive appears when the queue is silent;
reading with `Last-Event-ID` returns only later events; another person's request id is a 404.

**Docs.** Pattern 43 in `GRAPH_PATTERNS.md`; the HTTP API section of `README.md`.

### R14. No size limit on a message, a conversation id or attached images

**Established:** read from code.

**Problem.** `app/adapters/inbound/api/schemas.py::ChatRequest`: `message` has `min_length=1` and no maximum;
`thread_id` is any string; `images` is a list of any length of strings of any size. The front door sets no body limit
(`deploy/caddy/gateway.caddy` has no `request_body`). A request body is parsed whole, copied into a Redis stream entry,
and then into the checkpoint. One large request costs memory in four places, and the conversation id becomes part of
several Redis keys.

**Change.**
- `ChatRequest.message`: `max_length=settings.chat_message_max_chars` (new setting, default 32000).
  `thread_id`: `min_length=1, max_length=200` on `ChatRequest`, `ResumeRequest` and `CancelRequest`. Do not add a
  character pattern: Telegram conversation ids embed a principal (`telegram:<principal>:<chat>`).
  `images`: at most `chat_max_images` (default 4; first read the page's own limit in
  `app/adapters/inbound/api/static/` and do not go below it), each at most `chat_image_max_chars` (default 7,000,000,
  about 5 MB as base64).
- `deploy/caddy/gateway.caddy`, in `(api_site)`: `request_body { max_size 140MB }` as the ceiling (uploads are 5 files
  of 25 MB), and a matcher for `/chat/*` with `max_size 32MB`. Run `make authlimit-smoke` if you touch that file; it is
  imported by both the development and the production Caddyfile.
- This narrows a request schema. `make openapi-breaking` will report it: add the entries to
  `.github/openapi-breaking-ignore.txt` and raise the version as that file's rules say.

**Tests.** `test_api.py`: each bound returns 422 naming the field. The Bruno collection test still passes.

### R15. Production Redis: one instance for queue and cache, no persistence policy, floating image tags

**Established:** read from code (`deploy/compose/docker-compose.prod.yml`).

**Problem.**
- One Redis holds the work queue, the conversation locks, the cancel flags, the dedup claims, the front door's rate
  buckets, the Telegram state **and** the semantic cache. No eviction policy is right for both: a cache wants
  `allkeys-lru`; the queue and the locks must never be evicted. `volatile-lru` is not a way out, because locks,
  results streams and claims all carry an expiry and would be evicted too.
- No `command:` is set, so persistence is whatever the image defaults to (periodic snapshots). A restart can lose
  minutes of accepted jobs.
- `mem_limit: 256m` with no `maxmemory`: when Redis grows past it the container is killed, not throttled (see R04 for
  what makes it grow).
- `redis/redis-stack-server:latest` and `qdrant/qdrant:latest` are floating tags on the two stores that hold state,
  and every deploy runs `pull`. The same file pins LiteLLM, PgBouncer, Caddy and Casdoor by digest. From the field:
  the Redis Stack images are no longer the maintained line; Redis 8 includes the query engine the semantic cache needs.

**Change.**
1. Two services: `redis` (queue, locks, front door, Telegram) with
   `command: ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec", "--maxmemory", "<n>mb", "--maxmemory-policy", "noeviction"]`,
   and `redis-cache` with `--save "" --appendonly no --maxmemory <n>mb --maxmemory-policy allkeys-lru` and no volume.
   New setting `semantic_cache_redis_url` (default: the value of `redis_url`, so development and existing deployments
   are unchanged); `app/adapters/outbound/redis/semantic_cache.py::_get_client` uses it.
2. Pin both Redis images and Qdrant by digest, in the production and the development compose file. Move to `redis:8`
   only after `tests/integration/test_semantic_cache_tag_escaping_real_redis.py` and the queue integration tests pass
   against it (`tests/containers.py::ensure_redis` names the image).
3. An alert when either Redis is above 80% of its `maxmemory`. That needs a Redis exporter next to the PgBouncer
   exporter in the production compose file, and a scrape job.
4. Optional and separate: a password (`requirepass`) with `REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379`. Redis
   publishes no port in production, so this is defence in depth.

**Decide:** the two `maxmemory` values for the droplet size in use.

**Pitfall.** With `noeviction`, a full queue Redis refuses writes: the API's publish fails and the caller gets an
error. That is the correct failure (visible, and nothing is silently dropped), and it is why the alert in step 3 is
part of this work order and not optional.

**Docs.** `deploy/README.md`; a pattern in `ENGINEERING_PATTERNS.md` ("a cache and a queue cannot share an eviction
policy").

### R16. The production gateway stores every prompt and answer

**Established:** read from code.

**Problem.** `deploy/litellm/litellm-config.prod.yaml` ends with `store_prompts_in_spend_logs: true`. The development
config explains why it is on there (the admin UI's log page can show request and response bodies). In production it
means every message a person sends, every retrieved passage and memory placed in a prompt, and every answer is written
to the gateway's Postgres database, with no retention, readable with the gateway's admin credentials. The application
goes to some length not to keep a second copy of prompt text (`app/agent/graph/utils.py::instrumented` logs metadata
only; the log rules in Principle V). This setting is that second copy.

**Change.** Set it to `false` in the production file and leave the development file as it is. Add a comment in both
that says which is which and why. If an operator wants bodies in production for a period, that is a deliberate choice
with a retention period: check the pinned LiteLLM image's documentation for the spend-log retention setting and name
it in `deploy/README.md` (do not guess the key).

**Tests.** None in code. The repository's test rules say not to write a test that only compares strings in a config
file.

**Docs.** One line under "Four things that bite on a first deploy" in `README.md`, and the same in `deploy/README.md`.

### R17. No queue depth, queue wait or first-token metric; no traces in production

**Established:** read from code.

**Problem.** There are fifty-five alert rules and none can say "the queue is backing up". No metric reports how many
jobs are waiting, how long a job waited before a worker took it, or how long a person waited for the first token.
Those are the three numbers that size the worker pool (`WORKER_CONCURRENCY.md` says nothing scales on queue depth; at
present nothing can even see it). Separately, the production stack runs no Langfuse and the application emits no
OpenTelemetry traces, so in production a slow turn can only be investigated from logs.

**Change, part 1 (M): three metrics and two alerts.**
- `agent_queue_wait_seconds` (histogram, label `queue`): the publisher puts `enqueued_at` (epoch seconds) in the job
  payload (`queue.py`, `ingest_queue.py`, and keep the original value in `republish_job`); the worker observes
  `now - enqueued_at` when it starts the job. A payload without the field (a job published by an older API during a
  rolling deploy) is skipped.
- `agent_queue_pending` and `agent_queue_lag` (gauges, label `domain`): read `XINFO GROUPS` in each worker's reclaim
  loop and set them from the group's `pending` and `lag`.
- `agent_first_token_seconds` (histogram): in `_run_graph_stream`, the time from the start to the first `token` event.
- Buckets for the two histograms as `View`s in `app/platform/telemetry.py` (the SDK's defaults put every value in one
  bucket; pattern 11).
- Alerts `AgentQueueBacklog` (lag above a threshold for five minutes) and `AgentQueueWaitHigh` (p95 wait). A test
  already checks that every metric an alert names exists.

**Part 2 (L, needs a spec): traces.** One trace per turn across the queue: a span for the API request, the trace
context carried in the job payload, spans in the worker for the turn, each node, each model call and each tool call,
exported over OTLP to the collector that already receives the metrics. Use the OpenTelemetry GenAI attribute names
where they exist (`gen_ai.operation.name` with `chat` and `execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.call.id`);
they are still marked as in development, so treat the names as likely to change. Span attributes follow the same rule
as logs: no message content. This is also the route out of the dependency ladder in R19.

### R18. A re-uploaded document leaves its old chunks; no way to delete a document

**Established:** read from code.

**Problem.** A chunk's point id is derived from tenant, source, position and a digest of its text
(`app/ingestion/ingestor.py::_content_point_id`). That makes a repeated ingest idempotent, which is what it was built
for. It also means that when a document is edited and uploaded again, every changed chunk gets a new id and the old
chunk stays, still retrievable and still citable. Nothing lists a tenant's documents or removes one.

**Change.**
1. Each point's payload gains `ingest_id` (the job id, or a new uuid for the direct paths). After a successful upsert
   of a source, delete the points of the same `tenant` and `source` whose `ingest_id` differs (one filtered delete
   through the `DocumentWriter` port; add the method to the Protocol, the adapter and the fake). Only after success, so
   a failed re-ingest leaves the old version serving. Index `source` (R09).
2. `DELETE /ingest/documents` with the source name in the body: tenant from the ctx, never from the request. It also
   removes the uploaded object (`ObjectStore.delete_object` exists).
3. The semantic cache may hold an answer that cites the old version for up to its lifetime (an hour). State it; do not
   try to invalidate by content.

**Constitution.** Principle I: the delete's filter always carries the tenant from the ctx. Principle III: the target
is derived by code. A document delete is an HTTP route for a signed-in person, not an agent tool.

**Tests.** `tests/ingestion/`: re-ingesting a changed text leaves exactly the new chunks; a failed re-ingest leaves
the old ones. An `integration` test against real Qdrant that one tenant's delete cannot remove another tenant's points
with the same source name.

### R19. The dependency ladder: Langfuse 2, LangChain 0.3, LangGraph 0.2

**Established:** the pins are read from `requirements.txt`; the upstream state is from the field (section 10) and must
be re-checked on the day.

**Where it stands.** `langgraph>=0.2.20,<0.3` (0.2.76 installed), `langchain-core` 0.3, `langfuse` 2.60. The reason is
in `requirements.txt` and in "Next PR: the LangGraph upgrade" in `GRAPH_PATTERNS.md`: the Langfuse 2 SDK imports
modules that LangChain 1 removed. Upstream, LangGraph is on the 1.2 line, and Langfuse's current Python SDKs are built
on OpenTelemetry, support LangChain 1, and need a Langfuse server of at least 3.63. The development stack runs
`langfuse/langfuse:2`.

**What staying costs.** A private attribute of the checkpointer is replaced at start-up to work around a lock
(`app/adapters/outbound/postgres/checkpointing.py`); a custom reducer exists because the pinned version has no
`Overwrite` (`app/agent/graph/state.py::concat_or_reset`); an import-time compatibility warning; and security fixes
that land only on current lines.

**The ladder, one PR per rung, each with `make test-integration` and `make test-live`:**
1. Development Langfuse server 2 to 3 (compose only; it adds ClickHouse and object storage). Nothing in `app/` changes.
2. Langfuse SDK 2 to the current major. The handler moves (`langfuse.callback` to `langfuse.langchain`) and is built
   without arguments; the session and user move to metadata. The touch points are
   `app/agent/runtime/stream.py::_open_trace`, `_TurnTrace` and `app/core/tracing.py`. If R17 part 2 is done first,
   this rung can instead remove the SDK from the turn path and send the same spans over OTLP.
3. `langchain`, `langchain-core`, `langchain-openai` to 1.x.
4. LangGraph to the current 1.x and `langgraph-checkpoint-postgres` to the matching line. The six verification steps
   are already written in `GRAPH_PATTERNS.md`; follow them exactly. Then check whether the lock workaround and
   `concat_or_reset` can go, each as its own small PR with its own test.

**Deploying rung 4:** stop the workers and let in-flight turns finish first (pattern 16).

**Do not** attempt rungs 3 and 4 in one PR, and do not change `STATE_SCHEMA_VERSION` unless a paused conversation
really cannot resume (that would strand every pending approval).

### R20. Web fetches: rebinding window, whole body read before the size check, crawler egress

**Established:** read from code. The first and third are already disclosed (pattern 24's "Gap", and specs/009 A1);
the second is new.

**Problem.**
1. `app/core/url_safety.py::assert_safe_url_async` resolves the host and checks the addresses; the fetch then resolves
   the name again. A name that answers differently the second time reaches an internal address.
2. `app/adapters/outbound/web/page_fetch.py::HttpxPageFetcher.fetch` reads `response.content` in full and compares its
   length to `max_bytes` afterwards. A large response is held in memory before it is refused.
3. The crawler (`app/adapters/outbound/crawl4ai/web_crawler.py`) checks the first URL; the headless browser then
   follows redirects and loads sub-resources by itself, from a container on the same Docker network as Qdrant, the
   gateway and Prometheus. It is not in the production stack today; it must not be added there as it is.

**Change.**
1. Return the checked addresses from the guard (`resolve_checked(url) -> list[str]`), and in `HttpxPageFetcher`
   connect to one of them while keeping the host name for TLS and the `Host` header: an `httpx.AsyncHTTPTransport`
   whose connection goes to the pinned address (see how `httpx` exposes this in the pinned 0.28; check, do not assume),
   or a second address check on the connected socket's peer before the body is read.
2. Stream the body: `client.stream("GET", ...)`, add up `aiter_bytes()` and stop at `max_bytes + 1`.
3. For the crawler, isolation belongs to the network, not the application: its own Docker network with no route to
   the internal services and the cloud metadata address blocked. Write it into the development compose file and into
   `deploy/README.md` as a precondition for ever enabling the crawler in production.

**Also worth a spec, not part of this work order:** screening *ingested* documents and web results with the
injection classifier the ML service already runs for user input, counted first and not blocking. Today only the
person's message is screened; retrieved and fetched text is framed and scrubbed. The approval gate remains the control
that matters.

**Tests.** `tests/adapters/outbound/web/`: a response larger than the limit is abandoned without being read whole (an
`httpx.MockTransport` that counts the bytes it served); a host that resolves to a public address at check time is
connected to at that address.

### R21. Five statements that are no longer true

**Established:** read from code. Verify each against the code before editing; correct or delete.

| Where | It says | What the code does |
|---|---|---|
| the docstring of `app/adapters/outbound/qdrant/qdrant_store.py::scope_filter` | `tests/integration/test_tenant_isolation_real_qdrant.py` "proves the result against a real Qdrant" | No such file exists. R09 creates it; until then the sentence should say "pinned against a fake only" |
| `README.md`, "Roadmap and known gaps" | "Several domains in one process. Each domain is its own process" (not built) | Built: pattern 96, `AGENT_WORKER_DOMAINS` |
| `README.md`, same section | "The shipped proxy doesn't authenticate, and neither sets nor strips the identity headers" | The production stack runs the authlimit front door, which verifies tokens and strips those headers (pattern 98, `deploy/caddy/gateway.caddy`) |
| `GRAPH_PATTERNS.md`, "Not built" | "Real authentication" | Built: specs/018, patterns 84 to 86 |
| `GRAPH_PATTERNS.md` and `README.md` | "nothing restarts a crashed [worker]" | `restart: unless-stopped` restarts a worker that exits. What is true: nothing restarts one that hangs (R22), and nothing scales on queue depth (R17) |

Also: the "Further reading" table in `README.md` says "specs 001–018"; the last spec is 022.

## 6. P2: smaller items

**R22. A hung worker is never restarted.** The workers' health check is disabled in the production compose file
because they bind no port, and `WorkerUnreachable` only fires when a whole domain has no worker. A worker whose event
loop is stuck stays "up". Have each worker touch a file under `/tmp` once per read-loop pass and per reclaim pass, and
replace the disabled health check with one that fails when the file is older than two minutes; add a gauge
`agent_worker_last_loop_timestamp_seconds` and an alert on its age. Compose does not restart an unhealthy container by
itself: say in `deploy/README.md` what does (an orchestrator, or an autoheal sidecar).

**R23. The answer cache ignores the domain; its expiry is set in a second call.**
`app/adapters/outbound/redis/semantic_cache.py` scopes an entry by tenant and principal. The same person asking the
same question in another domain (another prompt, other tools) gets the first domain's answer. Add a `domain` tag to
the index schema, the stored document and the query filter, and a `domain` argument to the `AnswerCache` port.
`app/agent/graph/build.py::build_graph` knows the domain as `manifest.name` and binds it when it creates the two cache
nodes (it cannot import `app/agent/runtime/core.py`, which imports it). A schema change needs a new index name: put a
schema version in `INDEX_PREFIX`, as the embedding identity already does. In `set`, send `JSON.SET` and `EXPIRE` in
one pipeline so a failure between them cannot leave an entry that never expires. The gap that the key ignores the
conversation so far is already listed in "Known gaps" and is separate.

**R24. Two error events carry no `code`.** `queue.py::read_results` ("no response after N seconds") and
`stream.py::astream_events_resume` (a refused resume; already in "Known gaps"). Give both an `ErrorEnvelope`:
`ErrorCode.WORKER_UNREACHABLE` (new) and one for the two checkpoint refusals. Then the description of `ErrorEvent` in
`app/adapters/inbound/api/events.py` loses its exception. A new error code is also added to
`services/authlimit/errorcodes.json` if the registry test asks for it; run `make test` and read what it says.

**R25. A pool hands out a connection that died while idle.** `app/platform/sql_store.py::_get_pool` and
`PgCheckpointing.open` build `AsyncConnectionPool` with no `check=`. After PgBouncer or Postgres restarts, the first
query on each pooled connection fails. `check=AsyncConnectionPool.check_connection` tests a connection as it is handed
out, at the price of one round trip per checkout. **Decide** with a measurement (`make loadtest-queued-headless`);
if the cost is visible, keep it off and document the failure instead.

**R26. Backups are a paragraph, not a job; no restore drill.** `deploy/README.md` describes `pg_dump` to object
storage as an option. Make it a script and a cron line (appdata, checkpointer, litellm, and the Casdoor database),
add a Qdrant snapshot, and write down one restore that was actually performed and how long it took. This is also where
to record the availability statement from section 1: single instances of each store, and what moving them to managed
services would change.

**R27. A transcript is read whole.** `stream.py::get_session_messages` loads the full state of a conversation and
returns every message. Add `limit` and `before` parameters to the route and slice after filtering; the page asks for
the last fifty and loads more on scroll.

**R28. Nothing learns from production answers.** There is a strong offline tier (`make eval`, promptfoo, the model
evaluation harness) and nothing online. Two inexpensive additions: `POST /chat/feedback` (a thumbs up or down and an
optional comment on one answer, stored with the request id and attached to the trace as a score once R17 part 2
exists), and a scheduled job that samples yesterday's answered turns and runs the existing grounded-claims check over
them, exported as a metric. Keep both advisory, as Principle VII says of judged signals.

## 7. The cost of a turn, and the code's own structure

What one ordinary turn costs today, counted from the code (no measurement; R17 adds the measurement):

```
validate_input            pure
moderate_input            ML service call (prompt guard)                     } one after another,
check_semantic_cache      embedding call + Redis vector search               } before the model
[compact_history]         an LLM call, only past 24k tokens of history       } sees anything
route_model               ML service call (only when ROUTER_MODE is not off)  }
retrieve_context          2 x (embedding + sparse + 2 Qdrant queries + rerank) }
agent <-> tools           the answer: 1 LLM call per round
check_output              pure; a rejection costs another full round (retry_output)
suggest_followups   } in parallel: an LLM call (on a cited answer)
write_semantic_cache} and another embedding + a Redis write; `done` waits for both
```

### R29. Retrieval runs on every turn, after the router instead of beside it

**Established:** read from code (`app/agent/graph/build.py`); the parallel join reproduced on `langgraph==0.2.76`.

**Problem.**
1. `retrieve_context` runs on every turn of every domain: a document search and a memory search, each with an
   embedding, a sparse vector, a Qdrant query and a rerank. It runs for "hi", for "thanks", and for
   "log an incident" in the ops domain, whose tools do not use documents. The model can also call `search_docs`
   itself, so a turn that needs a search may pay for two.
2. `route_model` runs before `retrieve_context`, but the two do not depend on each other. `route_model` reads only the
   person's message ([model_route.py](app/agent/graph/model_route.py), it calls `human_text(last_human_message(...))`)
   and writes only `model_route`. `retrieve_context` writes only `context`, `citations` and `context_anchor_index`.
   Running them one after the other adds the router's latency (up to `ROUTER_TIMEOUT_SECONDS`, 0.5 s) to every turn
   that has routing on.

**Change.** Two PRs.

*PR 1: router beside retrieval (S).* In `build_graph`, when `profile.include_router` is true, the step before
retrieval fans out to both nodes and `agent` waits for both. Checked on the pinned LangGraph: a conditional edge may
return a list, and `add_edge([...], "agent")` waits for every node in the list.

```python
before_retrieval = ["route_model", "retrieve_context"] if profile.include_router else "retrieve_context"
# route_after_cache / route_after_compaction keep returning their names; the path maps send
# "retrieve_context" (and "compact_history" when compaction is off) to `before_retrieval`.
# A path-map value cannot be a list, so wrap the two routers instead, as route_after_check_bound does:
#   def to_retrieval(route): return before_retrieval if route == "retrieve_context" else route
if profile.include_router:
    builder.add_edge(["route_model", "retrieve_context"], "agent")
else:
    builder.add_edge("retrieve_context", "agent")
```

Remove the old `builder.add_edge("route_model", "retrieve_context")`. Read the whole edge section of `build_graph`
before changing it: the cache-miss and compaction paths both lead here.

*Checkpoint compatibility.* This changes the topology between `compact_history` and `agent`. A paused conversation
sits at `human_approval`, whose edges do not change, so it resumes. A turn that crashed exactly between those nodes
during the deploy is the case `tests/agent/graph/test_checkpoint_across_a_topology_change.py` covers (the reclaim path
dead-letters it). Do not bump `STATE_SCHEMA_VERSION`.

*PR 2: whether to pre-fetch (M).* `AgentManifest` (`app/ports/outbound/domains.py`) gains
`prefetch: Literal["always", "never"] = "always"`. With `"never"`, `build_graph` leaves `retrieve_context` out of the
graph and the edge goes straight to `agent` (or from `route_model` to `agent`). The domain's prompt must then tell the
model to call `search_docs` when it needs documents; check the domain has that tool.

**Decide:** which domains use `"never"`. The recommended starting point is `ops` only, judged by `make eval` and the
domain's promptfoo suite. A third mode (`"auto"`, skipping the search for greetings and very short messages) is
possible later; do not build it without a measurement of how many turns it would skip.

**Tests.** `tests/agent/graph/test_graph_integration.py`:
`test_the_router_and_retrieval_run_in_the_same_step` (both nodes' outputs reach `agent`; with fakes that record start
times, neither starts after the other finished) and `test_a_domain_that_never_prefetches_reaches_the_model_without_a_search`
(a counting fake `GraphDeps(search_docs=...)` is never called). The existing graph-flow tests must pass unchanged.

**Docs.** Pattern 20 and the "Graph Flow" diagram in `GRAPH_PATTERNS.md`; the graph diagram in
`ARCHITECTURE_DIAGRAMS.md`.

### R30. A second model call for follow-ups holds back `done`

**Established:** read from code (`app/agent/graph/followups.py`, `build.py`).

**Problem.** After an answer that cites something, `suggest_followups` makes a full LLM call (without tools since
pattern 79). It runs in parallel with the cache write, but the graph ends, and so `done` is sent, only when both have
finished. So every cited answer costs a second model call, and the person's client shows the turn as still running
for as long as that call takes. On a hosted model that is money on every cited turn; on a local one it is seconds
(a Langfuse trace quoted in `app/agent/graph/utils.py::without_tools` measured 15 of a turn's 40 seconds before the
tools were removed).

**Change.**
1. `AgentManifest` gains `suggest_followups: bool = True`. `build_graph` includes the node only when
   `profile.include_followups and manifest.suggest_followups`. The client already handles a turn with no
   `followups` event.
2. Setting `followups_enabled: bool = True` in `Settings`, so an operator can turn the feature off everywhere without
   a code change; `build_graph` reads it at build time (the graph is built once per process).

**Decide:** the defaults. Recommended: keep `True` for `ecorp` (the web page shows the chips) and set `False` for
`ops`, `support` and `sales`, whose answers usually end in an action rather than a reading list.

**Not recommended:** emitting `done` before the follow-ups. `done` is terminal for every reader of the stream
(`TERMINAL_EVENT_TYPES` in `app/ports/outbound/jobs.py`), so the follow-ups would never arrive.

**Tests.** `tests/agent/graph/test_post_answer_parallelism.py` (the existing tests of this step):
`test_a_domain_without_followups_makes_one_model_call_for_a_cited_answer` (count calls on the fake LLM).

**Docs.** Pattern 27 in `GRAPH_PATTERNS.md`.

### R31. Old tool results are re-sent to the model on every later call

**Established:** read from code (`app/agent/graph/agent_node.py`).

**Problem.** The agent node sends the whole stored history on every call. Tool results from earlier turns are part of
it: a crawled page (up to 20,000 characters), a sandbox output, a loaded skill's full text, a subagent's answer. Each
is paid for again on every later call, every round of every later turn, until history passes `HISTORY_TOKEN_CEILING`
(24,000 estimated tokens) and `compact_history` summarises it. On a small local model a long, stale tool output also
pushes the current question and the reminders further from where the model generates, which pattern 79 says it
already struggles with. Current practice is to clear stale tool results from what the model is sent and keep them in
the stored record.

**Change.** In `make_agent_node`'s `agent` function, next to where earlier turns' retry artifacts are dropped (the
`turn_start` / `droppable` code), replace the content of each `ToolMessage` that is before `turn_start` and longer than
a limit with a short note, **in the copy sent to the model only**:

```python
STALE_TOOL_RESULT_NOTE = "[Earlier result of {name}, omitted to save space. Call the tool again if you need it.]"

def _without_stale_tool_results(messages, turn_start, max_chars):
    if not max_chars or turn_start is None:
        return messages
    out = list(messages)
    for i in range(turn_start):
        m = out[i]
        if isinstance(m, ToolMessage) and len(str(m.content)) > max_chars:  # content may be a list of parts
            out[i] = m.model_copy(update={"content": STALE_TOOL_RESULT_NOTE.format(name=m.name or "a tool")})
    return out
```

- Keep `tool_call_id` and `name` (the copy keeps them): every `tool_call` must still have its answer, or the call is
  rejected by OpenAI-style APIs (pattern 8).
- Do it after the retry-artifact filtering and before `with_turn_notes`, and recompute nothing else: `anchor` is an
  index into `messages` and this replaces items in place without changing their positions.
- Never write the result back to state. `state["messages"]` and the checkpoint keep the full results;
  `get_session_messages` and the compaction summary are unchanged.
- New setting `stale_tool_result_max_chars: int = Field(default=2000, ge=0)` (0 = off) in `Settings` and
  `.env.example`; `build_graph` passes it to `make_agent_node`.

**Cost to state in the PR.** The provider's prompt cache keeps the prefix of the previous call. When a new turn
starts, the previous turn's long results become notes, so the prefix changes once at that point. Within a turn the
prefix stays stable. For a conversation with long tool outputs this trades one cache miss per turn for a much shorter
prompt on every call.

**Tests.** `tests/agent/graph/test_agent_node.py`: a long tool result from the previous turn reaches the fake model as
the note; a long tool result from the current turn reaches it whole; a short one from the previous turn is unchanged;
the stored state still holds the full text; every `tool_call` still has its `ToolMessage` in what was sent.

**Docs.** Pattern 13 (bounded history) in `GRAPH_PATTERNS.md`.

### R32. Output checks built for small models also run on strong ones

**Established:** read from code (`app/agent/graph/output_checks.py`, `routing.py::check_output`, `agent_node.py`).

**Problem.** Many steps of a turn exist because a 3B or 7B local model drops instructions: inserting a missing
citation marker, cutting a closing offer, detecting "I will do X" instead of doing it, detecting tool output the model
made up, reminders after the question (citation, summary, skill tool, links), and a full retry round when a check
fires. Each check is cheap; a false positive is not, because it costs another full model round, and against a strong
model most of these fire rarely or never. Nothing records which checks fire for which model tier, so nobody can tell
which ones still earn their place once `ROUTER_MODE=on` sends turns to the medium or strong tier.

**Change.** Two PRs; the second only if the first's numbers justify it.

*PR 1: count by tier (S).* A counter `agent_output_check_fired_total{check, tier}` incremented in `check_output` for
every code in `output_findings`, with `tier = state.get("model_route") or "local"`. Keep the existing per-check
counters (alerts and dashboards use them). Add a dashboard panel.

*PR 2: skip checks on the tiers that do not need them (M).* A setting
`output_checks_off_on_routed_tiers: str = ""` (comma-separated check codes). `build_graph` validates each code
against `CHECKS` and raises at start-up for an unknown one, and **refuses `leaked_prompt` and `fabricated`**: those
two protect the person, not the formatting. `check_output` receives the set (bound with `functools.partial`, like
its other per-domain arguments) and skips a listed check when the turn's tier is `medium` or `strong`. Nothing
changes for the local tier.

**Decide:** which codes to list, from PR 1's numbers. Candidates to look at first: `too_short`, `deferred`,
`uncited`, `misattributed`, `skipped_tool`.

**Tests.** `tests/agent/graph/test_routing.py`: a listed check does not fire on a strong-tier turn and still fires on a
local one; a start-up with `leaked_prompt` listed raises.

**Docs.** Pattern 5 in `GRAPH_PATTERNS.md`; pattern 68 (routing).

### R33. The answer cache can answer a follow-up from another conversation

**Established:** read from code. Already listed under "Known gaps" in `GRAPH_PATTERNS.md` as "The answer cache key
ignores conversation context"; ranked higher here because in an agent most messages after the first depend on the
conversation.

**Problem.** `check_semantic_cache` looks up the person's last message alone, by meaning, among everything that person
asked in the last hour in any conversation. "yes", "do it", "more detail please" and "and for March?" mean nothing on
their own, so they match whatever similar short message was cached, and the cached answer belongs to another
conversation. Every miss also costs an embedding and a vector search, and every finished turn an embedding and a write.

**Change.**
1. Consult and write the cache only for a conversation's **first** question: in both nodes in
   `app/agent/graph/cache.py`, return `{}` early when the thread has an earlier message the person sent
   (`messages.previous_human_message(state["messages"], <index of the last human message>)` is not `None`). A first
   question is the only one whose meaning does not depend on the conversation.
2. Setting `semantic_cache_enabled: bool = True` in `Settings`. When it is false, both nodes return `{}` at once.
   Keep the nodes in the graph so the topology does not change with a setting.
3. Then read `agent_semantic_cache_total` by outcome for a week. **Decide:** if hits are a small share of lookups,
   switch the cache off: each lookup adds latency to every first turn.

**Tests.** `tests/agent/graph/test_nodes.py`: a second message in a thread never calls `cache_get` or `cache_set`
(fakes injected through the node factories); a first message still does; with the setting off, neither is called.

**Docs.** Pattern 22 in `GRAPH_PATTERNS.md`; remove the bullet from "Known gaps" in the same PR.

### R34. Every change to the history stores the whole history again

**Established:** reproduced on `langgraph==0.2.76`; verified in `langgraph-checkpoint-postgres==2.0.25`
(`BasePostgresSaver._dump_blobs` serialises the full value of each channel whose version changed).

**Problem.** `messages` is one channel. Each step that changes it (the model's reply, a tool round, a repaired answer,
a retry) writes a new version, and each version is the whole list:

```
messages blobs stored (one per version): [184, 1408]      <- the second holds the first message again
```

So a turn with five message-changing steps on a conversation of 100 KB writes about 500 KB to `checkpoint_blobs`,
and the cost grows with the length of the conversation. Each turn also reads the full state twice
(`paused_approval_async` before it starts, `aget_state` after it ends). Nothing deletes old versions (see R11).

**Change.** Measure first, then choose.
1. Measure: run `make loadtest-queued-headless` against a stack whose conversations are long, and record
   `pg_total_relation_size('checkpoint_blobs')` before and after, and the checkpointer's query time
   (`pg_stat_statements` is already loaded in the production Postgres). Put the numbers in `WORKER_CONCURRENCY.md`.
2. R11's retention bounds the total; it does not bound the per-turn writes.
3. After R19 rung 4, use LangGraph's `durability="async"`, which writes checkpoints without holding up the next step.
   **Do not use `durability="exit"`:** `astream_events_continue_turn` (crash recovery) relies on the per-step writes
   being in the database when a worker dies.
4. R31 does not help here: it shortens what the model is sent, not what is stored.

**Not recommended without a spec:** storing messages outside the checkpoint (an append-only table keyed by thread and
message id). It removes the amplification but replaces LangGraph's message reducer and every reader of the state.

### R35. Code structure: node names in the runtime, repeated entry points, patch-shaped modules

**Established:** read from code; part A's mechanism reproduced on `langgraph==0.2.76` / `langchain-core==0.3.86`.

Five independent parts. Each is its own PR; none changes behaviour, so each is proven by the existing tests passing
unchanged plus the new ones listed.

**A. The runtime recognises nodes by name (M).** `app/agent/runtime/stream.py::_run_graph_stream` decides what to send
the client by matching `on_chain_end` events for `"retry_output"`, `"check_output"`, `"retry_exhausted"` and
`"compact_history"`, and reading their outputs. Renaming a node, or adding a node that also replaces the answer,
silently changes what the client sees. Instead, the node announces the event itself:

```python
from langchain_core.callbacks.manager import adispatch_custom_event
await adispatch_custom_event("agent_stream", {"type": "retry"}, config=config)
```

Checked on the pinned versions: it arrives in `astream_events(version="v2")` as `on_custom_event` named
`agent_stream`, with `metadata.langgraph_node` set. Steps:
1. In `retry_output`, `check_output` (only when it replaced the answer), `retry_exhausted` (only when it replaced the
   answer) and `compact_history` (only when it trimmed), dispatch `{"type": "retry"}` plus, where the text was
   replaced, `{"type": "token", "content": <replacement>}`, or `{"type": "compacted"}`. `retry_output`,
   `check_output` and `retry_exhausted` (`app/agent/graph/retry.py`, `routing.py`) are plain `def` today; a node that
   dispatches must become `async def` and take `config`. Their unit tests call them directly with a state, so give
   `config` a default of `None` and dispatch only when it is given.
2. In `_run_graph_stream`, one branch forwards every `on_custom_event` named `agent_stream` whose metadata has no
   `subagent_name` (a nested run's events reach the same stream, so this filter is still needed), appending a `token`'s
   content to `final_answer` and clearing it on `retry`. Delete the four `on_chain_end` branches.
3. The order of events the client sees must not change. `tests/agent/runtime/test_streaming_terminal_events.py` and
   the tests that hold the stream to the OpenAPI document (`tests/agent/conftest.py`) are the proof.

**B. Three entry points repeat their setup (S).** `astream_events_turn`, `astream_events_resume` and
`astream_events_continue_turn` each reserve a budget hold, load the graph, open a trace, build the same config and
release the hold. Extract one private async generator,
`_run_turn(name, thread_id, ctx, graph_input, trace_input, cancel_check)`, that does those and wraps
`_run_graph_stream`; each entry point keeps only its own checks (the allowance, the pending approval, resumability).
Keep `aclosing` on every inner generator (`.claude/rules/runtime-reliability.md`).

**C. Two log lines per node at INFO (S).** `app/agent/graph/utils.py::instrumented` logs `node_started` and
`node_completed` at INFO for every node: about 25 to 30 lines per turn. Log `node_started` at DEBUG and keep
`node_completed`, `node_failed` and `node_paused` at INFO (they carry the duration and the outcome).
`tests/agent/graph/test_instrumentation.py` asserts on `node_started`; update it in the same PR (capture at DEBUG).

**D. Modules shaped for test patching (L, gradual; optional).** `app/core/config.py` defines every setting twice, in
`Settings` and as a module constant read at import time, and `stream.py` calls `runtime_module.X` so that tests can
replace `X`. New code reads `settings.<name>` at call time; when a module is touched for another reason, move its
constants to that. Do not do it as one sweep: the constants are imported in many modules and patched in many tests.

**E. Docstrings that retell history (S each, optional).** `CLAUDE.md` asks for short comments that name the failure
and point at the pattern number. Many docstrings in `app/agent/runtime/stream.py`,
`app/adapters/inbound/workers/agent_worker.py`, `app/core/config.py` and `app/agent/graph/` retell the whole
investigation, which makes the code slow to read for a person and costly for a coding agent's context. When a module
is touched, move the story into the pattern it belongs to and leave one sentence and the pattern number. Never delete
a "why" without moving it.

## 8. Reviewed and deliberately left alone

These were read with the same question and need no change. They are listed so that nobody "fixes" them.

- **The slot-based ports** (`app/ports/slot.py`) instead of constructor injection. The decision and its price are
  written in `ARCHITECTURE.md`; the tests that every entrypoint wires and every port has an adapter pay for it.
- **`ToolNode` plus `handle_tool_errors`, one approval for a whole batch of calls.** Per-call approval and editing
  arguments are possible later (R12 makes the calls addressable); they are a product decision, not a defect.
- **The whole-turn timeout wrapping the event iterator** (`_iterate_with_timeout`). It is the outer bound; R06 adds the
  inner one.
- **Always acknowledging a job, even on failure,** and reclaiming by continuing the checkpointed run. This is the
  right answer to at-least-once delivery with side effects, and it is the part of the design most worth keeping.
- **Failing open on the dedup store and on the budget read, with a metric and an alert.** Each is a stated policy with
  a switch where a deployment may want the other answer (`BUDGET_CHECK_FAILURE_POLICY`).
- **The semantic cache's similarity threshold (0.95) and its refusal to cache a turn that acted.**
- **Prompt construction:** the system prompt as a constant, per-turn notes on the question instead of extra system
  messages, tools removed from calls that need none (pattern 79).
- **A fixed `temperature=0` and no strict tool schemas by default** (pattern 74 has the measurement).
- **mypy strict, ruff's rule set, the hermetic default tier, the advisory status of judged evaluations.**
- **The billing package's boundary** and the authlimit service's boundary: both enforced in both directions.

## 9. What this review did not cover

- **The test suite was not run.** The checkout had no virtual environment and the machine's Python is 3.15. The
  reproductions in R01 and R02 and the library checks in R03, R06, R09 and R11 were made in a separate environment with
  the pinned versions of those libraries only. Nothing here says the suite is green or red at `495b35a`.
- **Nothing was run against a live stack**: no load test, no real Redis, Postgres or Qdrant. Every performance
  statement is a count of round trips read from the code, not a measurement. R08 step 2 and R25 say to measure first
  for that reason.
- **Read only in outline, or not at all:** `packages/billing/`, the Go code of `services/authlimit/`,
  `services/ml-service/`, the Telegram channel and the MCP gateway beyond their entry points, the evaluation datasets,
  the promptfoo suites, the Grafana dashboards, and the Terraform. A finding in those areas would be new.
- **The web statements in R15, R17 and R19** come from search results, several of them secondary sources. Each says
  "from the field" and should be checked against the project's own documentation on the day the work is done.

## 10. Sources

Consulted on 2026-11-13.

- LangGraph releases and persistence documentation: <https://releasebot.io/updates/langchain-ai/langgraph>,
  <https://docs.langchain.com/oss/python/langgraph/checkpointers>
- Langfuse Python SDK upgrade path and server requirement:
  <https://langfuse.com/docs/observability/sdk/upgrade-path/python-v2-to-v3>,
  <https://langfuse.com/docs/observability/sdk/python/setup>
- OpenTelemetry GenAI semantic conventions (status and agent spans):
  <https://www.dash0.com/knowledge/opentelemetry-genai-semantic-conventions-explained>,
  <https://www.greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions>
- OWASP Top 10 for Agentic Applications (goal hijack, tool misuse, memory and context poisoning; "least agency"):
  <https://cycode.com/blog/owasp-top-10-agentic-applications/>
- Redis Stack to Redis 8: <https://redis.io/docs/latest/operate/oss_and_stack/install/upgrade/standalone/>

Measured against this repository's design, the OWASP agentic list is mostly covered by what section 1 lists as built:
the approval gate and capability tiers (tool misuse), untrusted-content framing and input screening (goal hijack),
and a gated, owner-scoped `remember` (memory poisoning). The remaining exposure is content the agent reads rather than
content the person types, which is the note at the end of R20.

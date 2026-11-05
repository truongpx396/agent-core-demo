# Architecture: ports and adapters

The agent and its domains are the inside. Everything they use that is not theirs (Postgres, Qdrant, Redis, the LLM gateway, the ML service, the
web, Slack, the sandbox) is outside, reached through a **port**: a `Protocol` in `app/ports/outbound/` that says what the inside needs, in its own terms. An
**adapter** implements a port against one system. One module, `app/bootstrap.py`, names both and joins them when a process starts. The build fails
when an import crosses the line the wrong way (`tests/core/test_architecture_layers.py`).

```
 DRIVING (inbound) ────────► the application ───────► ports ◄─────────── DRIVEN (outbound)
 adapters call in            asks for what it         Protocols, data,    adapters are called
                             needs, by port           slots

 app/adapters/inbound/       app/agent    graph,      app/ports/outbound/ app/adapters/outbound/
   api/       HTTP                        runtime,                          postgres/ qdrant/ redis/ minio/
   channels/  CLI, Telegram                tools, cost                       litellm/ mlservice/ embeddings/
   mcp/       MCP servers, gateway app/domains tools, policy                web/ crawl4ai/ notify/ mcp/
   workers/   queue consumers      app/core    the kernel                   app/domains/*/store.py  each domain's repository
 scripts/

                  app/bootstrap.py binds every port to its adapter, once per process
```

## The rings, and what each may import

| Ring | Where | May import |
|---|---|---|
| **ports** | `app/ports/` (`outbound/`, and `slot.py`), and each business module's own `app/domains/<module>/ports.py` | the standard library, `pydantic`, the kernel, other ports |
| **kernel** | `app/core/*.py` except the platform modules | the kernel and the ports. No infrastructure client, nothing above it |
| **application** | `app/agent/`, `app/domains/` (except the adapter files below), `app/ingestion/` | the kernel, the ports, LangGraph and LangChain core, OpenTelemetry/Langfuse/structlog. Never an infrastructure client, an adapter, an inbound adapter, or (from `app/agent`) a domain |
| **adapters** | `app/adapters/` (`inbound/` and `outbound/`, below), `app/ingestion/`, `app/bootstrap.py`, the platform modules in `app/core/` (`sql_store`, `internal_identity`, `telemetry`, `profiling`, `logging_config`, `job_runtime`), and a domain's `store.py`, `metrics_client.py` and `registry.py` | anything inward of them |

## The business modules

Inside the application ring the code is a modular monolith: one process, and a business feature is a **module** that owns everything it needs.

```
app/domains/
  ecorp/ ops/ sales/ support/    one module each, the same shape:
    domain.py      the plugin: its AgentManifest and DomainPlugin (what the registry publishes)
    tools.py       the tool layer (schemas, ctx check, idempotent())   usecases.py  the rules, where a module has them
    ports.py       the module's own driven ports (a repository) and their Slots
    store.py       the adapter for them (also metrics_client.py in ops); bound in bootstrap.py
    skills/ subagents/
  shared/          what the modules have in common: policy.py, sandbox_session.py, web_search.py (the shared kernel)
  registry.py      names every module; the one place that does
```

Two rules, each a test (`tests/core/test_architecture_layers.py`):

- **A module imports no other module.** It may import itself, `app/domains/shared/`, the kernel and the platform ports; not a sibling and not the registry. What two modules both need goes in `shared/`;
  what one asks of the world is a port.
- **The rest of the code imports only what a module publishes**: `domain` (the plugin) and `usecases` (what a driving adapter calls). `app/bootstrap.py` also names `ports` and the adapter (`store`, `metrics_client`),
  which is its job. Four reaches remain in `MODULE_LEDGER`, which may only shrink.

A port belongs to the module when only that module's tools use it (`TicketStore` is the support module's) and to the platform (`app/ports/outbound/`) when several things use it (`WebSearch`, `SandboxCatalog`, `UsageEventStore`).

## The engine's two doors

`app/agent/` is what every module plugs into and every driving adapter calls, and it has two doors (`tests/core/test_architecture_layers.py`):

- **`app.agent.tools`** is the SDK a business module builds its plugin from: the shared tools, the skill and subagent catalogs, `run_subagent`. A module imports nothing else from the engine.
- **`app.agent.runtime`** and **`app.agent.cost`** are what a driving adapter calls: the streaming turns, the session directory, the spend queries. An inbound adapter imports nothing else from the engine.

The graph's nodes, guards and state stay behind them, so the graph can change without touching a module or an adapter. When an adapter needs something that is graph knowledge, it goes into `runtime/`:
the queue worker reads a reclaimed turn's checkpoint through `app/agent/runtime/reclaimed_turn.py`, which knows which messages are the person's and which are retry artifacts, and keeps the Redis side itself.

## The two sides of the adapter ring

`app/adapters/` has one half for each direction a call can go:

| | `inbound/` : DRIVING (primary) | `outbound/` : DRIVEN (secondary) |
|---|---|---|
| Who calls whom | something outside calls the application | the application calls out |
| What is in it | `api/` the chat, ingest and health HTTP API; `channels/` the CLI (`cli/`) and Telegram (`telegram/`), over the shared `door.py` and `oauth.py`; `mcp/` the published MCP servers and their OAuth gateway; `workers/` the queue consumers | one package per technology: `postgres/`, `qdrant/`, `redis/`, `minio/`, `litellm/`, `mlservice/`, `embeddings/`, `web/`, `crawl4ai/`, `notify/`, `mcp/` (the client and the sandbox bridge) |
| How it meets the application | calls the application's own functions (`app/agent/runtime/`): there is no driving port, because nothing needs to swap the application for a fake | implements a Protocol from `app/ports/outbound/`; `app/bootstrap.py` binds it |
| Who may import it | nobody in `app/` (a leaf) | `app/bootstrap.py` and nothing else; an inbound adapter reaches it through its port |

Where new code goes: a new way in (another channel, another published server, another consumer) is `app/adapters/inbound/<name>/`; a new system the application calls is a port in
`app/ports/outbound/` and `app/adapters/outbound/<technology>/`; logic that decides something is in `app/agent/`, `app/domains/` or `app/core/`, never in an adapter.

Three tests keep the sides apart (`tests/core/test_architecture_layers.py`): the inbound packages are leaves, **an outbound adapter never imports an inbound one**, and **an inbound adapter
never imports an outbound one**. Where an inbound adapter needs something an outbound one does, there are three ways, in this order of preference:

1. a port, when the application's side needs it (the API submits a turn through `TurnQueue`, an upload goes through `ObjectStore`, the readiness probe asks `DocumentIndex.ping`);
2. its own technology, when it is the adapter's trigger and not something the application calls: the queue workers read their Redis stream themselves (`app/adapters/inbound/workers/streams.py`), and
   what the producer and the consumer must agree on (stream and key names, TTLs) is plain data in the port module (`app/ports/outbound/jobs.py`);
3. a platform module in `app/core`, when both sides open the same connection (`app/core/redis_client.py`, like `sql_store.py`), or `bootstrap.close_adapters()` for shutdown.

"Infrastructure client" is a fixed list: `psycopg`, `psycopg_pool`, `redis`, `qdrant_client`, `openai`, `langchain_openai`, `httpx`, `minio`,
`fastembed`, `crawl4ai`, `mcp`, `fastapi`, `starlette`, `uvicorn`, `jwt`, `limits`, and the LangGraph Postgres saver
(`tests/core/test_architecture_layers.py::INFRASTRUCTURE`).

Rules beyond the imports, each a test:

- **The inbound adapters are leaves.** Nothing in `app/` imports `app.adapters.inbound.api`, `channels`, `mcp` or `workers`.
- **Neither side imports the other** (above).
- **Only a process entrypoint imports `app.bootstrap`** (and `scheduled_job`, which starts the one-shot jobs).
- **Every entrypoint that reaches the application calls `wire()`** (`tests/core/test_entrypoints_wire.py`).
- **The modules that decide stay free of the agent framework**: security, errors, scrubbing, URL safety, moderation, routing policy, the whole cost policy and the
  domain policy import no LangGraph or LangChain (`FRAMEWORK_FREE` in the layering test).

## The ports

`Slot[T]` (`app/ports/slot.py`) is how the application reaches an adapter: `ticket_store.get().get_ticket(...)`. It is read at call time, binds once, and an unbound
slot raises `PortNotBound` naming the port. There is no default adapter, so the application never decides which one is real.

| Port module | Ports | Adapter | What stays in the application |
|---|---|---|---|
| `cost` | `UsageEventStore`, `BudgetHoldStore`, `BudgetPolicyStore`, `GatewaySpendLog` (a parameter, not a slot) | `app/adapters/outbound/postgres/`, `app/adapters/outbound/litellm/spend_logs.py` | the allowance rule, windows, fail-open reads, every degrade counter (`app/agent/cost/`) |
| `models` | `ModelCatalog` | `app/adapters/outbound/litellm/model_catalog.py` | pricing and the unpriced-model policy |
| `llm` | `ChatModels`, `LlmFailures` | `app/adapters/outbound/litellm/chat_model.py`, `failures.py` | the alias, the tool shape, never retrying a budget stop, answering a guardrail refusal |
| `sessions`, `idempotency`, `checkpointing` | `SessionStore`, `ToolCallLedger`, `Checkpointing` | `app/adapters/outbound/postgres/` | ownership never defaults to yes; the exactly-once guard fails open and steers the agent to verify after a timeout |
| `domains` | `DomainCatalog`, and the plugin contract `AgentManifest` / `DomainPlugin` | `app/domains/registry.py` | the graph is never forked for a domain |
| `retrieval` | `DocumentIndex`, `DocumentWriter`, `Embedder`, `AnswerCache` | `app/adapters/outbound/qdrant/qdrant_store.py`, `app/adapters/outbound/embeddings/embeddings.py`, `app/adapters/outbound/redis/semantic_cache.py` | WHAT may be seen: every tenant search carries an `AccessScope` |
| `classification`, `screening` | `RouteClassifier`, `InjectionScorer` | `app/adapters/outbound/mlservice/` | the routing decision and the moderation verdict (fail open on a failed check, closed on a real hit) |
| `web`, `notify`, `sandbox` | `WebSearch`, `PageRenderer`, `PageFetcher`, `TeamChannel`, `SandboxCatalog` | `app/adapters/outbound/web/` (search, `page_fetch.py`), `app/adapters/outbound/crawl4ai/web_crawler.py`, `app/adapters/outbound/notify/`, `app/adapters/outbound/mcp/sandbox_tools.py` | framing a result as untrusted, scrubbing, bounding, the approval tier |
| `jobs`, `storage` | `TurnQueue`, `IngestQueue`, `ObjectStore` | `app/adapters/outbound/redis/queue.py`, `ingest_queue.py`, `app/adapters/outbound/minio/object_store.py` | which events end a stream; the dedup window; a job carries a pointer to an upload, never its bytes |
| a module's own (`app/domains/<module>/ports.py`) | support `TicketStore`, sales `LeadStore`, ops `IncidentStore` and `MetricsReader`, ecorp `KnowledgeBase` and `EmployeeDirectory` | each module's `store.py` (the module itself), `ops/metrics_client.py` | use cases, closed vocabularies, deterministic ids, result text |

Billing was the first port and is unchanged: `credit_billing.port.BillingPort`, reached through `app/billing_integration/port.py`, enforced by its own two tests.

Three ports carry a rule the others do not, because a constitution principle rests on it:

- **Tenant isolation (Principle I).** `Policy.lower` returns an `AccessScope` (tenant, kind, owner, retention cutoff), never a store's filter type. `DocumentIndex.search`
  cannot be called without one; the unscoped lookup of the skills catalog is a separate, named `search_catalog`. The Qdrant adapter turns the scope into a
  pre-filter (`app/adapters/outbound/qdrant/qdrant_store.py::scope_filter`), pinned condition by condition.
- **Exactly-once (Principle IV).** `ToolCallLedger.claim_or_cached` must be atomic at the store. The row-level `tool_call_id UNIQUE` writes are the domain stores' and are untouched.
- **A store's error text never reaches the model.** The composition root registers the driver's error class (`app/core/errors.py::register_store_error`); the kernel asks `is_store_error`.

## Starting a process

Every entrypoint calls `wire()` before it does anything else; the API also calls `verify_wired()` in its lifespan, and so do the workers and the Telegram channel. `wire()` binds each
port that has no adapter yet (`Slot.provide`), opens no connection, and is safe to call twice. `adapters()` is the one table of port to adapter.

`GraphDeps` is unchanged and is still how a test (or a nested subagent run) hands the compiled graph its own LLM, search and cache. Slots are for what a plain function has to
reach; `GraphDeps` is for what the graph is built with.

## Adding something

**A new external dependency** (a second search provider, a new store):
1. the Protocol, its data and its `Slot` in `app/ports/outbound/<capability>.py`. The Protocol's docstring states the contract an adapter must keep (what it raises, what it scopes by);
2. the adapter in `app/adapters/outbound/<technology>/`. It returns data in the port's types and translates the system's errors into the port's;
3. one entry in `app/bootstrap.py::adapters()`. `tests/core/test_bootstrap.py` fails on a port without an adapter and on an adapter that does not match its Protocol (names, async-ness, parameters);
4. a fake in `tests/fakes/` if consumers need one, listed in `tests/fakes/test_fakes.py`, which holds it to the same Protocol check.

**A new domain** is a new module: `app/domains/<name>/` with `domain.py` (an `AgentManifest` and a `DomainPlugin`), `tools.py` and `store.py`, one line in `registry.py`, and `ports.py` for its repository; `store.py` is bound in `bootstrap.py`. It imports
nothing from a sibling (`shared/` is for what is common), and `tests/core/test_architecture_layers.py` says so if it does.

**A new way in** (a channel, a published server, a consumer): `app/adapters/inbound/<name>/`, a leaf; its entrypoint calls `wire()`. The test says so if you forget.

## Testing

| What | How |
|---|---|
| The application (a rule, a fail-open path, a metric) | against a fake bound with `Slot.overriding` (`tests/fakes/`), or a scripted connection through the real adapter where the SQL is part of what is being proved |
| An adapter (the SQL it sends, the request it makes, the error it translates) | `tests/adapters/outbound/`: the adapter driven directly against a recorded cursor or an `httpx.MockTransport` |
| That an adapter does what a database does (`ON CONFLICT`, an append-only trigger, a real filter) | `tests/integration/`, against a container; `tests/integration/postgres_adapters.py` points the adapters at it |
| That a fake and an adapter are interchangeable | `tests/fakes/conformance.py`: same method names, async-ness and parameters as the Protocol |

`tests/conftest.py` calls `wire()` at import, so a test that does not care runs the production adapters, whose I/O the suite's autouse guards already stop. The default for the
usage-event store, the model catalog, the classifier and the injection scorer is a fake.

## Decisions

- **A typed slot, not a constructor argument.** The code this was retrofitted onto is module-level functions called from graph nodes, tools and scripts, and the repo's own earlier port
  (`billing_port.billing`) is "a module-level name read at call time". Threading a container through every call would have touched most of the 285 files; a slot reads the same and
  can be listed, checked and overridden. The price is an ambient lookup; the test that every entrypoint wires, and the one that every port has an adapter, are what pay it.
- **LangGraph and LangChain core are in the application ring, and LangChain's messages are the graph's transport type.** `State.messages` is `list[BaseMessage]` under LangGraph's
  `add_messages` reducer, and that is what the checkpointer stores, so there is no second message model mirroring it. A graph node is the adapter: it reads the message and hands what the
  decision needs (tool-call dicts, a capability map, a string) to code that knows no message type. The decisions the principles rest on are pinned framework-free (`FRAMEWORK_FREE`). The
  guards (`routing`, `tool_guards`, `loop_guards`, `skill_guards`, `output_checks`) are part of the graph and keep a message-type check or cast at their edge; beyond it they work on
  tool-call dicts, mappings, strings and `CheckContext`. If the framework is ever replaced, the mapper goes at that boundary.
- **OpenTelemetry, Langfuse and structlog are allowed everywhere.** Observability is cross-cutting.
- **A domain's `store.py` is its adapter, as a module.** The `store.py` + `tools.py` + `domain.py` shape is the documented convention, and a module satisfies a `Protocol`
  structurally, so the SQL did not move and its tests did not change.
- **Modules first, layers inside.** The platform stays layered by technology (`app/adapters/outbound/<technology>/`, `app/ports/outbound/`) because several things share each system; a business feature is
  cut by module, with its repository port beside the adapter that implements it. The ports moved with their `Slot` objects intact, so no call site changed beyond an import path.
- **A queue consumer owns its broker.** The workers' Streams code (consumer group, thread lock, reclaim, dead letters) is the inbound adapter's own technology, not a port: nothing in the application calls it,
  and putting it behind one would only move the Redis commands. The producer is a port because the API, the application's caller, submits work through it.
- **Moved code was moved verbatim.** Behaviour, fail-open/closed choices and metrics are the same; the diff is where things live and what imports them.

## Not done, and why

- **The readiness probes for Postgres and the ML service are inline in `app/adapters/inbound/api/health.py`** (`psycopg`, `httpx`). An inbound adapter may use an infrastructure client, so the rule allows it; a probe port for each would have one caller.
  The operator scripts likewise import adapter modules (`scripts/jobs/followup_sweep.py` calls the sales store).
- **`app/adapters/outbound/qdrant/memory.py` (scoped memory deletion) builds its selector in the adapter ring.** Nothing in the application calls it: it is for a data-subject request or a retention sweep, run by an operator.
- **A domain's `store.py` is an adapter living in its domain**, by the `store.py` + `tools.py` + `domain.py` convention (see Decisions).
- **Four reaches into a module's internals remain** (`MODULE_LEDGER`): the ops MCP server uses `ops/tools.py`, the ops digest job uses `ops/metric_checks.py` and `ops/ports.py`, and the follow-up sweep queries `sales/store.py`. Each is a use case the module has not published (ops has no `usecases.py`).
- **Only the business domains are modules.** The agent engine, `app/ingestion/` and `app/billing_integration/` keep their own shape; they are not cut the same way and are not under the module rules.
- **`app/billing_integration/` sits beside the adapters, not in them.** It is the earlier seam to the billing package, with its own two boundary tests; moving it would touch the constitution's "Self-contained packages".
- **The constitution does not describe the rings.** They and the rules above would be a MINOR addition to "Architecture & Technology Constraints", which goes through `/speckit-constitution`. (Its two path pointers that named the API's old package were fixed in 1.4.2.)

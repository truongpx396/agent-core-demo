# Architecture diagrams: the big picture

Maps of the whole system for someone who has not read the code yet: what runs where, how the code is layered, what one turn of the agent does, and the flows where a mistake would leak data, spend money or write twice.

A diagram is a map, not the contract. The text under each one names the code that is the contract, and the other docs hold the reasoning: [ARCHITECTURE.md](ARCHITECTURE.md) (the layering and the tests that enforce it), [GRAPH_PATTERNS.md](GRAPH_PATTERNS.md) (the graph; each pattern has the bug behind it), [COST_GOVERNANCE.md](COST_GOVERNANCE.md) and the [constitution](.specify/memory/constitution.md). Checked against the code on 2026-11-10. When a diagram and the code disagree, the code is right, and a change that alters a flow fixes its diagram.

| You want to know | Read |
|---|---|
| Which processes exist and who talks to whom | [1](#1-runtime-topology) |
| How the code is layered, and what may import what | [2](#2-the-rings-ports-and-adapters), [3](#3-the-engine-and-its-domain-modules) |
| What one turn of the agent does, node by node | [4](#4-the-agent-graph) |
| Where each piece of state lives | [5](#5-where-state-lives) |
| Where the three non-negotiable rules are enforced | [6](#6-where-the-non-negotiable-rules-are-enforced) |
| How it is built, shipped and run | [7](#7-deployment-and-delivery) |
| A request from click to answer | [8](#8-a-chat-turn-end-to-end), [9](#9-signing-in-and-crossing-the-front-door) |
| Approval, and why a write happens once | [10](#10-human-approval-pause-decide-resume), [11](#11-a-write-tool-runs-exactly-once) |
| What happens when a worker dies | [12](#12-a-worker-dies-mid-turn) |
| Retrieval, cost, skills and subagents, uploads, MCP, start-up | [13](#13-retrieval-hybrid-search-under-a-tenant-scope) to [18](#18-process-start-wire-and-the-graph) |

## Part 1: Structure

### 1. Runtime topology

The processes and how a request reaches them. Every chat client, the web UI, the CLI and Telegram alike, goes through the front door and the queue; nothing runs the graph inside a client's process and nothing but the API writes to the queue (patterns 89 and 91). The MCP gateway is the one other way in, and it only reads. Which process touches which store is in [section 5](#5-where-state-lives).

```mermaid
flowchart LR
    ui["Web UI, CLI, Telegram bot"]
    mcpc["External MCP client"]
    casdoor["Casdoor<br/>OAuth 2.1 server"]

    subgraph door_sg["Front door (specs 018, 020)"]
        caddy["Caddy<br/>TLS, forward_auth"]
        authlimit["authlimit (Go)<br/>verify token offline, rate limit,<br/>revocation, sign a 30 s identity"]
    end

    subgraph app_sg["Application processes (one image)"]
        api["api (FastAPI)<br/>SSE, ownership checks,<br/>publishes jobs"]
        mcpgw["mcp-gateway<br/>read-only tools"]
        aw["agent-worker pool<br/>runs the LangGraph turn"]
        iw["ingest-worker pool<br/>parse, chunk, embed"]
    end

    queue[("Redis Stack<br/>Streams: turn jobs, results,<br/>ingest jobs")]
    stores[("Postgres, Qdrant,<br/>MinIO or Spaces<br/>see section 5")]

    subgraph models_sg["Model plane"]
        litellm["LiteLLM proxy<br/>aliases, retries, gateway budget"]
        llm[("LLM provider<br/>Ollama in dev")]
        ml["ml-service<br/>reranker, Prompt Guard,<br/>embeddings, label sets"]
        sandbox["OpenSandbox<br/>code execution, opt-in"]
    end

    obs["Observability<br/>OTel collector, Prometheus, Loki,<br/>Grafana, Langfuse"]

    ui --> caddy
    mcpc --> caddy
    ui -.->|"get a token"| casdoor
    mcpc -.->|"get a token"| casdoor
    caddy -->|"asks about every request"| authlimit
    authlimit -.->|"signing keys"| casdoor
    caddy -->|"signed identity"| api
    caddy -->|"bearer token"| mcpgw
    api <-->|"jobs in, events out"| queue
    queue <--> aw
    queue --> iw
    app_sg --> stores
    aw --> litellm
    iw --> litellm
    litellm --> llm
    aw -->|"rerank, screen, route"| ml
    aw -.->|"MCP stdio"| sandbox
    app_sg -.->|"OTLP push, traces"| obs
```

- The API and the workers share no memory, only Redis and Postgres, so the tier that holds SSE connections and the tier that runs turns scale on their own (pattern 43).
- The production compose file ships `api`, `agent-worker`, `ingest-worker`, PgBouncer, Postgres, Redis, Qdrant, LiteLLM and `ml-service` behind Caddy. Casdoor, the MCP gateway, Langfuse, MinIO, crawl4ai and OpenSandbox are in the dev stack only, and the production Caddyfile is not yet wired to authlimit ([README](README.md#-deploying-to-production)).
- The README's [architecture diagram](README.md#-architecture) draws the same system at node level, including the upload pipeline.

### 2. The rings: ports and adapters

The application reaches every outside system through a `Protocol` (a port), and one module, `app/bootstrap.py`, joins each port to its adapter when a process starts. The arrows are the only legal import directions.

```mermaid
flowchart LR
    subgraph driving_sg["DRIVING adapters: app/adapters/inbound/"]
        in_ch["channels/<br/>CLI, Telegram"]
        in_api["api/<br/>FastAPI routes, SSE"]
        in_wk["workers/<br/>queue consumers"]
        in_mcp["mcp/<br/>stdio servers, OAuth gateway"]
    end

    subgraph app_ring_sg["APPLICATION"]
        agent["app/agent<br/>graph, runtime, tools, cost"]
        domains["app/domains<br/>ecorp, support, ops, sales"]
        ingestion["app/ingestion<br/>chunking, extractors, ingestor"]
    end

    ports["PORTS: app/ports/outbound/<br/>Protocols and Slot[T]<br/>a module's own repository port is its ports.py"]

    subgraph driven_sg["DRIVEN adapters: app/adapters/outbound/"]
        out["postgres, qdrant, redis, minio, litellm,<br/>mlservice, embeddings, web, crawl4ai,<br/>notify, mcp client"]
        store["app/domains/*/store.py<br/>a module's own repository adapter"]
    end

    subgraph support_sg["Supporting modules"]
        kernel["KERNEL: app/core<br/>config, security, errors, metrics, tool runtime<br/>no infrastructure client, imports nothing above it"]
        plat["PLATFORM: app/platform<br/>SQL pool, Redis client, telemetry, identity verifier"]
        boot["COMPOSITION ROOT: app/bootstrap.py<br/>wire() binds every Slot to its adapter"]
    end

    in_ch -->|"HTTP, through the front door"| in_api
    in_api -->|"app.agent.runtime, app.agent.cost"| agent
    in_wk -->|"app.agent.runtime"| agent
    in_mcp -->|"a module's usecases"| domains
    domains -->|"app.agent.tools only"| agent
    agent -->|"Slot lookups at call time"| ports
    domains -->|"Slot lookups"| ports
    ingestion --> ports
    out -.->|"implements"| ports
    store -.->|"implements"| ports
    boot -.->|"binds"| ports
    app_ring_sg -->|"may use"| kernel
    driven_sg -->|"may use"| plat
```

- **Inbound adapters are leaves**: nothing in `app/` imports them, and an inbound adapter never imports an outbound one (it goes through a port, its own technology, or `app/platform`).
- **A module imports no other module.** What two modules share goes in `app/domains/shared/`; code outside `app/domains/` imports only a module's `domain` and `usecases`.
- **No infrastructure client** (`psycopg`, `redis`, `qdrant_client`, `httpx`, ...) in the application ring; a module's own `store.py` is its adapter and is the exception. A slot with no adapter raises `PortNotBoundError` naming the port.
- **Only an entrypoint imports `app.bootstrap`**, and every entrypoint calls `wire()` first.

Each rule is a test in `tests/core/test_architecture_layers.py` or `tests/core/test_entrypoints_wire.py`; [ARCHITECTURE.md](ARCHITECTURE.md) has the full catalogue and how to add a port.

### 3. The engine and its domain modules

`app/agent` is the one engine; a business feature is a module that plugs into it. A new use case is a manifest and a plugin, never a fork of `build_graph()` (pattern 23). The engine has two doors, one for modules and one for adapters.

```mermaid
flowchart LR
    subgraph adapters_sg["Driving adapters"]
        a_api["api, workers, channels"]
        a_mcp["mcp servers"]
    end

    subgraph engine_sg["app/agent: the engine"]
        runtime["runtime/<br/>turns, streaming, sessions<br/>DOOR for adapters"]
        cost["cost/<br/>meter and caps<br/>DOOR for adapters"]
        graph_pkg["graph/<br/>build_graph, nodes, guards, State"]
        tools["tools/<br/>shared tools, catalogs, run_subagent<br/>DOOR for modules"]
    end

    subgraph contract_sg["Plugin contract: app/ports/outbound/domains.py"]
        catalog["DomainCatalog (port)<br/>resolve(name)"]
        plugin["AgentManifest + DomainPlugin<br/>prompt, allowed_tools, tools,<br/>capabilities, policy"]
    end

    registry["app/domains/registry.py<br/>the one module that names every domain"]

    subgraph module_sg["One domain module, four of them"]
        domain_py["domain.py"]
        tools_py["tools.py<br/>schemas, ctx check, idempotent()"]
        usecases["usecases.py"]
        mports["ports.py<br/>its repository port"]
        mstore["store.py<br/>its adapter"]
        skills["skills/ and subagents/"]
    end

    shared["app/domains/shared<br/>policy, sandbox_session, web_search"]

    a_api --> runtime & cost
    a_mcp --> usecases
    runtime --> catalog
    runtime -->|"build_graph(manifest, plugin)"| graph_pkg
    registry -.->|"implements"| catalog
    registry --> domain_py
    domain_py -->|"builds"| plugin
    domain_py --> tools_py & skills
    tools_py --> mports
    mstore -.->|"implements"| mports
    tools_py -->|"imports only this door"| tools
    tools_py --> shared
    graph_pkg -->|"gates every tool by capability"| plugin
```

- A module's `AgentManifest.allowed_tools` is an exact list: that omission, not a policy check, is what "sandboxed" means for a domain.
- Each skill (`skills/<name>/SKILL.md`) and subagent (`subagents/<name>/AGENT.md`) lives in its domain's folder, so the folder is the owner and a domain never sees another's.
- The runtime builds one compiled graph per served domain over a single shared checkpointer (`app/agent/runtime/core.py::init_graph_async`).

### 4. The agent graph

One turn is one walk through this state machine, 20 nodes in all, drawn here in two halves that meet at `agent` and `check_output`. The refusal exits are first-class: a turn that cannot be answered ends with a named reason, not a silent truncation. Dashed nodes end the turn.

**First half: the entry gates.** Nothing that spends runs until a turn has passed these.

```mermaid
flowchart TD
    start_n(["START"]) --> validate_input
    validate_input -->|"no valid tenant and principal"| reject_context
    validate_input -->|"empty message"| reject_input
    validate_input --> moderate_input
    moderate_input -->|"blocked"| reject_moderation
    moderate_input --> check_semantic_cache
    check_semantic_cache -->|"hit: already an answer"| to_output["check_output (second half)"]
    check_semantic_cache -->|"miss"| compact_history
    compact_history -->|"summary too large"| context_window_exceeded
    compact_history --> route_model
    route_model --> retrieve_context
    retrieve_context --> to_agent["agent (second half)"]

    classDef gate stroke-width:3px
    classDef exit stroke-dasharray:4 3
    class moderate_input gate
    class reject_context,reject_input,reject_moderation,context_window_exceeded exit
```

**Second half: the agent loop and the output gate.**

```mermaid
flowchart TD
    from_gates(["from the entry gates"]) --> agent
    cache_hit(["a semantic cache hit"]) --> check_output

    agent -->|"budget, no progress or guardrail refusal"| no_answer
    agent -->|"malformed tool batch"| tool_batch_guard
    tool_batch_guard --> agent
    agent -->|"any call that is not read_only"| human_approval
    agent -->|"read_only calls"| tools
    tools --> agent
    human_approval -->|"approved"| tools
    human_approval -->|"rejected"| agent
    human_approval -->|"cancelled"| cancelled_n(["the turn ends"])
    agent -->|"final answer"| check_output

    check_output -->|"answer rejected"| retry_output
    retry_output --> agent
    check_output -->|"same reason repeatedly"| retry_exhausted
    check_output -->|"answer stands"| finish_sg

    subgraph finish_sg["Finish: one parallel step"]
        suggest_followups
        write_semantic_cache
    end

    classDef gate stroke-width:3px
    classDef exit stroke-dasharray:4 3
    class human_approval gate
    class no_answer,retry_exhausted,suggest_followups,write_semantic_cache exit
```

- **Order is cost order.** Moderation runs before anything that spends, the cache before compaction (an LLM call), compaction before the router, so a refused or cached turn pays for none of them (patterns 25, 22, 41, 68).
- **`should_continue` checks the budgets first**, whatever the model said: iterations, tokens, dollars and repeated identical calls all end in `no_answer` (patterns 10, 34, 35). Anything not `read_only` goes to `human_approval` and no flag skips it (patterns 8, 15).
- **`check_output` is a registry of checks** (leaked prompt, too short, fabricated, uncited, a skipped required tool, ...), and `retry_output` loops back to `agent` with a corrective message tagged as a retry artifact so it is never mistaken for the person's words (pattern 5).
- **The two finish steps run in parallel**, and the cache write is skipped when the turn called a non-read-only tool (pattern 22).
- A nested `run_subagent` run builds the same graph with `SUBAGENT_PROFILE`: no cache pair, follow-ups, compaction or router. `GraphProfile` is in `app/agent/graph/build.py`.
- The full routing detail, with every dead end, is in [GRAPH_PATTERNS.md](GRAPH_PATTERNS.md#graph-flow).

### 5. Where state lives

A turn keeps nothing in process memory, which is why any worker can take any job and a dead worker's turn can be continued. A table says this more precisely than a diagram with a line from every process to every store.

| Store | What lives there | Written by | Read by |
|---|---|---|---|
| Postgres `checkpointer` | LangGraph's saved state: the conversation, and any approval pause | agent-worker | agent-worker, api (session reads) |
| Postgres `appdata` | `chat_sessions` (who owns a thread), `tool_call_dedup`, `usage_events`, budget holds and policies, each domain's tables, the billing tables | api (thread claim), agent-worker (ledger, events, holds, domain writes) | api (`GET /usage`), agent-worker, mcp-gateway |
| Redis Streams | `agent:requests:<domain>` jobs, `agent:results:<request_id>` events (5 minute TTL), `:dead` dead letters, `ingest:requests`, `ingest:results:<job_id>` | api (requests), workers (results) | workers (requests), api (results) |
| Redis plain keys | `agent:lock:<thread>`, `agent:cancel:<thread>`, `chat:submit_dedup:...`, Telegram's update list and poll offset | agent-worker (lock), api (cancel flag, dedup claim), the bot | the same |
| Redis semantic cache | a RediSearch vector index, keyed by tenant and principal | agent-worker (the finish step) | agent-worker (the cache check) |
| Redis limiter state | token buckets, the revocation list | authlimit | authlimit |
| Qdrant | documents and memories in one collection, told apart by a `kind` field; a separate skills collection. A collection's name carries the embedding identity of its vectors | ingest-worker (documents), agent-worker (memories, through the approved `remember` tool), `make index-skills` | agent-worker |
| MinIO or Spaces | uploaded files, as `<tenant>/<job_id>-<filename>` | api | ingest-worker |

- **Two Postgres databases, on purpose.** `checkpointer` is what makes an approval pause survive a restart; `appdata` is everything the application queries. Both are reached through PgBouncer in transaction mode, so no pool uses prepared statements (`tests/core/test_pooler_safe_sql.py`).
- **One Redis, three jobs**: Streams for the queues, a vector index for the semantic cache, plain keys for locks and flags. The names the producer and the consumer must agree on are plain data in `app/ports/outbound/jobs.py`.
- **A Qdrant collection records the embedding model its vectors were made with** (model, size, prefixes, revision) and the app refuses to mix two (pattern 67).

### 6. Where the non-negotiable rules are enforced

Three principles in the [constitution](.specify/memory/constitution.md) cannot be waived: I tenant isolation, II mandatory approval, IV exactly-once. Each is held by several independent layers, each closing a failure the one before it cannot see.

| Principle | The layers, outermost first |
|---|---|
| **I. Tenant isolation** fails closed | The caller is the front door's signed identity, and `X-Tenant-Id` is ignored in `identity` mode (`app/adapters/inbound/api/deps.py::get_ctx`). A thread belongs to whoever sent on it first, and anyone else gets the 404 an unknown id gets (`app/adapters/inbound/api/routers/chat.py::_require_conversation_owner`). `SecurityCtx` is stamped from request config, never from message text, and every tool re-checks it (`app/core/tool_runtime.py::ctx_or_refuse`). `Policy.lower` turns it into an `AccessScope` that the store applies inside the query as a pre-filter (`app/adapters/outbound/qdrant/qdrant_store.py::scope_filter`, `WHERE tenant = %s`), never as a Python filter afterwards |
| **II. Approval** is mandatory | Every tool declares `read_only`, `mutating` or `outward`, and an undeclared one counts as `outward`. `should_continue` routes any non-`read_only` call to `human_approval`, which pauses with `interrupt()` and persists the pause in the checkpoint (`app/agent/graph/routing.py::should_continue`, `app/agent/graph/hitl.py::make_human_approval_node`). Unattended callers decline and never approve; subagents get `read_only` tools only; the routing model can write `State.model_route` and nothing else |
| **IV. Exactly-once** side effects | `idempotent()` claims the `tool_call_id` before a write runs (`app/core/tool_idempotency.py::idempotent`). The domain store backs it with `tool_call_id UNIQUE` and `ON CONFLICT DO NOTHING`. A crashed turn is continued from its checkpoint, never restarted, so no new ids are minted. A timeout tells the agent to verify before retrying. A resubmitted request is deduplicated at the API |

The others, briefly: **III** tools are fixed and typed, with closed vocabularies and ids derived by code (no text-to-SQL); **V** failure is bounded by nine independent budgets and every degrade path is counted; **VI** retrieved or crawled text is framed as data, never instructions (`app/core/untrusted.py::frame_untrusted`).

### 7. Deployment and delivery

How a change reaches a running system. Creating or destroying droplets is always a human-run `terraform apply`; everything after a merge is automatic.

```mermaid
flowchart LR
    dev["Developer"] -->|"PR"| ci["GitHub Actions CI<br/>lint, typecheck, tests, integration,<br/>live, security scans"]
    ci -->|"merge to main"| deploy["deploy.yml<br/>build images, push to GHCR"]
    tf["Terraform<br/>human-run"] -.->|"provisions"| appdrop_sg
    tf -.->|"provisions"| obsdrop_sg
    deploy -->|"SSH, pull, redeploy"| appdrop_sg

    subgraph appdrop_sg["App droplet: docker-compose.prod.yml"]
        caddy["Caddy<br/>TLS, balances the api replicas"]
        procs["api xN, agent-worker xN, ingest-worker<br/>one image"]
        pgb["PgBouncer"]
        pg[("Postgres")]
        rd[("Redis Stack")]
        qd[("Qdrant")]
        lite["LiteLLM"]
        ml["ml-service"]
        side["otel-collector-agent, promtail,<br/>cAdvisor, node-exporter"]
    end

    subgraph obsdrop_sg["Observability droplet"]
        prom["Prometheus and Alertmanager<br/>routes to Slack"]
        loki["Loki"]
        graf["Grafana"]
    end

    llm[("LLM provider<br/>any OpenAI-compatible endpoint")]
    spaces[("DigitalOcean Spaces")]

    caddy --> procs
    procs --> pgb --> pg
    procs --> rd
    procs --> qd
    procs --> lite
    procs --> ml
    procs -->|"uploads"| spaces
    lite --> llm
    side -->|"private VPC"| obsdrop_sg
    prom --> graf
    loki --> graf
```

- **The same code runs in dev and prod**; only configuration and the compose file differ ([README](README.md#-environments)).
- **Schema changes are not applied by the deploy.** `postgres-init/*.sql` runs on a fresh volume only; the billing scripts have their own `make billing-migrate`, which runs before the code that needs them ([README](README.md#database-migrations)).
- **A graph change needs the workers drained first**: a renamed router or node can hide a mid-flight turn's pending node (pattern 16). The runbook is [deploy/README.md](deploy/README.md).

## Part 2: Flows

Reading the sequence diagrams: a numbered arrow is a call or a message; `Note` lines carry the rule that makes the step safe; `alt` branches are the failure and degrade paths, which is where most of the design lives.

### 8. A chat turn, end to end

The path every turn takes, whichever client sent it. The API never runs the graph: it publishes a job and relays the worker's events as SSE.

```mermaid
sequenceDiagram
    autonumber
    actor U as Person
    participant C as Client (web, CLI, Telegram)
    participant D as Front door
    participant A as api
    participant R as Redis
    participant W as agent-worker
    participant G as Graph
    participant L as LiteLLM and LLM
    participant P as Postgres

    U->>C: message
    C->>D: POST /chat/stream/queued with a bearer token
    D->>A: the request plus a signed identity (diagram 9)
    A->>P: claim the thread for this person, or verify they own it (404 if not)
    A->>R: SET NX submit-dedup claim, an identical resubmit streams the first turn
    A->>R: XADD agent:requests:domain {turn, ctx, text}
    A-->>C: open the SSE stream, start reading agent:results:request_id
    R->>W: XREADGROUP delivers the job to exactly one worker
    W->>R: SET NX thread lock, a busy thread fails fast with THREAD_BUSY
    W->>W: model priced, tenant allowance, reserve a budget hold (diagram 14)
    W->>G: astream_events(message, ctx)
    G->>G: validate, moderate, semantic cache lookup
    alt semantic cache hit
        G->>G: skip compaction, retrieval and the model, go to check_output
    else miss
        G->>G: compact history, route model, retrieve (diagram 13)
        loop until a final answer
            G->>L: chat call with this domain's tools, metered
            L-->>G: tokens or tool calls
            opt read_only tool calls
                G->>P: the tool runs under the person's ctx
            end
        end
    end
    G->>G: check_output: citations, leaks, deferral, retry if rejected
    G->>R: write the semantic cache, in parallel with follow-ups
    loop each event: token, tool_start, tool_end, citations, followups
        W->>R: XADD agent:results:request_id
        R-->>A: XREAD
        A-->>C: SSE data frame
    end
    W->>R: release the thread lock, then publish done
    A-->>C: done
    W->>R: XACK, always, even after a failure
```

- **The lock is released before the terminal event is published**, not after. A client that sees `done` or `approval_required` may fire the next request at once: releasing only after the handler returned rejected about a third of resumes as `THREAD_BUSY`, and releasing right after the publish still rejected about one in eight (`app/adapters/inbound/workers/agent_worker.py::process_request`).
- **A job is always acked**, even on failure: a redelivered, already-attempted job would re-run side effects. A worker that dies before the ack is the reclaim path (diagram 12).
- The first-event deadline turns "no worker is running" into an `error` event instead of an SSE connection that hangs.

### 9. Signing in and crossing the front door

Identity is proven once at the edge and handed on as a short-lived signed assertion. The API trusts nothing a client sends about who it is.

```mermaid
sequenceDiagram
    autonumber
    participant X as Client
    participant Z as Casdoor
    participant D as Caddy
    participant L as authlimit
    participant R as Redis
    participant A as api

    Note over X,Z: Sign in, once per session
    alt web UI
        X->>Z: authorization code with PKCE, in the browser
    else CLI
        X->>Z: browser sign-in, redirect caught on a loopback port
    else Telegram bot
        X->>Z: sign in as the person telegram-id, or as a linked account
    end
    Z-->>X: access token: audience is the API, owner is the tenant, sub is the person

    Note over X,A: Every request
    X->>D: request with Authorization: Bearer token
    D->>L: GET /v1/check, forward_auth, with the caller's Authorization
    L->>L: classify the route from routes.json
    L->>L: verify RS256 offline: issuer, audience, expiry, scope, lifetime cap
    L->>R: is this person revoked, then charge tenant and person buckets in one atomic call
    alt token invalid or person revoked
        L-->>D: 401 or 403
        D-->>X: refused, the API is never called
    else over the limit
        L-->>D: 429 with the wait time
        D-->>X: refused
    else allowed
        L-->>D: 200 and X-Internal-Identity, Ed25519 signed, valid 30 seconds
        D->>A: the request with that header and WITHOUT Authorization
        A->>L: fetch the signing keys from /internal/jwks.json, cached
        A->>A: verify the signature, build SecurityCtx from tenant and sub, ignore X-Tenant-Id
        A-->>X: response
    end
    Note over L,R: Limiter store down: serve, count, alert (a protection must not become an outage)
    Note over L: No usable Casdoor keys: refuse everything with 503 (a security check fails closed)
```

- **Fail closed where it is a check, open where it is a protection.** No usable signing keys or an untrustworthy revocation list is a 503; a Redis outage for the limiter is served and alerted. The API likewise answers 503, never a guess, when it cannot confirm its keys (pattern 86).
- Route classes (`services/authlimit/routes.json`): a turn and an upload are verified and limited; cancel and reads are verified and not limited; `/health` and the web page are public; the two MCP routes are limit-only.
- The CLI and Telegram are clients of this same path (spec 020): they never touch the queue or the graph (`tests/adapters/inbound/channels/test_channels_use_the_door.py`).

### 10. Human approval: pause, decide, resume

The pause is a checkpoint, not a held connection, so it survives a restart and any worker can resume it. The decision is a separate request with its own results stream.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as api
    participant R as Redis
    participant W as agent-worker
    participant G as Graph
    participant K as Checkpointer (Postgres)
    participant T as tools node

    Note over C,T: Phase 1, the turn pauses
    W->>G: run the turn
    G->>G: agent proposes a mutating or outward call
    G->>G: should_continue sends any non-read_only call to human_approval
    G->>K: interrupt() saves the paused state
    G-->>W: approval_required with the tool and its arguments
    W->>R: release the lock, publish approval_required (terminal), XACK
    R-->>A: event
    A-->>C: SSE approval_required, the stream ends

    Note over C,T: Phase 2, a person decides, at any time
    C->>A: POST /chat/resume {thread_id, approved}
    A->>A: ownership check, anyone else's thread is a 404
    A->>R: XADD a resume job on the same domain's stream
    A-->>C: a new SSE stream for the resume job
    R->>W: delivered to a worker in that domain's pool, not necessarily the first
    W->>R: acquire the thread lock
    W->>W: tenant allowance re-checked, new budget hold
    W->>G: resumability check: paused for real, schema version matches
    alt approved
        W->>G: Command(resume=True)
        G->>T: run the call, wrapped in idempotent() (diagram 11)
        T-->>G: result
        G->>G: agent writes the answer, check_output
        G-->>C: tokens then done
    else rejected
        W->>G: Command(resume=False)
        G->>G: a ToolMessage says "Rejected by human reviewer" and the agent reacts
    end

    opt the person cancels instead
        C->>A: POST /chat/cancel
        A->>R: set the cancel flag and XADD a cancel job
        W->>G: Command(resume="cancelled")
        G->>G: the run ends, the model never reacts, nothing is spent
    end
```

- **A new message on a paused thread is refused** with `PENDING_APPROVAL`; it is never auto-cancelled, because the pending call is by construction a write the person may not know about (`app/agent/runtime/stream.py::astream_events_turn`).
- **Cancel is a third outcome, not a rejection.** A rejection hands the model a message to react to; a cancel ends the run (pattern 36). It is never gated by the budget, so an over-budget tenant can always clear a paused thread.
- **Unattended callers** (Telegram with approvals off, queue jobs) decline up to a bounded number of pauses, then cancel; nothing ever approves on their behalf (pattern 8). With Telegram buttons only the tap of the person the buttons were shown to approves, once, in time (pattern 92).

### 11. A write tool runs exactly once

Approval says a write may happen; this says it happens once, however many times the call is replayed (a reclaimed resume, a crash, a retry).

```mermaid
sequenceDiagram
    autonumber
    participant N as tools node
    participant T as Tool (for example create_ticket)
    participant I as idempotent()
    participant L as ToolCallLedger (tool_call_dedup)
    participant S as Domain store (Postgres)
    participant M as Agent (LLM)

    N->>T: call with a tool_call_id
    T->>T: ctx_or_refuse, fail closed on a missing or malformed ctx
    T->>I: idempotent(tool_call_id, fn)
    I->>L: claim_or_cached: INSERT ... ON CONFLICT DO NOTHING
    alt the claim is new
        L-->>I: no cached result, run it
        alt fn finishes
            I->>S: INSERT with tool_call_id UNIQUE, ON CONFLICT DO NOTHING, scoped by tenant
            S-->>I: result text
            I->>L: store_result(tool_call_id, result)
            I-->>T: result
        else fn times out
            I-->>T: MutatingToolTimedOutError
            T-->>M: the effect may already have landed: check with a read-only tool before retrying
        end
    else this tool_call_id was seen before
        L-->>I: the first call's cached result
        I-->>T: that result, fn never runs
    else the ledger is unreachable
        I->>S: run fn anyway, counted and alerted as degraded
        Note right of S: the row-level UNIQUE is still a backstop
    end
```

- **The ledger is keyed by `tool_call_id`**, which the provider issues fresh per call, so two invocations with the same id are one invocation attempted again. It makes one invocation exactly-once, and nothing more: a whole turn re-run from scratch would get new ids, which is why a crashed turn is continued (diagram 12) rather than restarted.
- **It fails open on purpose**: a dedup outage must not block every write, and the schema's own uniqueness is the second line (`app/core/tool_idempotency.py::idempotent`).
- **The timeout case is the one an id cannot close**: the retry would be a fresh decision with a new id, so the agent is steered to verify instead (`MutatingToolTimedOutError`).
- `tests/domains/test_write_tools_contract.py` enumerates every domain's non-read-only tool, so a new write tool that skips `idempotent()` or its ctx check fails by name. The checklist is `.claude/rules/side-effect-tools.md`.

### 12. A worker dies mid-turn

Redis Streams delivers each job to one consumer and keeps it pending until acked. Every worker runs a reclaim loop that rescues what a dead one left behind, and the rescue is decided from the checkpoint.

```mermaid
sequenceDiagram
    autonumber
    participant W1 as worker A
    participant R as Redis (requests stream)
    participant W2 as worker B (reclaim loop)
    participant K as Checkpointer
    participant C as Client (still reading results)

    W1->>R: XREADGROUP takes the job
    W1->>W1: runs the turn, checkpointing as it goes
    Note over W1: the process dies (OOM, host loss) before XACK
    Note over R: the entry stays pending, the thread lock expires by its TTL
    loop every WORKER_RECLAIM_INTERVAL_SECONDS
        W2->>R: XAUTOCLAIM entries idle past AGENT_WORKER_RECLAIM_IDLE_SECONDS
    end
    R-->>W2: the abandoned entry
    W2->>K: read the thread's checkpoint, read-only
    alt a resume or cancel job
        W2->>R: republish it, the tool ledger makes a resume safe to repeat
    else a turn that cannot be continued safely
        Note over W2,K: unreadable, already answered, or paused at a real approval
        W2->>R: WORKER_LOST error on the results stream, copy to the dead-letter stream, XACK
        R-->>C: error event, the caller is told to retry
    else a turn with nothing checkpointed yet
        W2->>R: republish as turn, same request_id
    else a turn checkpointed and unfinished
        W2->>R: republish as turn_continue, same request_id
        R->>W2: delivered like any job
        W2->>K: continue the checkpointed run, never restart it
        Note over W2,K: a restart would ask the LLM again and mint new tool_call_ids that the ledger cannot connect to the crashed attempt
        W2->>R: events to the same results stream
        R-->>C: the stream carries on and the client sees no crash
    end
    Note over W2,R: past MAX_AUTO_RECLAIM_RETRIES any job is dead-lettered too
```

- **Continue, not restart** is what makes a crashed turn safe even after a write ran: passing no new input makes LangGraph resume the unfinished step and match writes already recorded instead of re-executing them (`app/agent/runtime/stream.py::astream_events_continue_turn`; the decision is `_classify_reclaimed_turn` in `app/adapters/inbound/workers/agent_worker.py`).
- **The conservative edge is deliberate**: a crash before the turn's own first message was checkpointed can look like the previous, finished turn and be dead-lettered. That is a false negative (a safe retry skipped), never a duplicate write.
- A graceful shutdown (SIGTERM) stops reading, lets in-flight jobs finish and ack, then closes the pools; only a hard kill leaves work for the reclaim loop.
- The ingest worker has the same loop, but a reclaimed ingest job is always safe to retry because point ids derive from content (diagram 16).

### 13. Retrieval: hybrid search under a tenant scope

The agent is given context before it reasons, and what it may see is decided by a scope the store enforces inside the query.

```mermaid
sequenceDiagram
    autonumber
    participant G as retrieve_context
    participant P as Policy (app/core/security.py)
    participant E as Embedder (LiteLLM embed alias, local BM25)
    participant Q as Qdrant
    participant M as ml-service (reranker)
    participant A as agent and check_output

    G->>P: lower(ctx, "documents") and lower(ctx, "memories")
    P-->>G: two AccessScopes: tenant and kind, plus owner and a retention cutoff for memories
    Note over G,Q: every read first checks that the collection's embedding identity matches this process
    par documents
        G->>E: embed_query (dense) and embed_sparse (BM25)
        G->>Q: query_points: dense and sparse prefetches, each with the scope as a PRE-filter, fused by RRF
        alt the sparse leg fails
            Q-->>G: dense only, counted as degraded
        end
        Q-->>G: candidates
        G->>M: rerank(query, passages)
        alt the reranker is down
            M-->>G: keep the fused order, counted as degraded
        end
        M-->>G: scores, keep the top k above MIN_RERANK_SCORE
    and memories
        G->>Q: the same hybrid search under the memory scope, with no rerank because one person's set is small
    end
    G->>G: number the sources [1] to [n] across both, keep each parent passage for the prompt
    G-->>A: a retrieved_document block framed as untrusted data, plus citation records
    A->>A: the model cites [n], check_output keeps only citations retrieval returned
```

- **The scope is lowered once and applied in the query.** A Python post-filter looks identical to a correct one until a cross-tenant leak; `DocumentIndex.search` cannot be called without an `AccessScope`, and the unscoped lookup for the skills catalog is a separate, named method (`search_catalog`).
- **Documents are tenant-wide; memories are also scoped to their owner** and invisible after `MEMORY_RETENTION_DAYS`, enforced at read time (patterns 17, 18, 33). The two searches run concurrently, so the pre-fetch costs the slower of them.
- **Retrieval degrades, never fails the turn**: it is enrichment, and the model can still call `search_docs` itself. Each degrade is counted (`agent_retrieval_degraded_total`).
- Small child chunks are what is matched; their larger parent passage is what the model sees (pattern 24).

### 14. Cost: allowance, hold, metering, backstop

Every model call is priced and recorded in one place, the dollar caps sum those records, and a second stop sits outside the app in case the app's own checks are wrong.

```mermaid
sequenceDiagram
    autonumber
    participant W as worker (turn entry)
    participant B as budgets (app/agent/cost)
    participant P as Postgres
    participant G as agent node
    participant ME as metered_invoke
    participant L as LiteLLM
    participant BP as billing package (optional)

    W->>B: is the chat model priced? (UNPRICED_MODEL_POLICY)
    W->>B: allowance for this tenant and person: day and month limits
    B->>P: sum usage_events in the window, plus holds of turns in flight
    opt credit billing is on
        B->>BP: admit(tenant)
        BP-->>B: the credit gate's answer
    end
    alt over a limit
        B-->>W: refuse: tenant_budget_exceeded, personal_budget_exceeded or insufficient_credits
    else within limits
        W->>P: reserve a hold of MAX_COST_USD_PER_TURN
        loop each model call in the turn
            G->>ME: metered_invoke(messages, alias)
            ME->>L: the call, tagged with user tenant:t/user:p
            L-->>ME: response and usage
            ME->>ME: price it from LiteLLM model info, an unknown price is never read as 0
            ME->>P: INSERT one usage_events row, ON CONFLICT DO NOTHING
            ME->>BP: record_usage_in, in the same transaction
            ME-->>G: response and cost
            G->>G: should_continue checks the token and dollar ceilings first
        end
        W->>P: release the hold, always, in a finally
    end
    Note over L: Backstop: the app holds a scoped key with its own max_budget. If the app's checks are wrong the gateway stops spend, reported as provider_budget_exceeded and never retried
```

- **One choke point**: `metered_invoke` is the only place a chat-model call is made, priced and recorded, and `tests/agent/cost/test_metering_choke_point.py` fails on a new unmetered call. A call site that must remember to meter will one day forget.
- **The same rows serve the caps, `GET /usage`, the wallet and the export**, so what is capped and what is billed cannot disagree (pattern 58).
- **Holds keep concurrent turns honest**: without them, N turns starting together all read the same "spent so far". A hold ages out so a killed worker's cannot refuse real turns forever.
- **Fail policy is a decision**: a failed allowance read follows `BUDGET_CHECK_FAILURE_POLICY` (open by default, counted and alerted); a lost usage event pages.
- The billing half (rate, wallet, export outbox) is an independent package the app reaches through `app/billing_integration/port.py` (`BILLING_INTEGRATION.md`).

### 15. Delegation: skills and subagents

Both extend what one turn can do without growing the graph. A skill is instructions loaded on demand; a subagent is an isolated, read-only run whose answer comes back as a tool result.

```mermaid
sequenceDiagram
    autonumber
    participant M as agent (LLM)
    participant T as tools node
    participant Q as Qdrant (skills collection)
    participant D as Disk (the domain's skills and subagents folders)
    participant S as Nested graph (SUBAGENT_PROFILE)

    Note over M,D: Skills: progressive disclosure
    M->>T: skill_search("onboarding brief")
    T->>Q: search_catalog: names and descriptions only
    T->>T: keep the skills this domain owns, judged from the disk catalog
    T-->>M: candidate names
    M->>T: use_skill(exact name), refused if skill_search has not run first
    T->>D: read SKILL.md, the disk is authoritative and Qdrant is only an index
    T-->>M: the instructions, followed with the model's other tools

    Note over M,S: Subagents: scoped, isolated delegation
    M->>T: run_subagent(name, task)
    T->>T: refuse if its model is unpriced under UNPRICED_MODEL_POLICY=block
    T->>S: a fresh message list, its own prompt and the task only, read_only tools only, the caller's ctx
    S->>S: moderate, retrieve, agent and tools, check_output, under its own smaller budgets
    S-->>T: final answer with its tokens and cost
    T->>T: add the spend to the parent turn's ceilings
    T-->>M: the answer as a tool result
```

- A subagent never sees the conversation, uses a throwaway in-memory checkpointer, and inherits the `SecurityCtx` unconditionally. A last message that still carries tool calls is reported `budget_exceeded`, never passed on as an answer (pattern 46; `app/agent/tools/run_subagent.py::run_subagent_impl`).
- `skill_search` and `use_skill` sit first in a domain's tool list because a small model ignored them when they were last, whatever the prompt said (`app/agent/tools/builtin.py::skill_tools_first`).

### 16. Document ingestion

Upload only gets bytes into object storage and a job onto a queue of its own, separate from chat turns; parsing and embedding happen in the ingest worker.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as api
    participant O as Object store (MinIO or Spaces)
    participant R as Redis (ingest:requests)
    participant W as ingest-worker
    participant E as Embedder (LiteLLM embed alias, local BM25)
    participant Q as Qdrant

    C->>A: POST /ingest/upload (files, topic) through the front door
    loop each file
        A->>A: allowed type, read in 1 MB chunks, reject past the size limit
        A->>O: upload tenant/job_id-filename
        A->>R: XADD an ingest job with the object key and ctx, never the bytes
        Note over A,O: if publishing fails, the blob is deleted
    end
    A-->>C: a job_id per file, or that file's own error
    C->>A: GET /ingest/stream/job_id
    R->>W: the consumer group delivers the job
    W->>R: progress: started
    W->>O: download the bytes
    W->>W: extract the text (PDF, DOCX), in a thread
    W->>W: parent-child chunking: small children are embedded, parents go to the prompt
    loop each batch of children
        W->>E: embed_documents (dense), sparse vectors for the whole document
        W->>R: progress: chunks embedded of total
    end
    W->>Q: upsert points tagged tenant, kind document and topic, ids derived from content
    W->>R: done with the chunk count, then XACK
    R-->>A: progress events
    A-->>C: SSE progress, then done
    Note over W,Q: content-derived ids make a re-run an overwrite, so a crashed ingest job is safely retried
```

- The ingest queue and the turn queue are separate streams and consumer groups, so a burst of uploads cannot starve chat.
- A failure here is usually deterministic (a corrupt file), so the worker surfaces the error at once instead of retrying inside the same attempt.

### 17. MCP gateway

The same two read-only MCP servers, served over HTTP to clients that are not a child process of the person using them. The gateway is an OAuth 2.1 resource server: it checks tokens and never issues one.

```mermaid
sequenceDiagram
    autonumber
    participant X as External MCP client
    participant Z as Casdoor
    participant D as Caddy and authlimit (limit-only)
    participant G as mcp-gateway
    participant P as Postgres

    X->>Z: OAuth 2.1 authorization code with PKCE and resource = the endpoint URL
    Z-->>X: token: audience is that URL, scope ecorp:read, owner is the tenant, sub is the person
    X->>D: POST /ecorp/mcp with Authorization: Bearer token
    D->>D: identify the caller only to meter and revoke, a missing or bad token is passed on
    D->>G: the request
    G->>G: verify: RS256 from an allow-list, issuer, audience is THIS endpoint, scope, expiry
    alt token invalid
        G-->>X: 401 with the metadata URL a client uses to find the authorization server
    else token valid
        G->>G: build_server(identity) from the token's owner and sub, never from an argument
        G->>P: a read-only tool, scoped by tenant
        P-->>G: rows
        G-->>X: the result, one audit line without arguments
    end
    Note over G: Stateless, so it scales behind Caddy. No write tool and no sandbox is behind it, because an outside caller does not pass the approval gate
```

- **The identity is the token's**, never a tool argument or the launcher's environment. Over stdio (`make mcp-serve`) a server acts for its launcher's `MCP_TENANT` and `MCP_PRINCIPAL`; the gateway never reads those (patterns 78, 82).
- **The audience check is what stops a token minted for one resource being replayed at another.** A token without a `resource` parameter has the client id as its audience and is refused.
- The reverse direction, the agent consuming a remote MCP catalog, treats the remote as untrusted input: every remote tool defaults to `outward`, catalogs can be pinned by hash, and replies are scrubbed and bounded (pattern 28).

### 18. Process start: wire() and the graph

How the composition root turns "a Protocol with no implementation" into a running process, and why a mis-wired deploy stops at boot instead of failing the first request that needs the missing piece.

```mermaid
sequenceDiagram
    autonumber
    participant E as Entrypoint (api, worker, channel, script)
    participant B as app/bootstrap.py
    participant S as Slots (app/ports)
    participant CK as Checkpointing port
    participant R as Domain registry
    participant G as build_graph

    E->>B: wire()
    B->>S: bind every Slot to its production adapter, opening no connection, safe to call twice
    E->>B: verify_wired()
    B-->>E: raises PortNotBoundError naming the port if one has no adapter
    E->>E: check the settings, for example identity mode needs AUTHLIMIT_KEYS_URL
    E->>CK: init_graph_async, open the Postgres checkpointer on THIS event loop
    loop each domain served
        E->>R: resolve(domain)
        R-->>E: its AgentManifest and DomainPlugin
        E->>G: build_graph(manifest, plugin, checkpointer)
        G-->>E: a compiled graph, a bad manifest stops the process here
    end
    E->>E: a worker then creates its consumer groups and starts the read loop and the reclaim loop
    Note over E: Shutdown: stop reading, finish and ack in-flight jobs, then close the adapters, the SQL pool and the checkpointer
```

- **Slots are read at call time and bind once**: `ticket_store.get().get_ticket(...)`. Tests bind a fake with `Slot.overriding` (`tests/fakes/`); `tests/core/test_bootstrap.py` fails on a port without an adapter and on an adapter that does not match its Protocol.
- **The checkpointer is opened on the loop that will use it**: its lock binds to the event loop that created it, so every process awaits `init_graph_async()` on its own loop (pattern 16).
- A worker builds one graph per domain in `AGENT_WORKER_DOMAINS` (one name, a list, or `all`) under one concurrency budget, and the domain of a job is the stream it came from, never a field of the payload (pattern 96). The API builds its default domain's graph at start.

## Where to go next

| For | Read |
|---|---|
| The layering rules, the port catalogue, how to add a port, a domain or a way in | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Every graph pattern, each with the bug that motivated it, and what is not built | [GRAPH_PATTERNS.md](GRAPH_PATTERNS.md) |
| Spend, caps and the gateway backstop | [COST_GOVERNANCE.md](COST_GOVERNANCE.md) |
| Sizing the worker pools and the connection budget | [WORKER_CONCURRENCY.md](WORKER_CONCURRENCY.md) |
| The edge service: failure policy, route classes, key rotation | [services/authlimit/README.md](services/authlimit/README.md) |
| The billing package and its seam | [BILLING_INTEGRATION.md](BILLING_INTEGRATION.md) |
| Running, deploying and operating it | [README.md](README.md), [deploy/README.md](deploy/README.md) |
| The rules a change must keep | [constitution](.specify/memory/constitution.md), [CLAUDE.md](CLAUDE.md) |

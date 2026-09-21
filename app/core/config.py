"""Typed, validated settings via Pydantic (pydantic-settings).

Values are read from the environment / .env. A single `Settings` instance is
created and its fields are also re-exported as module constants so existing
imports (`from app.core.config import QDRANT_URL`) keep working.
"""
from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Also load .env into os.environ so third-party SDKs that read env vars
# directly (e.g. the Langfuse client) pick up their keys.
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM proxy (OpenAI-compatible LiteLLM endpoint)
    openai_api_base: str = "http://localhost:4000/v1"
    openai_api_key: str = "sk-anything"
    chat_model: str = "chat"
    embed_model: str = "embed"

    # Qdrant
    qdrant_url: str = "http://localhost:6333"
    collection: str = "docs"

    # Multi-tenant isolation (app/core/security.py) — the tenant scripts/seed.py
    # stamps on seeded sample docs, so `make ingest` has a tenant without a
    # signup flow. A real deployment ingests per real tenant.
    default_tenant: str = "ecorp"

    # Langfuse
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # Durable checkpointer (app/agent/runtime.py) — survives a process restart.
    # Separate DB from the shared Postgres stack (own schema/lifecycle via
    # AsyncPostgresSaver.setup()); Postgres not SQLite because it's shared
    # across multiple processes (API + agent_worker.py replicas). Tests
    # degrade gracefully when unreachable (see test_durable_checkpoint.py).
    checkpointer_database_url: str = "postgresql://langfuse:langfuse@localhost:5432/checkpointer"

    # Hybrid retrieval (app/retrieval/embeddings.py, qdrant_store.py) — sparse
    # (BM25) + dense, fused server-side, then cross-encoder reranked. See
    # GRAPH_PATTERNS.md pattern 20.
    #
    # Sparse (BM25) runs locally via fastembed/ONNX — lexical scoring, no
    # serving-infra question, cached after first download.
    sparse_model: str = "Qdrant/bm25"
    # Reranking is a real ONNX cross-encoder and was a measured concurrency
    # bottleneck in-process (each call held a thread-pool slot plus ONNX's
    # own all-cores default under a 50-concurrent-turn burst). Moved to a
    # dedicated container (docker-compose.yml's `ml-service`,
    # docker/ml-service/main.py) to get it off agent-worker's GIL entirely.
    #
    # Hand-rolled FastAPI + onnxruntime, not TEI — TEI was tried
    # (BAAI/bge-reranker-base) and measured against this workload first:
    # ~94% rejected under a 100-simultaneous burst, ~84s to sustain 100
    # requests, 2.26GiB idle. This service handles that burst with zero
    # rejections, ~14s sustainably, ~200-420MiB. Also hosts a second ONNX
    # model (Llama Prompt Guard 2, moderation.py's ML injection layer) in
    # the same container, hence the generic name — see
    # docker/ml-service/main.py for full reasoning and measured numbers.
    ml_service_url: str = "http://localhost:8083"
    hybrid_prefetch_limit: int = 20  # candidates pulled per leg (dense, sparse) before fusion
    rerank_top_k: int = 5            # final results returned after rerank

    # Skill packages (app/agent/skills.py, pattern 45) — bundled SKILL.md
    # files, a *procedural capability* shipped with the app, not tenant data.
    # `skills_dir` is disk truth for a skill's body (loaded by use_skill);
    # `skills_collection` is a SEPARATE Qdrant collection (never `collection`
    # above) holding {name, description} for skill_search, rebuilt via
    # `make index-skills`.
    skills_dir: str = "skills"
    skills_collection: str = "skills"
    # 1, not a more generous top-K: live-verified (tests/live/
    # test_chat_ui.py::test_a_skill_is_found_and_followed, qwen2.5:3b, this
    # app's own real default) that even ONE distractor candidate alongside
    # the genuinely correct match is enough to make this small a model
    # hedge into "none of these are suitable" instead of committing to it
    # — k=2 reproduced the exact same failure as k=3; only k=1 (no
    # alternatives shown at all) passed reliably. Trades away a second
    # chance to correct an imperfect top-1 ranking for a small model that
    # commits confidently instead of second-guessing a right answer.
    skills_search_top_k: int = 1

    # Subagents (app/agent/subagents.py, pattern 46) — bundled AGENT.md files,
    # each a scoped nested agent run `run_subagent` can delegate to.
    # `subagents_dir` is disk truth like `skills_dir`, but no separate Qdrant
    # collection — a subagent's description is embedded in run_subagent's
    # own tool schema.
    subagents_dir: str = "subagents"

    # Structured-data tool (app/agent/sql_store.py) — a SEPARATE database in
    # the same Postgres as LiteLLM/Langfuse, not sharing their schema.
    appdata_database_url: str = "postgresql://langfuse:langfuse@localhost:5432/appdata"

    # Semantic cache (app/retrieval/semantic_cache.py) — Redis Stack
    # (RediSearch, for vector KNN), not plain Redis.
    redis_url: str = "redis://localhost:6379"
    semantic_cache_similarity_threshold: float = 0.95  # cosine; a query must
    # be nearly identical in meaning to reuse a cached answer, not just topically close
    semantic_cache_ttl_seconds: int = 3600

    # Cross-session memory retention (app/core/security.py, app/agent/memory.py)
    # — a memory older than this is invisible at RECALL time (Policy.lower),
    # not just eventually removed by delete_memories (pattern 33).
    memory_retention_days: int = 365

    # Per-run cost ceiling (graph_routing.py::should_continue, pattern 35) —
    # a HARD stop before the next tool/LLM call, independent of
    # MAX_TOKENS_PER_TURN (a token cap bounds work; a dollar cap bounds what
    # that work costs on the configured model tier). $0 for local Ollama
    # models, so it only starts mattering once OPENAI_API_BASE points at a
    # real paid provider.
    max_cost_usd_per_turn: float = 0.50

    # Per-run cost ceiling for a NESTED subagent run (tools.py::run_subagent,
    # pattern 46) — smaller than max_cost_usd_per_turn above. A subagent's
    # spend is recorded to the ledger for audit but NOT folded back into the
    # parent turn's live total_cost_usd (disclosed gap, pattern 46), so this
    # is the only real enforcement on one subagent call's spend.
    max_subagent_cost_usd_per_run: float = 0.15

    # Per-tenant ceiling across MANY turns (runtime.py's
    # _tenant_over_daily_budget), rolling 24h window against usage_ledger —
    # distinct from MAX_COST_USD_PER_TURN, which only sees one turn at a
    # time. Same "$0 on local Ollama" note applies.
    max_cost_usd_per_tenant_per_day: float = 20.0

    # Outermost per-turn wall-clock bound (app/agent/runtime.py) — catches a
    # turn stuck inside one slow LLM/tool call, which MAX_ITERATIONS/
    # RECURSION_LIMIT never see. Configurable (unlike the cost/token
    # ceilings, which are code-level constants bounding spend): it's a pure
    # operational timeout, and a slow backend has a legitimate reason to
    # widen it. `tests/live/conftest.py` overrides this since a small
    # model's inference time on shared CI can exceed the 60s local default.
    request_timeout_seconds: int = 60

    # Object storage for uploaded documents (app/ingestion/object_store.py) —
    # MinIO, self-hosted S3-compatible (docker-compose's `minio` service).
    # Defaults match that service's own MINIO_ROOT_USER/MINIO_ROOT_PASSWORD.
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "ingest-uploads"
    minio_secure: bool = False  # plain http:// for local docker-compose; a real deployment sets this True

    # Telegram channel (app/channels/telegram.py, pattern 42) — empty by
    # default so the bot refuses to start rather than run with no way to
    # authenticate. Create a bot via @BotFather. The one surface in this app
    # that necessarily reaches the public internet.
    telegram_bot_token: str = ""

    # Which domain (app/domains/registry.py) a process boots its shared
    # graph singleton against — read by telegram.py (a generalized gateway,
    # pattern 23/42) and agent_worker.py (which worker POOL reads which
    # requests stream; `POST /chat/stream/queued` instead picks the domain
    # per-request via an X-Domain header). "ecorp" is the default; "support"
    # and "sales" are the other demo domains, "ops" is registered too though
    # its own scripts don't need a channel. Unknown name fails loud at
    # startup (registry.py::resolve_domain).
    agent_domain: str = "ecorp"

    # Ops bot (app/domains/ops/) — queries this repo's own Prometheus
    # (`make obs-up`) via its HTTP query API for the "pull metrics
    # dashboards" tool. Not the otel-collector's OTLP port — that's a push
    # target, not queryable.
    prometheus_url: str = "http://localhost:9090"

    # Sandbox session defaults (app/domains/sandbox_session.py, shared by
    # ops/support/sales, pattern 50) — image and lifetime for the one
    # OpenSandbox sandbox each investigation thread lazily creates and
    # reuses. A fresh sandbox has NO network egress by default, so needed
    # packages are baked into a custom image instead (docker/sandbox.Dockerfile,
    # `make sandbox-build`, currently python:3.12-slim + numpy + pandas) —
    # bump both if a real deployment needs more. No domain prefix: sandbox
    # creation was never ops-specific, only its original wiring was.
    sandbox_image: str = "agent-core-demo-sandbox:latest"
    sandbox_ttl_seconds: int = 1800  # 30 min — enough for several human-approval
    # pauses, short enough that an abandoned sandbox doesn't linger.

    # Team-channel notifications (app/domains/notify.py) — used by the ops
    # bot's post_to_team_channel and support/sales's escalate/handoff tools.
    # Empty by default: degrades to a local file+log sink rather than
    # refusing to start (opposite posture from TELEGRAM_BOT_TOKEN) — a demo
    # sink nobody's on-call actually depends on.
    slack_webhook_url: str = ""

    # API-layer protection (app/api/main.py) — one client/tenant must not
    # flood the shared Redis Streams queue or starve other tenants. Per-tenant
    # (X-Tenant-Id), backed by the same Redis, not in-process memory (which
    # would stop working once more than one `uvicorn` process runs, pattern
    # 43). Fails OPEN if Redis is unreachable, same posture as
    # semantic_cache.py/moderation.py.
    rate_limit_per_minute: int = 30

    # Concurrent turns ONE agent_worker.py process runs at once
    # (asyncio.Semaphore-bounded). Env-configurable so load tests can dial it
    # without a redeploy — production sizing depends on the downstream LLM
    # backend's real concurrency (see loadtest/fake_llm_server.py on Ollama's
    # `-np 1` ceiling vs a genuinely concurrent backend).
    agent_worker_max_concurrency: int = 10

    # app/agent/runtime.py::_open_checkpointer's AsyncConnectionPool size.
    # Raising this ALONE won't raise checkpoint throughput past one
    # connection's worth: AsyncPostgresSaver wraps every checkpoint I/O in
    # one `asyncio.Lock()` per saver instance guarding pool.connection()
    # itself (verified against langgraph's source) — only one turn's
    # checkpoint I/O is ever in flight per process regardless of pool size.
    # Real additional throughput comes from more agent_worker.py REPLICAS
    # (pattern 43), each with its own saver/lock. Still worth matching to
    # agent_worker_max_concurrency so the two numbers don't drift apart.
    checkpointer_pool_max_size: int = 10

    # Concurrent ingest jobs ONE ingest_worker.py process runs at once, same
    # asyncio.Semaphore shape as agent_worker_max_concurrency. Blocking
    # stages (MinIO download, PDF/DOCX extraction) run via asyncio.to_thread,
    # which overlaps I/O but doesn't parallelize CPU-bound extraction (GIL).
    # Matched to the same default (10) as a starting point, not backed by
    # "matches backend concurrency" reasoning like the worker setting above;
    # lower it (and add replicas) if extraction, not I/O, dominates.
    ingest_worker_max_concurrency: int = 10

    # Cap on app/job_queue/queue.py::get_client()'s connection pool —
    # redis-py's default (100) is easy to blow through since every
    # POST /chat/stream/queued SSE connection holds a pooled connection for
    # its turn's full blocking-read duration (XREAD BLOCK), not a quick
    # round trip. Verified: 250 concurrent requests against the redis-py
    # default produced an 82% MaxConnectionsError failure rate past ~100.
    # Comfortably above normal load-test needs, still finite so one process
    # can't exhaust Redis's own maxclients.
    redis_max_connections: int = 300

    # Comma-separated CORS allowed origins, or "*" (default — fine for a
    # local demo; the built-in web UI is same-origin and never needs CORS).
    # A real multi-origin deployment narrows this (see .env.prod.example).
    cors_allowed_origins: str = "*"

    # POST /ingest/upload's per-file cap, enforced before any MinIO write —
    # an unbounded upload is a memory/storage exhaustion vector.
    max_upload_size_mb: int = 25

    # POST /ingest/upload's per-REQUEST file-count cap, mirrored client-side
    # by the upload form. A UX/abuse guard on one HTTP request, distinct from
    # ingest_worker_max_concurrency — raising this doesn't add throughput,
    # just changes how many files one submission may batch.
    max_upload_files_per_request: int = 5

    # OpenSandbox MCP bridge (app/domains/sandbox_tools.py, pattern 50) —
    # `--domain` passed to the `opensandbox-mcp` stdio bridge process
    # (mcp/client.py::load_remote_tools spawns it), i.e. the host:port
    # `opensandbox-server` listens on. Localhost:PORT since the bridge is
    # spawned by this process and always reaches the server via a published
    # port, not an in-network name.
    #
    # 8090, not OpenSandbox's packaged default of 8080 — this repo's own
    # open-webui already binds host port 8080. Before opensandbox_api_key
    # existed, a stray server failing to bind 8080 silently landed requests
    # on open-webui's uvicorn instead (same-shaped 405), indistinguishable
    # from a real error. `make sandbox-up` publishes this exact port.
    opensandbox_mcp_domain: str = "localhost:8090"

    # Bearer token `opensandbox-mcp` sends OpenSandbox's server
    # (sandbox_tools.py appends `--api-key`). docker-compose.yml's
    # opensandbox-server sets the same value via OPENSANDBOX_SERVER_API_KEY,
    # which overrides the TOML's `server.api_key`. Required: current
    # opensandbox-server releases refuse to start without one set — no
    # insecure fallback, unlike SLACK_WEBHOOK_URL, since an unauthenticated
    # sandbox executor is a meaningfully worse default.
    opensandbox_api_key: str = ""

    # crawl4ai's dockerized server (app/ingestion/web_crawler.py,
    # docker-compose.yml's default-profile `crawl4ai` service). 11235 is the
    # image's real listen port (not Crawl4aiDockerClient's stale
    # localhost:8000 constructor default).
    crawl4ai_server_url: str = "http://localhost:11235"

    # Bearer token for the crawl4ai server above. Required: crawl4ai 0.9.0+
    # is secure-by-default — without a matching token the server binds
    # loopback-only inside its container, so the published port just
    # connection-resets instead of a clear auth error.
    crawl4ai_api_token: str = ""

    # OTel metrics export (app/core/telemetry.py) — OTLP/HTTP base URL (no
    # /v1/metrics suffix; configure_telemetry appends it). Points at the
    # shared otel-collector in a real deployment; localhost:4318 matches
    # this file's usual "host talks to docker-compose service via published
    # port" pattern.
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"


settings = Settings()

# Backward-compatible module-level constants.
OPENAI_API_BASE = settings.openai_api_base
OPENAI_API_KEY = settings.openai_api_key
CHAT_MODEL = settings.chat_model
EMBED_MODEL = settings.embed_model
QDRANT_URL = settings.qdrant_url
COLLECTION = settings.collection
DEFAULT_TENANT = settings.default_tenant
LANGFUSE_HOST = settings.langfuse_host
LANGFUSE_PUBLIC_KEY = settings.langfuse_public_key
LANGFUSE_SECRET_KEY = settings.langfuse_secret_key
CHECKPOINTER_DATABASE_URL = settings.checkpointer_database_url
SPARSE_MODEL = settings.sparse_model
ML_SERVICE_URL = settings.ml_service_url
HYBRID_PREFETCH_LIMIT = settings.hybrid_prefetch_limit
RERANK_TOP_K = settings.rerank_top_k
SKILLS_DIR = settings.skills_dir
SKILLS_COLLECTION = settings.skills_collection
SKILLS_SEARCH_TOP_K = settings.skills_search_top_k
SUBAGENTS_DIR = settings.subagents_dir
APPDATA_DATABASE_URL = settings.appdata_database_url
REDIS_URL = settings.redis_url
SEMANTIC_CACHE_SIMILARITY_THRESHOLD = settings.semantic_cache_similarity_threshold
SEMANTIC_CACHE_TTL_SECONDS = settings.semantic_cache_ttl_seconds
MEMORY_RETENTION_DAYS = settings.memory_retention_days
MAX_COST_USD_PER_TURN = settings.max_cost_usd_per_turn
MAX_SUBAGENT_COST_USD_PER_RUN = settings.max_subagent_cost_usd_per_run
MAX_COST_USD_PER_TENANT_PER_DAY = settings.max_cost_usd_per_tenant_per_day
REQUEST_TIMEOUT_SECONDS = settings.request_timeout_seconds
TELEGRAM_BOT_TOKEN = settings.telegram_bot_token
AGENT_DOMAIN = settings.agent_domain
PROMETHEUS_URL = settings.prometheus_url
SLACK_WEBHOOK_URL = settings.slack_webhook_url
MINIO_ENDPOINT = settings.minio_endpoint
MINIO_ACCESS_KEY = settings.minio_access_key
MINIO_SECRET_KEY = settings.minio_secret_key
MINIO_BUCKET = settings.minio_bucket
MINIO_SECURE = settings.minio_secure
RATE_LIMIT_PER_MINUTE = settings.rate_limit_per_minute
AGENT_WORKER_MAX_CONCURRENCY = settings.agent_worker_max_concurrency
CHECKPOINTER_POOL_MAX_SIZE = settings.checkpointer_pool_max_size
INGEST_WORKER_MAX_CONCURRENCY = settings.ingest_worker_max_concurrency
REDIS_MAX_CONNECTIONS = settings.redis_max_connections
CORS_ALLOWED_ORIGINS = settings.cors_allowed_origins
MAX_UPLOAD_SIZE_MB = settings.max_upload_size_mb
MAX_UPLOAD_FILES_PER_REQUEST = settings.max_upload_files_per_request
OPENSANDBOX_MCP_DOMAIN = settings.opensandbox_mcp_domain
OPENSANDBOX_API_KEY = settings.opensandbox_api_key
SANDBOX_IMAGE = settings.sandbox_image
SANDBOX_TTL_SECONDS = settings.sandbox_ttl_seconds
CRAWL4AI_SERVER_URL = settings.crawl4ai_server_url
CRAWL4AI_API_TOKEN = settings.crawl4ai_api_token
OTEL_EXPORTER_OTLP_ENDPOINT = settings.otel_exporter_otlp_endpoint

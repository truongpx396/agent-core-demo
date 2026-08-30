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
    # stamps on the seeded sample docs. A real deployment ingests per real
    # tenant; this exists so `make ingest` still has a tenant to stamp
    # without inventing a signup flow just to run the demo.
    default_tenant: str = "acme"

    # Langfuse
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # Durable checkpointer (app/agent/runtime.py) — survives a process restart,
    # unlike the in-memory MemorySaver build_graph() defaults to for tests.
    # A SEPARATE database in the same Postgres the stack already runs for
    # LiteLLM/Langfuse/appdata (see postgres-init/05-checkpointer-db.sql),
    # not sharing their schema — AsyncPostgresSaver.setup() owns and
    # migrates its own multi-table schema (checkpoints/checkpoint_blobs/
    # checkpoint_writes), which shouldn't share a lifecycle with
    # hand-maintained app tables. Postgres, not a SQLite file, because this
    # checkpoint store is now shared across multiple OS processes at once
    # (the API process plus one or more independently-scaled
    # app/turns/agent_worker.py processes) — a single SQLite file's
    # writer-locking is fragile under that; Postgres is built for it. The
    # test suite degrades gracefully when this isn't reachable (see
    # tests/agent/test_durable_checkpoint.py's connectivity-probe skip fixture),
    # so `pytest -q` still needs no live services for its non-checkpoint
    # coverage.
    checkpointer_database_url: str = "postgresql://langfuse:langfuse@localhost:5432/checkpointer"

    # Hybrid retrieval (app/retrieval/embeddings.py, app/retrieval/qdrant_store.py) — sparse
    # (BM25) + dense, fused server-side, then cross-encoder reranked.
    # Both models run locally via fastembed/ONNX — no network call, no
    # LiteLLM route, downloaded once and cached (same "offline after first
    # pull" shape as the Ollama models). See GRAPH_PATTERNS.md pattern 20.
    sparse_model: str = "Qdrant/bm25"
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    hybrid_prefetch_limit: int = 20  # candidates pulled per leg (dense, sparse) before fusion
    rerank_top_k: int = 5            # final results returned after rerank

    # Skill packages (app/agent/skills.py, GRAPH_PATTERNS.md pattern 45) — a
    # bundled catalog of SKILL.md files (name/description frontmatter + a
    # markdown instruction body), each a *procedural capability* shipped with
    # the app, not tenant data (no SecurityCtx involved, same as calculator).
    # `skills_dir` is disk truth for a skill's full body (loaded by
    # use_skill); `skills_collection` is a SEPARATE Qdrant collection —
    # never `collection` above — holding just {name, description} for
    # skill_search's hybrid search, rebuilt via `make index-skills`.
    skills_dir: str = "skills"
    skills_collection: str = "skills"
    skills_search_top_k: int = 3

    # Subagents (app/agent/subagents.py, GRAPH_PATTERNS.md pattern 46) — a
    # bundled catalog of AGENT.md files (name/description/tools/model
    # frontmatter + a markdown system-prompt body), each a scoped, isolated
    # nested agent run the `run_subagent` tool can delegate a task to.
    # `subagents_dir` is disk truth, same "on disk, not a search index"
    # shape as `skills_dir` — but unlike skills there is no separate Qdrant
    # collection for discovery, since a subagent's short description is
    # embedded directly in run_subagent's own tool schema.
    subagents_dir: str = "subagents"

    # Structured-data tool (app/agent/sql_store.py) — a SEPARATE database in the
    # same Postgres the stack already runs for LiteLLM/Langfuse (see
    # postgres-init/02-appdata.sql), not sharing their schema.
    appdata_database_url: str = "postgresql://langfuse:langfuse@localhost:5432/appdata"

    # Semantic cache (app/retrieval/semantic_cache.py) — Redis Stack (RediSearch, for
    # vector KNN), not plain Redis.
    redis_url: str = "redis://localhost:6379"
    semantic_cache_similarity_threshold: float = 0.95  # cosine; a query must
    # be nearly identical in meaning to reuse a cached answer, not just topically close
    semantic_cache_ttl_seconds: int = 3600

    # Cross-session memory retention (app/core/security.py, app/agent/memory.py) — a
    # memory older than this is invisible at RECALL time (Policy.lower),
    # not just eventually removed by a sweep script calling
    # app/agent/memory.py::delete_memories (GRAPH_PATTERNS.md pattern 33).
    memory_retention_days: int = 365

    # Per-run cost ceiling (app/agent/graph.py::should_continue, GRAPH_PATTERNS.md
    # pattern 35) — a HARD stop, enforced before the next tool/LLM call,
    # independent of MAX_TOKENS_PER_TURN: a token cap bounds work done, a
    # dollar cap bounds what that work is actually worth on whichever
    # model tier is configured (app/agent/meter.py::PRICE_PER_1K_TOKENS_USD).
    # $0 for every locally-run Ollama model this demo's own docker-compose
    # ships, so this never trips in the default local setup — it starts
    # mattering the moment OPENAI_API_BASE points at a real paid provider.
    max_cost_usd_per_turn: float = 0.50

    # Per-run cost ceiling for a NESTED subagent run (app/agent/tools.py::
    # run_subagent, GRAPH_PATTERNS.md pattern 46) — deliberately separate
    # from, and smaller than, max_cost_usd_per_turn above: a subagent's own
    # spend is recorded to the usage ledger for audit but is NOT folded back
    # into the live total_cost_usd the parent turn's should_continue
    # enforces in real time (a disclosed gap, see pattern 46), so this is
    # the one enforcement point actually bounding what one subagent call can
    # spend.
    max_subagent_cost_usd_per_run: float = 0.15

    # Per-tenant ceiling across MANY turns (app/agent/runtime.py's
    # _tenant_over_daily_budget), a rolling 24-hour window against
    # app/agent/meter.py's usage_ledger — distinct from MAX_COST_USD_PER_TURN
    # above, which only ever sees ONE turn at a time and has no memory of
    # what a tenant already spent on turns before it. Same "$0 for every
    # locally-run Ollama model" note as MAX_COST_USD_PER_TURN: this never
    # trips against this demo's own docker-compose stack, only once
    # OPENAI_API_BASE points at a real paid provider.
    max_cost_usd_per_tenant_per_day: float = 20.0

    # Object storage for uploaded documents (app/ingestion/object_store.py) — MinIO,
    # a self-hosted S3-compatible store (docker-compose's `minio` service),
    # consistent with this app's fully-offline posture everywhere else.
    # Defaults match that service's own MINIO_ROOT_USER/MINIO_ROOT_PASSWORD.
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "ingest-uploads"
    minio_secure: bool = False  # plain http:// for local docker-compose; a real deployment sets this True

    # Telegram channel (app/channels/telegram.py, GRAPH_PATTERNS.md pattern
    # 42) — empty by default, so the bot refuses to start rather than
    # silently running with no way to authenticate to Telegram's API. Create
    # a bot via @BotFather to get a real token; this is the one surface in
    # this app that necessarily reaches the public internet, unlike the rest
    # of the stack (fully local via docker-compose).
    telegram_bot_token: str = ""

    # Which domain (app/domains/registry.py) a process boots its shared
    # graph singleton against — read by app/channels/telegram.py, which is
    # now a generalized gateway rather than an Acme-only one (GRAPH_PATTERNS.md
    # pattern 23/42). "acme" is this app's own existing default; the demo
    # example domains this app ships alongside it are "support" and "sales"
    # (see app/domains/support/, app/domains/sales/) — "ops" is registered
    # too, mostly so it CAN be chatted with, though its own use case
    # (scripts/ops_digest.py, scripts/ops_investigate.py) doesn't need a
    # channel at all. An unknown name fails loud at process start
    # (app/domains/registry.py::resolve_domain), same discipline as
    # TELEGRAM_BOT_TOKEN's missing-config check above.
    agent_domain: str = "acme"

    # Ops bot (app/domains/ops/) — queries the Prometheus this repo already
    # runs (docker-compose.observability.yml, `make obs-up`) for its
    # "pull metrics dashboards" tool, over Prometheus's own HTTP query API.
    # Not the otel-collector's OTLP port — that's a push target, not
    # queryable; Prometheus is what actually stores/serves these numbers.
    prometheus_url: str = "http://localhost:9090"

    # Team-channel notifications (app/domains/notify.py) — used by the ops
    # bot's post_to_team_channel tool and the support/sales domains'
    # escalate_to_human/handoff_to_human. Empty by default: the notifier
    # degrades to a local file+log sink rather than refusing to start (the
    # OPPOSITE posture from TELEGRAM_BOT_TOKEN above) — same "additive,
    # never load-bearing" relationship this app already has with Langfuse/
    # observability, appropriate for a demo notification sink nobody's
    # on-call rotation actually depends on.
    slack_webhook_url: str = ""

    # API-layer protections (app/api/main.py) — a single client (or one
    # misbehaving/compromised tenant) must not be able to flood the shared
    # Redis Streams queue (app/turns/queue.py) or starve every other tenant's
    # turns. Rate limiting is per-tenant (X-Tenant-Id), backed by the SAME
    # Redis this app already depends on — not in-process memory, which
    # would silently stop working the moment more than one `uvicorn`
    # process is running (this app's own scaling story, see
    # GRAPH_PATTERNS.md pattern 43) since each process would count hits
    # independently. Fails OPEN if Redis itself is unreachable (see
    # app/api/main.py's limiter construction) — the same "an ancillary system's
    # outage must not take down the core turn" posture
    # app/retrieval/semantic_cache.py/app/agent/moderation.py already established, applied
    # here to a THIRD ancillary system.
    rate_limit_per_minute: int = 30

    # Comma-separated list of allowed origins for CORS, or "*" for any
    # (the default — appropriate for a local demo with no other frontend
    # pointed at it; a real multi-origin deployment narrows this). The
    # built-in web UI (app/api/static/index.html) is same-origin and never
    # needs CORS at all — this only matters for a DIFFERENT origin calling
    # this API directly from a browser.
    cors_allowed_origins: str = "*"

    # POST /ingest/upload's per-file cap, enforced before any MinIO write
    # (app/api/main.py) — an unbounded upload is a memory/storage exhaustion
    # vector, not just a slow request.
    max_upload_size_mb: int = 25

    # OTel metrics export (app/core/telemetry.py) — the OTLP/HTTP base URL
    # (no /v1/metrics suffix; configure_telemetry appends it) every
    # long-running process pushes its metrics to. Points at the shared
    # otel-collector (docker-compose.observability.yml) in a real
    # deployment; localhost:4318 here matches the "host process talks to a
    # docker-compose service via its published port" pattern this file
    # already uses for every other dependency (QDRANT_URL, REDIS_URL, ...).
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
RERANK_MODEL = settings.rerank_model
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
CORS_ALLOWED_ORIGINS = settings.cors_allowed_origins
MAX_UPLOAD_SIZE_MB = settings.max_upload_size_mb
OTEL_EXPORTER_OTLP_ENDPOINT = settings.otel_exporter_otlp_endpoint

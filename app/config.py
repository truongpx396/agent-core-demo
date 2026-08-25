"""Typed, validated settings via Pydantic (pydantic-settings).

Values are read from the environment / .env. A single `Settings` instance is
created and its fields are also re-exported as module constants so existing
imports (`from app.config import QDRANT_URL`) keep working.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

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

    # Multi-tenant isolation (app/security.py) — the tenant app/ingest.py
    # stamps on the seeded sample docs. A real deployment ingests per real
    # tenant; this exists so `make ingest` still has a tenant to stamp
    # without inventing a signup flow just to run the demo.
    default_tenant: str = "acme"

    # Langfuse
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # Durable checkpointer (app/agent.py) — survives a process restart,
    # unlike the in-memory MemorySaver build_graph() defaults to for tests.
    # A SEPARATE database in the same Postgres the stack already runs for
    # LiteLLM/Langfuse/appdata (see postgres-init/05-checkpointer-db.sql),
    # not sharing their schema — AsyncPostgresSaver.setup() owns and
    # migrates its own multi-table schema (checkpoints/checkpoint_blobs/
    # checkpoint_writes), which shouldn't share a lifecycle with
    # hand-maintained app tables. Postgres, not a SQLite file, because this
    # checkpoint store is now shared across multiple OS processes at once
    # (the API process plus one or more independently-scaled
    # app/agent_worker.py processes) — a single SQLite file's
    # writer-locking is fragile under that; Postgres is built for it. The
    # test suite degrades gracefully when this isn't reachable (see
    # tests/test_durable_checkpoint.py's connectivity-probe skip fixture),
    # so `pytest -q` still needs no live services for its non-checkpoint
    # coverage.
    checkpointer_database_url: str = "postgresql://langfuse:langfuse@localhost:5432/checkpointer"

    # Hybrid retrieval (app/embeddings.py, app/qdrant_store.py) — sparse
    # (BM25) + dense, fused server-side, then cross-encoder reranked.
    # Both models run locally via fastembed/ONNX — no network call, no
    # LiteLLM route, downloaded once and cached (same "offline after first
    # pull" shape as the Ollama models). See GRAPH_PATTERNS.md pattern 20.
    sparse_model: str = "Qdrant/bm25"
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    hybrid_prefetch_limit: int = 20  # candidates pulled per leg (dense, sparse) before fusion
    rerank_top_k: int = 5            # final results returned after rerank

    # Structured-data tool (app/sql_store.py) — a SEPARATE database in the
    # same Postgres the stack already runs for LiteLLM/Langfuse (see
    # postgres-init/02-appdata.sql), not sharing their schema.
    appdata_database_url: str = "postgresql://langfuse:langfuse@localhost:5432/appdata"

    # Semantic cache (app/semantic_cache.py) — Redis Stack (RediSearch, for
    # vector KNN), not plain Redis.
    redis_url: str = "redis://localhost:6379"
    semantic_cache_similarity_threshold: float = 0.95  # cosine; a query must
    # be nearly identical in meaning to reuse a cached answer, not just topically close
    semantic_cache_ttl_seconds: int = 3600

    # Cross-session memory retention (app/security.py, app/memory.py) — a
    # memory older than this is invisible at RECALL time (Policy.lower),
    # not just eventually removed by a sweep script calling
    # app/memory.py::delete_memories (GRAPH_PATTERNS.md pattern 33).
    memory_retention_days: int = 365

    # Per-run cost ceiling (app/graph.py::should_continue, GRAPH_PATTERNS.md
    # pattern 35) — a HARD stop, enforced before the next tool/LLM call,
    # independent of MAX_TOKENS_PER_TURN: a token cap bounds work done, a
    # dollar cap bounds what that work is actually worth on whichever
    # model tier is configured (app/meter.py::PRICE_PER_1K_TOKENS_USD).
    # $0 for every locally-run Ollama model this demo's own docker-compose
    # ships, so this never trips in the default local setup — it starts
    # mattering the moment OPENAI_API_BASE points at a real paid provider.
    max_cost_usd_per_turn: float = 0.50

    # Object storage for uploaded documents (app/object_store.py) — MinIO,
    # a self-hosted S3-compatible store (docker-compose's `minio` service),
    # consistent with this app's fully-offline posture everywhere else.
    # Defaults match that service's own MINIO_ROOT_USER/MINIO_ROOT_PASSWORD.
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "ingest-uploads"
    minio_secure: bool = False  # plain http:// for local docker-compose; a real deployment sets this True

    # Telegram channel (app/telegram_channel.py, GRAPH_PATTERNS.md pattern
    # 42) — empty by default, so the bot refuses to start rather than
    # silently running with no way to authenticate to Telegram's API. Create
    # a bot via @BotFather to get a real token; this is the one surface in
    # this app that necessarily reaches the public internet, unlike the rest
    # of the stack (fully local via docker-compose).
    telegram_bot_token: str = ""


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
APPDATA_DATABASE_URL = settings.appdata_database_url
REDIS_URL = settings.redis_url
SEMANTIC_CACHE_SIMILARITY_THRESHOLD = settings.semantic_cache_similarity_threshold
SEMANTIC_CACHE_TTL_SECONDS = settings.semantic_cache_ttl_seconds
MEMORY_RETENTION_DAYS = settings.memory_retention_days
MAX_COST_USD_PER_TURN = settings.max_cost_usd_per_turn
TELEGRAM_BOT_TOKEN = settings.telegram_bot_token
MINIO_ENDPOINT = settings.minio_endpoint
MINIO_ACCESS_KEY = settings.minio_access_key
MINIO_SECRET_KEY = settings.minio_secret_key
MINIO_BUCKET = settings.minio_bucket
MINIO_SECURE = settings.minio_secure

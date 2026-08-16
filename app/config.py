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

    # Langfuse
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # Durable checkpointer (app/agent.py) — survives a process restart,
    # unlike the in-memory MemorySaver build_graph() defaults to for tests.
    # A bare relative filename by design: no parent directory to create,
    # no docker-compose service required (see GRAPH_PATTERNS.md's durable
    # checkpointer note for why this isn't Postgres, which the stack
    # already runs for LiteLLM but which would make the test suite depend
    # on `make up` being up).
    checkpoint_db_path: str = "checkpoints.sqlite3"


settings = Settings()

# Backward-compatible module-level constants.
OPENAI_API_BASE = settings.openai_api_base
OPENAI_API_KEY = settings.openai_api_key
CHAT_MODEL = settings.chat_model
EMBED_MODEL = settings.embed_model
QDRANT_URL = settings.qdrant_url
COLLECTION = settings.collection
LANGFUSE_HOST = settings.langfuse_host
LANGFUSE_PUBLIC_KEY = settings.langfuse_public_key
LANGFUSE_SECRET_KEY = settings.langfuse_secret_key
CHECKPOINT_DB_PATH = settings.checkpoint_db_path

"""Shared embedding client pointed at the LiteLLM proxy."""
from langchain_openai import OpenAIEmbeddings

from app.config import EMBED_MODEL, OPENAI_API_BASE, OPENAI_API_KEY

embeddings = OpenAIEmbeddings(
    model=EMBED_MODEL,
    base_url=OPENAI_API_BASE,
    api_key=OPENAI_API_KEY,
    check_embedding_ctx_length=False,  # let the proxy/Ollama handle chunking
)


def embed_text(text: str) -> list[float]:
    return embeddings.embed_query(text)

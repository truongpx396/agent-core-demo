"""Shared settings, loaded from .env.

Every module reads endpoints and model names from here so the wiring between
the app and the Docker services lives in exactly one place.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# LLM proxy (OpenAI-compatible LiteLLM endpoint)
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "http://localhost:4000/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "sk-anything")
CHAT_MODEL = os.getenv("CHAT_MODEL", "chat")
EMBED_MODEL = os.getenv("EMBED_MODEL", "embed")

# Qdrant
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION = os.getenv("COLLECTION", "docs")

# Langfuse
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "http://localhost:3000")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")

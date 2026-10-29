"""What a vector collection's vectors were made with, and a refusal to mix two answers (specs/014
FR-018..FR-020).

The problem: two embedding models can share a dimension and be entirely different spaces (nomic-embed-text and
gte-modernbert-base are both 768-d), so Qdrant accepts one model's query against the other's vectors and every
search silently returns confident nonsense. A prefix change does the same with the model unchanged (nomic's
`search_query: ` / `search_document: `). A size check catches neither.

The identity is (the model the `embed` alias resolves to, vector size, query prefix, document prefix, an
operator revision label). Its first 8 hex digits go in the collection's NAME (`docs-1a2b3c4d`) and the answer
cache's index name, so any process can read what a collection holds with no second store, and `qdrant_store`
compares it with the configured identity on every read and write.

"Resolved model" comes from LiteLLM's /model/info (app/core/model_resolver.py). Where that answers nothing
(OPENAI_API_BASE straight at an OpenAI-compatible server, as in the live test tier, or LiteLLM briefly down)
the identity falls back to the ALIAS NAME. That is weaker (re-pointing the alias at another model of the same
size goes unnoticed, which `EMBED_MODEL_REVISION` is for) but stable per deployment, and counted as
`unverified`; refusing would break such deployments. The resolver caches a success for the process life, so a
flapping proxy doesn't flip the hash.
"""
import hashlib
import json
from dataclasses import dataclass

from app.core.config import (
    EMBED_DOCUMENT_PREFIX,
    EMBED_MODEL,
    EMBED_MODEL_REVISION,
    EMBED_QUERY_PREFIX,
)
from app.core.errors import ErrorCode
from app.ports import models as ports
from app.retrieval import embeddings

HASH_LENGTH = 8


@dataclass(frozen=True)
class EmbeddingIdentity:
    alias: str
    resolved_model: str | None  # None = the alias could not be resolved; the hash then uses `alias:<name>` and is counted unverified
    dim: int
    query_prefix: str
    document_prefix: str
    revision_label: str

    @property
    def verified(self) -> bool:
        return self.resolved_model is not None

    def hash8(self) -> str:
        """Stable across processes and restarts. The alias NAME is not part of it when the model resolved (renaming an alias to the same
        model must not force a re-embed); it stands in for the model only when nothing resolved."""
        body = {
            "model": self.resolved_model if self.resolved_model is not None else f"alias:{self.alias}",
            "dim": self.dim,
            "query_prefix": self.query_prefix,
            "document_prefix": self.document_prefix,
            "revision": self.revision_label,
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:HASH_LENGTH]

    def describe(self) -> dict:
        """For logs and the readiness warning: no secret in any field, and never the text of a document."""
        return {
            "alias": self.alias,
            "model": self.resolved_model or f"alias:{self.alias} (unresolved)",
            "dim": self.dim,
            "query_prefix": self.query_prefix,
            "document_prefix": self.document_prefix,
            "revision": self.revision_label,
            "hash": self.hash8(),
        }


class EmbeddingIdentityMismatch(Exception):
    """A collection holds vectors from a different embedding identity than this process is configured to use.

    Reads degrade like a retrieval outage (the three callers that already absorb a retrieval failure are unchanged); writes fail visibly.
    The message names the way out because the person reading it is an operator in the middle of an incident."""

    code = ErrorCode.EMBEDDING_IDENTITY_MISMATCH

    def __init__(self, collection: str, expected: str, found: str, *, write: bool) -> None:
        self.collection, self.expected, self.found, self.write = collection, expected, found, write
        super().__init__(
            f"collection {collection!r} holds vectors made under embedding identity {found} but this process is configured for "
            f"{expected}; mixing them would return wrong-space results. Rebuild it under the configured identity with "
            f"`make reembed COLLECTION_NAME={collection}` (or restore the previous embedding settings)."
        )


async def current_identity() -> EmbeddingIdentity:
    """The identity this process is configured for. Cheap after the first call: the resolver caches a success for the process life and
    the dimension is probed once."""
    return EmbeddingIdentity(
        alias=EMBED_MODEL,
        resolved_model=await ports.model_catalog.get().resolve(EMBED_MODEL),
        dim=await embeddings.embedding_dim(),
        query_prefix=EMBED_QUERY_PREFIX,
        document_prefix=EMBED_DOCUMENT_PREFIX,
        revision_label=EMBED_MODEL_REVISION,
    )

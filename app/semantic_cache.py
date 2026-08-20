"""Tenant+principal-scoped semantic cache backed by Redis Stack (RediSearch's
vector KNN over JSON documents) — not plain Redis, which has no vector
search of its own.

A cache entry is keyed by *meaning*, not exact text: `get` embeds the
incoming query with the same dense embedding used for Qdrant retrieval
(app/embeddings.py::embed_text — one embedding space, one model, no drift
between what's indexed for documents and what's indexed for cache lookups)
and does a cosine-KNN search restricted to this ctx's tenant+principal, same
pre-filter discipline as app/security.py's Policy.lower() and
app/sql_store.py's mandatory `WHERE tenant = %s` — a cache is still a store
that can leak across tenants if the isolation is bolted on as a Python
post-filter instead of a server-side predicate.

Graceful degradation, always: Redis unreachable, the index missing, or an
embedding call failing all fall through to a cache miss (`get` returns
None) or a swallowed no-op (`set` returns without writing) — a semantic
cache is a latency optimization, never a correctness dependency, so it must
never be able to fail a turn the way retrieve_context's own degradation
guarantees for retrieval (see app/graph.py). Every outcome (hit/miss/error)
is recorded via agent_semantic_cache_total (app/metrics.py) so a degraded
cache is visible, not silently smoothed over.
"""
import json
import logging
import re
import uuid

import numpy as np
import redis
from redis.commands.search.field import TagField, TextField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query

from app.config import (
    REDIS_URL,
    SEMANTIC_CACHE_SIMILARITY_THRESHOLD,
    SEMANTIC_CACHE_TTL_SECONDS,
)
from app.embeddings import embed_text
from app.security import SecurityCtx
from app import metrics

logger = logging.getLogger(__name__)

INDEX_NAME = "idx:semantic_cache"
KEY_PREFIX = "cache:"
# COSINE distance (RediSearch) is 1 - cosine_similarity: 0 for identical
# vectors, up to 2 for opposite ones — verified empirically against a real
# Redis Stack instance before writing this module. A cached entry only
# counts as a hit within this much distance of the incoming query.
_MAX_DISTANCE = 1 - SEMANTIC_CACHE_SIMILARITY_THRESHOLD

_client: redis.Redis | None = None
_index_ready = False

# RediSearch TAG queries treat these characters as syntax, not literal text
# (verified empirically: an unescaped tenant like "other-co" raises "Syntax
# error... near other") — every tag value interpolated into a query string
# must be escaped first, the same reason app/sql_store.py never interpolates
# a caller-supplied value into SQL text unparameterized.
_TAG_ESCAPE_RE = re.compile(r"([,.<>{}\[\]\"':;!@#$%^&*()\-+=~ ])")


def _escape_tag(value: str) -> str:
    return _TAG_ESCAPE_RE.sub(r"\\\1", value)


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(REDIS_URL)
    return _client


def _ensure_index(client: redis.Redis) -> None:
    """Idempotent: creates the index on first use, in whichever process
    reaches it first. Dimension is derived from a real embed_text() call,
    never hardcoded — the same approach app/ingest.py uses for Qdrant's
    collection (`ensure_collection(dim=len(vectors[0]))`), so this module
    never drifts out of sync with whatever embedding model is actually
    configured."""
    global _index_ready
    if _index_ready:
        return
    dim = len(embed_text("dimension probe"))
    schema = (
        TagField("$.tenant", as_name="tenant"),
        TagField("$.principal", as_name="principal"),
        TextField("$.query", as_name="query"),
        # Declared (not just stored) so return_fields can project them back
        # on a hit — RediSearch's JSON index only returns fields that are
        # part of the schema, verified empirically (an undeclared field
        # silently comes back missing, not as an error, which is worse).
        TextField("$.answer", as_name="answer", no_stem=True),
        TextField("$.citations", as_name="citations", no_stem=True),
        VectorField(
            "$.embedding",
            "FLAT",
            {"TYPE": "FLOAT32", "DIM": dim, "DISTANCE_METRIC": "COSINE"},
            as_name="embedding",
        ),
    )
    try:
        client.ft(INDEX_NAME).create_index(
            schema, definition=IndexDefinition(prefix=[KEY_PREFIX], index_type=IndexType.JSON)
        )
    except redis.exceptions.ResponseError as exc:
        if "already exists" not in str(exc).lower():
            raise
    _index_ready = True


def get(ctx: SecurityCtx | None, query: str) -> tuple[str, list[dict]] | None:
    """Look up a near-identical past query, scoped to ctx's tenant AND
    principal (never just tenant — a cached answer can carry citations to
    that principal's own memories, so this is at least as narrow as
    Policy.lower(ctx, "memories"), not merely "documents"). Returns
    (answer, citations) on a hit, None on a miss OR any failure — a
    degraded cache is a miss, never an exception the caller has to guard
    against.
    """
    if not ctx or not ctx.get("tenant") or not ctx.get("principal"):
        return None
    try:
        client = _get_client()
        _ensure_index(client)
        vector = np.array(embed_text(query), dtype=np.float32).tobytes()
        q = (
            Query(
                f"(@tenant:{{{_escape_tag(ctx['tenant'])}}} "
                f"@principal:{{{_escape_tag(ctx['principal'])}}})"
                "=>[KNN 1 @embedding $vec AS dist]"
            )
            .sort_by("dist")
            .return_fields("answer", "citations", "dist")
            .dialect(2)
        )
        result = client.ft(INDEX_NAME).search(q, query_params={"vec": vector})
        if not result.docs or float(result.docs[0].dist) > _MAX_DISTANCE:
            metrics.agent_semantic_cache_total.labels(outcome="miss").inc()
            return None
        doc = result.docs[0]
        metrics.agent_semantic_cache_total.labels(outcome="hit").inc()
        return doc.answer, json.loads(doc.citations)
    except Exception:  # noqa: BLE001 - a cache is a latency optimization, never a correctness dependency
        logger.warning("semantic cache lookup failed; treating as a miss", exc_info=True)
        metrics.agent_semantic_cache_total.labels(outcome="error").inc()
        return None


def set(ctx: SecurityCtx | None, query: str, answer: str, citations: list[dict]) -> None:
    """Best-effort write-through after a turn completes (app/graph.py's
    write_semantic_cache node, only on the miss path — see its docstring
    for why a hit never re-writes). Swallows every failure: a cache write
    that fails must not fail the turn whose answer it's trying to cache."""
    if not ctx or not ctx.get("tenant") or not ctx.get("principal"):
        return
    try:
        client = _get_client()
        _ensure_index(client)
        vector = embed_text(query)
        key = f"{KEY_PREFIX}{uuid.uuid4().hex}"
        client.json().set(
            key,
            "$",
            {
                "tenant": ctx["tenant"],
                "principal": ctx["principal"],
                "query": query,
                "answer": answer,
                "citations": json.dumps(citations),
                "embedding": vector,
            },
        )
        client.expire(key, SEMANTIC_CACHE_TTL_SECONDS)
    except Exception:  # noqa: BLE001 - see get()'s matching note
        logger.warning("semantic cache write failed; continuing without caching", exc_info=True)
        metrics.agent_semantic_cache_total.labels(outcome="error").inc()

"""SecurityCtx + Policy: this app's tenant/principal isolation axis (see
GRAPH_PATTERNS.md's "Multi-Tenant Isolation" pattern).

Scope: this is *authorization/isolation* — enforced as a store-level
pre-filter — not *authentication*. Nothing here verifies a password, JWT,
or session; `app/api/main.py` reads tenant/principal from trusted headers
as the seam a real auth middleware replaces later, so that swap is a
gateway config change, not a rewrite.

Deliberately narrower than full RBAC: no `agent_role`/`allowed_tools` (no
such feature exists yet), no `trace_id` (covered by graph.py's `run_id`).
`claims` is an opaque escape hatch, carried through but never branched on
here, for a future policy to use.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, TypedDict, TypeGuard

from qdrant_client.models import (
    Condition,
    DatetimeRange,
    FieldCondition,
    Filter,
    MatchValue,
)

from app.core.config import MEMORY_RETENTION_DAYS


class SecurityCtx(TypedDict):
    """Stamped ONCE at the trusted boundary (app/api/main.py's header
    extraction, or a local entry point like app/channels/chat.py) — never by
    a graph node, never derived from message content or tool output. A ctx a
    caller could influence via the request body or conversation would be a
    privilege-escalation primitive, not a parameter."""

    tenant: str
    principal: str
    claims: dict  # opaque; no code in this app reads a key out of it (yet)


class Policy(Protocol):
    """The access model. PURE — no I/O, no clock, no randomness — so it's
    exhaustively testable and can't fail open on a network blip (a Policy
    that calls out to a DB to decide could itself become the outage that
    accidentally allows or denies everything)."""

    def permit(self, action: str, ctx: SecurityCtx) -> bool:
        """May this action happen at all, for this ctx? Unknown actions and
        malformed ctx (missing/empty tenant or principal) MUST return
        False — fail closed, not fail open."""
        ...

    def lower(self, ctx: SecurityCtx, target: str) -> Filter:
        """Lower ctx into a store-native predicate scoped to `target`
        ("documents" | "memories", sharing one Qdrant collection). MUST be
        applied INSIDE the store as a pre-filter (app/retrieval/qdrant_store.py)
        — post-filtering in Python is a correctness bug even if the output
        looks identical, since the store already returned rows this
        principal may not see."""
        ...


_KNOWN_ACTIONS = frozenset(
    {"search", "write_note", "recall_memory", "write_memory", "query_structured_data"}
)
_TARGETS = frozenset({"documents", "memories"})


class TenantIsolationPolicy:
    """The one Policy this app ships: every principal belongs to exactly one
    tenant, invisible to every other tenant. Within a tenant,
    `target="documents"` stays tenant-wide, but `target="memories"`
    additionally scopes to `ctx["principal"]` — a memory belongs to whoever
    wrote it, a second isolation axis nested inside the first."""

    def permit(self, action: str, ctx: SecurityCtx) -> bool:
        if action not in _KNOWN_ACTIONS:
            return False
        # Missing `ctx` must deny, not raise — a Policy that crashes on bad
        # input isn't "failing closed," it hands the caller an exception to
        # accidentally mishandle into failing open.
        return valid_ctx(ctx)

    def lower(self, ctx: SecurityCtx, target: str) -> Filter:
        if target not in _TARGETS:
            raise ValueError(f"unknown lowering target: {target!r}")
        must: list[Condition] = [
            FieldCondition(key="tenant", match=MatchValue(value=ctx["tenant"])),
            FieldCondition(
                key="kind",
                match=MatchValue(value="document" if target == "documents" else "memory"),
            ),
        ]
        if target == "memories":
            must.append(
                FieldCondition(key="owner", match=MatchValue(value=ctx["principal"]))
            )
            # Retention enforced AT RECALL TIME, not just by a background
            # sweep (memory.py::delete_memories, pattern 33) — a memory past
            # MEMORY_RETENTION_DAYS is invisible on the next read even if
            # unswept. Note: Qdrant treats a missing field as never matching
            # a range filter, so a memory with no `created_at` (written
            # before this field existed) is also excluded — invisible until
            # re-written, not just a future-only guarantee.
            cutoff = datetime.now(UTC) - timedelta(days=MEMORY_RETENTION_DAYS)
            must.append(
                FieldCondition(key="created_at", range=DatetimeRange(gte=cutoff))
            )
        return Filter(must=must)


DEFAULT_POLICY: Policy = TenantIsolationPolicy()


def valid_ctx(ctx: SecurityCtx | None) -> TypeGuard[SecurityCtx]:
    """True if `ctx` is present with a non-empty tenant and principal — the
    one check every fail-closed call site needs first. Not a Policy method:
    a structural presence check independent of which Policy is wired in.

    Typed as `TypeGuard` (not plain `bool`) so every `if valid_ctx(ctx):`
    guard also narrows `ctx` from `SecurityCtx | None` to `SecurityCtx` for
    the type checker.
    """
    if ctx is None:
        return False
    return bool(ctx.get("tenant")) and bool(ctx.get("principal"))

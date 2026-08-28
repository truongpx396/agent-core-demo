"""SecurityCtx + Policy: the tenant/principal isolation axis this app didn't
have until now — see GRAPH_PATTERNS.md's "Multi-Tenant Isolation" pattern
for the full write-up and the hard-won lessons this is modeled on.

Scope, stated honestly: this is the *authorization/isolation* structure —
who may see what, enforced as a store-level pre-filter — not
*authentication*. Nothing here verifies a password, a JWT, or a session;
`app/api/main.py` reads tenant/principal from trusted headers as the seam a real
auth middleware replaces later. Building that seam correctly (never a
client-supplied body field, never a silent default identity) is what makes
it a drop-in replacement rather than a rewrite.

Deliberately narrower than a full RBAC model: no `agent_role` or
per-role `allowed_tools` (this app has no role-based tool restriction
feature to hang that off), no `trace_id` (already covered by graph.py's
`run_id`, a different concern). `claims` exists as the same opaque,
domain-specific escape hatch as the reference design — carried through but
never branched on here — so a future policy (per-role, per-project, ...)
has somewhere to add its own facts without changing this module's shape.
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
    """Stamped ONCE, at the trusted boundary (app/api/main.py's header extraction,
    or a local dev entry point like app/channels/chat.py) — never by a graph node,
    never derived from message content or tool output. A ctx a caller could
    influence through the request body or the conversation itself would be
    a privilege-escalation primitive, not a parameter; see
    app/api/main.py::_extract_ctx for where the line actually gets drawn.
    """

    tenant: str
    principal: str
    claims: dict  # opaque; no code in this app reads a key out of it (yet)


class Policy(Protocol):
    """The access model. PURE — no I/O, no clock, no randomness, so it's
    exhaustively testable and can't fail open on a network blip (a Policy
    that calls out to a database to decide "may this happen" can itself be
    the outage that decides everyone's requests get allowed, or denied, by
    accident)."""

    def permit(self, action: str, ctx: SecurityCtx) -> bool:
        """May this action happen at all, for this ctx? Unknown actions and
        malformed ctx (missing/empty tenant or principal) MUST return
        False — fail closed, not fail open."""
        ...

    def lower(self, ctx: SecurityCtx, target: str) -> Filter:
        """Lower ctx into a store-native predicate scoped to `target`
        ("documents" | "memories" — the two kinds sharing app/agent/tools.py's
        Qdrant collection). MUST be applied INSIDE the store as a
        pre-filter (see app/retrieval/qdrant_store.py) — post-filtering in Python is
        a correctness bug even when the output looks identical, because it
        means the store already returned rows this principal may not see."""
        ...


_KNOWN_ACTIONS = frozenset(
    {"search", "write_note", "recall_memory", "write_memory", "query_structured_data"}
)
_TARGETS = frozenset({"documents", "memories"})


class TenantIsolationPolicy:
    """The one Policy this app ships: every principal belongs to exactly one
    tenant, and a tenant's data is invisible to every other tenant. Within
    a tenant, `target="documents"` stays tenant-wide (any principal in the
    tenant can search the shared knowledge base) — but `target="memories"`
    additionally scopes to `ctx["principal"]`, because a memory belongs to
    whoever wrote it, not to the tenant at large. That's a second, finer
    isolation axis nested inside the first, not a special case bolted on.
    """

    def permit(self, action: str, ctx: SecurityCtx) -> bool:
        if action not in _KNOWN_ACTIONS:
            return False
        # `ctx` itself missing (not just an empty/malformed dict) must
        # deny, not raise — a Policy that crashes on bad input is not
        # "failing closed," it's handing the caller an exception to
        # accidentally mishandle into failing open instead.
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
            # Retention horizon enforced AT RECALL TIME, not merely by a
            # background sweep (app/agent/memory.py::delete_memories) — a memory
            # past MEMORY_RETENTION_DAYS is invisible on the very next
            # read even if nothing has swept it away yet (GRAPH_PATTERNS.md
            # pattern 33). Verified empirically: a memory with no
            # `created_at` at all (written before this field existed) is
            # ALSO excluded by this `gte` range condition — Qdrant treats
            # a missing field as never matching a range filter — so this
            # is a real migration note, not just a future-only guarantee:
            # any pre-existing memory written before this feature landed
            # becomes invisible at recall until re-written.
            cutoff = datetime.now(UTC) - timedelta(days=MEMORY_RETENTION_DAYS)
            must.append(
                FieldCondition(key="created_at", range=DatetimeRange(gte=cutoff))
            )
        return Filter(must=must)


DEFAULT_POLICY: Policy = TenantIsolationPolicy()


def valid_ctx(ctx: SecurityCtx | None) -> TypeGuard[SecurityCtx]:
    """True if `ctx` is present and has a non-empty tenant and principal —
    the one check every fail-closed call site (validate_input, each tool)
    needs before doing anything else. Deliberately not a Policy method:
    this is a structural presence check on `ctx` itself, independent of
    which Policy ends up wired in, so it stays correct even for a future
    Policy whose `permit` rules are more elaborate than tenant equality.

    Typed as a `TypeGuard` (not a plain `bool`) so every `if valid_ctx(ctx):`
    /`if not valid_ctx(ctx): return ...` guard clause already used
    throughout this codebase also narrows `ctx` from `SecurityCtx | None`
    to `SecurityCtx` for the type checker — matching what was already true
    at runtime, not a behavior change.
    """
    if ctx is None:
        return False
    return bool(ctx.get("tenant")) and bool(ctx.get("principal"))

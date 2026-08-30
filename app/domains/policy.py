"""A small, reusable `Policy` (app/core/security.py's Protocol) shared by
every example domain under `app/domains/` (support, ops, sales).

`app/agent/manifest.py`'s own docstring is explicit that a `DomainPlugin`'s
`policy()` is never called by `build_graph()` itself — enforcement happens
INSIDE each domain's own tool implementations, the same way
`app/agent/tools.py`'s `_ctx_or_refuse` calls `DEFAULT_POLICY.permit(...)`
directly. These new domains follow that exact discipline, just against a
different, domain-specific action vocabulary (`create_ticket`,
`schedule_followup`, ...) instead of Acme's `{"search", "write_note", ...}`
— `app.core.security.TenantIsolationPolicy.permit` would fail closed on
every one of these action names (its `_KNOWN_ACTIONS` allowlist has never
heard of them), which is correct for THAT policy but means each new domain
needs its own.

Rather than hand-write three near-identical Policy classes, this one takes
its allowed action set as data. None of these domains hold any Qdrant-
scoped data of their own (tickets/leads/followups live in Postgres, always
queried with an explicit `tenant = %s`, mirroring `app/agent/sql_store.py`
— see each domain's own `store.py`), so `lower()` has nothing to lower
into and raises rather than fabricating a Filter nothing ever reads —
same "honestly implement the Protocol, even the unused half" posture
`tests/agent/test_manifest.py::_AllowAllPolicy` already takes, just not
pretending "permit anything" the way that test-only policy does: this one
still fails closed on a missing/malformed ctx via `valid_ctx`.
"""
from __future__ import annotations

from dataclasses import dataclass

from qdrant_client.models import Filter

from app.core.security import SecurityCtx, valid_ctx


@dataclass(frozen=True)
class ActionAllowlistPolicy:
    actions: frozenset[str]

    def permit(self, action: str, ctx: SecurityCtx) -> bool:
        return action in self.actions and valid_ctx(ctx)

    def lower(self, ctx: SecurityCtx, target: str) -> Filter:
        raise NotImplementedError(
            "no Qdrant-scoped data in this domain — its tools query Postgres "
            "directly with an explicit tenant predicate (see its store.py), "
            "so nothing ever calls Policy.lower() for it."
        )

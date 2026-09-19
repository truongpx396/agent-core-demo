"""Small, reusable `Policy` (app/core/security.py's Protocol) shared by the
support/ops/sales domains.

`DomainPlugin.policy()` is never called by `build_graph()` itself —
enforcement happens inside each domain's own tools, same as
`app/agent/tools.py`'s `_ctx_or_refuse` calling `DEFAULT_POLICY.permit(...)`
directly, just against a domain-specific action vocabulary (`create_ticket`,
`schedule_followup`, ...) that `TenantIsolationPolicy` doesn't know.

Takes its allowed action set as data instead of one class per domain. None
of these domains hold Qdrant-scoped data (tickets/leads/followups live in
Postgres, always queried with an explicit `tenant = %s` — see each domain's
`store.py`), so `lower()` raises rather than fabricating an unused Filter;
`permit()` still fails closed on a missing/malformed ctx via `valid_ctx`.
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

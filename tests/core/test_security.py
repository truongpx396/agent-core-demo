"""Tests for app/core/security.py: SecurityCtx validity, and TenantIsolationPolicy
— the one Policy this app ships.

app/agent/tools/builtin.py's tests already prove the Policy is wired correctly into
search_docs/add_note/remember; these test the Policy itself in isolation,
the way GRAPH_PATTERNS.md's "Multi-Tenant Isolation" pattern describes:
pure, fail-closed, and exhaustively testable without any I/O.
"""
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import MEMORY_RETENTION_DAYS
from app.core.security import (
    DEFAULT_POLICY,
    AccessScope,
    TenantIsolationPolicy,
    valid_ctx,
)

FULL_CTX = {"tenant": "ecorp", "principal": "u1", "claims": {}}


class TestValidCtx:
    def test_none_is_invalid(self):
        assert valid_ctx(None) is False

    def test_empty_dict_is_invalid(self):
        assert valid_ctx({}) is False

    def test_missing_tenant_is_invalid(self):
        assert valid_ctx({"principal": "u1"}) is False

    def test_missing_principal_is_invalid(self):
        assert valid_ctx({"tenant": "ecorp"}) is False

    def test_empty_string_tenant_is_invalid(self):
        assert valid_ctx({"tenant": "", "principal": "u1"}) is False

    def test_empty_string_principal_is_invalid(self):
        assert valid_ctx({"tenant": "ecorp", "principal": ""}) is False

    def test_full_ctx_is_valid(self):
        assert valid_ctx(FULL_CTX) is True


class TestTenantIsolationPolicyPermit:
    def test_purity_same_input_same_output(self):
        """No I/O, no clock, no randomness — calling it 1000 times with the
        same input must never disagree with itself (see Policy's docstring
        for why this matters: a Policy that can fail open on a network
        blip is the exact failure mode this property rules out)."""
        policy = TenantIsolationPolicy()
        results = {policy.permit("search", FULL_CTX) for _ in range(1000)}
        assert results == {True}

    def test_known_actions_permitted_with_valid_ctx(self):
        policy = TenantIsolationPolicy()
        for action in ("search", "write_note", "recall_memory", "write_memory"):
            assert policy.permit(action, FULL_CTX) is True

    def test_unknown_action_denied_even_with_valid_ctx(self):
        """Fail closed: an unrecognized action is not an unconstrained
        one — see Policy.permit's docstring."""
        policy = TenantIsolationPolicy()
        assert policy.permit("delete_everything", FULL_CTX) is False

    def test_known_action_denied_with_missing_ctx(self):
        policy = TenantIsolationPolicy()
        assert policy.permit("search", None) is False
        assert policy.permit("search", {}) is False

    def test_known_action_denied_with_empty_tenant_or_principal(self):
        policy = TenantIsolationPolicy()
        assert policy.permit("search", {"tenant": "", "principal": "u1"}) is False
        assert policy.permit("search", {"tenant": "ecorp", "principal": ""}) is False


class TestTenantIsolationPolicyLower:
    """`lower` gives a store-neutral `AccessScope`; that the Qdrant adapter turns it into the right pre-filter is
    tests/retrieval/test_qdrant_store.py::TestScopeFilter."""

    def test_documents_target_scopes_to_tenant_and_kind_document(self):
        scope = TenantIsolationPolicy().lower(FULL_CTX, "documents")
        assert scope == AccessScope(tenant="ecorp", kind="document")

    def test_memories_target_additionally_scopes_to_owner(self):
        """The second, finer isolation axis nested inside the first — see
        TenantIsolationPolicy's docstring."""
        scope = TenantIsolationPolicy().lower(FULL_CTX, "memories")
        assert (scope.tenant, scope.kind, scope.owner) == ("ecorp", "memory", "u1")

    def test_memories_target_also_includes_a_retention_horizon(self):
        """The retention-at-recall condition (GRAPH_PATTERNS.md pattern 33): a memory older than MEMORY_RETENTION_DAYS is invisible."""
        scope = TenantIsolationPolicy().lower(FULL_CTX, "memories")
        assert scope.created_after is not None
        expected = datetime.now(UTC) - timedelta(days=MEMORY_RETENTION_DAYS)
        assert abs((scope.created_after - expected).total_seconds()) < 5

    def test_documents_target_has_no_owner_and_no_retention(self):
        """Retention and ownership are memory concerns — pattern 33's cutoff must never leak onto the documents lowering."""
        scope = TenantIsolationPolicy().lower(FULL_CTX, "documents")
        assert scope.owner is None and scope.created_after is None

    def test_documents_and_memories_scopes_never_overlap(self):
        """A document can never satisfy a memory scope or vice versa — the `kind` discriminator is what keeps
        app/agent/tools/builtin.py's shared collection from leaking one kind into the other's results."""
        policy = TenantIsolationPolicy()
        assert policy.lower(FULL_CTX, "documents").kind != policy.lower(FULL_CTX, "memories").kind

    def test_unknown_target_raises(self):
        with pytest.raises(ValueError):
            TenantIsolationPolicy().lower(FULL_CTX, "something_else")

    def test_different_tenants_produce_different_scopes(self):
        policy = TenantIsolationPolicy()
        s1 = policy.lower({"tenant": "ecorp", "principal": "u1"}, "documents")
        s2 = policy.lower({"tenant": "other-co", "principal": "u1"}, "documents")
        assert s1.tenant != s2.tenant

    def test_a_scope_cannot_be_edited_after_it_is_made(self):
        scope = TenantIsolationPolicy().lower(FULL_CTX, "documents")
        with pytest.raises(AttributeError):
            scope.tenant = "someone-else"  # type: ignore[misc] - the point of the test


class TestDefaultPolicy:
    def test_default_policy_is_a_tenant_isolation_policy(self):
        assert isinstance(DEFAULT_POLICY, TenantIsolationPolicy)

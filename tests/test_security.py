"""Tests for app/security.py: SecurityCtx validity, and TenantIsolationPolicy
— the one Policy this app ships.

app/tools.py's tests already prove the Policy is wired correctly into
search_docs/add_note/remember; these test the Policy itself in isolation,
the way GRAPH_PATTERNS.md's "Multi-Tenant Isolation" pattern describes:
pure, fail-closed, and exhaustively testable without any I/O.
"""
from app.security import DEFAULT_POLICY, TenantIsolationPolicy, valid_ctx

FULL_CTX = {"tenant": "acme", "principal": "u1", "claims": {}}


class TestValidCtx:
    def test_none_is_invalid(self):
        assert valid_ctx(None) is False

    def test_empty_dict_is_invalid(self):
        assert valid_ctx({}) is False

    def test_missing_tenant_is_invalid(self):
        assert valid_ctx({"principal": "u1"}) is False

    def test_missing_principal_is_invalid(self):
        assert valid_ctx({"tenant": "acme"}) is False

    def test_empty_string_tenant_is_invalid(self):
        assert valid_ctx({"tenant": "", "principal": "u1"}) is False

    def test_empty_string_principal_is_invalid(self):
        assert valid_ctx({"tenant": "acme", "principal": ""}) is False

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
        assert policy.permit("search", {"tenant": "acme", "principal": ""}) is False


class TestTenantIsolationPolicyLower:
    def test_documents_target_scopes_to_tenant_and_kind_document(self):
        policy = TenantIsolationPolicy()
        f = policy.lower(FULL_CTX, "documents")
        values = {c.key: c.match.value for c in f.must}
        assert values == {"tenant": "acme", "kind": "document"}

    def test_memories_target_additionally_scopes_to_owner(self):
        """The second, finer isolation axis nested inside the first — see
        TenantIsolationPolicy's docstring."""
        policy = TenantIsolationPolicy()
        f = policy.lower(FULL_CTX, "memories")
        values = {c.key: c.match.value for c in f.must if c.match is not None}
        assert values == {"tenant": "acme", "kind": "memory", "owner": "u1"}

    def test_memories_target_also_includes_a_retention_horizon_range(self):
        """The retention-at-recall condition (GRAPH_PATTERNS.md pattern
        33) — a separate condition from the match-based ones above, so
        it's asserted on its own rather than folded into the match-value
        dict comprehension."""
        policy = TenantIsolationPolicy()
        f = policy.lower(FULL_CTX, "memories")
        range_conditions = [c for c in f.must if c.key == "created_at"]
        assert len(range_conditions) == 1
        assert range_conditions[0].range.gte is not None

    def test_documents_target_has_no_retention_range(self):
        """Retention is a memory-specific concern — pattern 33's condition
        must never leak onto the documents lowering."""
        policy = TenantIsolationPolicy()
        f = policy.lower(FULL_CTX, "documents")
        assert not any(c.key == "created_at" for c in f.must)

    def test_documents_and_memories_filters_never_overlap(self):
        """A document can never satisfy a memory-scoped filter or vice
        versa — the `kind` discriminator is what keeps app/tools.py's
        shared Qdrant collection from leaking one kind into the other's
        results."""
        policy = TenantIsolationPolicy()
        doc_kind = next(
            c.match.value for c in policy.lower(FULL_CTX, "documents").must if c.key == "kind"
        )
        mem_kind = next(
            c.match.value for c in policy.lower(FULL_CTX, "memories").must if c.key == "kind"
        )
        assert doc_kind != mem_kind

    def test_unknown_target_raises(self):
        policy = TenantIsolationPolicy()
        try:
            policy.lower(FULL_CTX, "something_else")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for an unknown lowering target")

    def test_different_tenants_produce_different_filters(self):
        policy = TenantIsolationPolicy()
        f1 = policy.lower({"tenant": "acme", "principal": "u1"}, "documents")
        f2 = policy.lower({"tenant": "other-co", "principal": "u1"}, "documents")
        t1 = next(c.match.value for c in f1.must if c.key == "tenant")
        t2 = next(c.match.value for c in f2.must if c.key == "tenant")
        assert t1 != t2


class TestDefaultPolicy:
    def test_default_policy_is_a_tenant_isolation_policy(self):
        assert isinstance(DEFAULT_POLICY, TenantIsolationPolicy)

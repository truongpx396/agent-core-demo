"""Tests for app/domains/registry.py."""
import pytest

from app.agent.manifest import DEFAULT_MANIFEST
from app.domains.ops.domain import OPS_MANIFEST
from app.domains.registry import resolve_domain
from app.domains.sales.domain import SALES_MANIFEST
from app.domains.support.domain import SUPPORT_MANIFEST


@pytest.mark.parametrize(
    "name,expected_manifest",
    [
        ("acme", DEFAULT_MANIFEST),
        ("support", SUPPORT_MANIFEST),
        ("ops", OPS_MANIFEST),
        ("sales", SALES_MANIFEST),
    ],
)
def test_resolves_each_known_domain(name, expected_manifest):
    manifest, _domain = resolve_domain(name)
    assert manifest is expected_manifest


def test_unknown_domain_raises_with_the_valid_names_listed():
    with pytest.raises(ValueError, match="acme"):
        resolve_domain("not-a-real-domain")

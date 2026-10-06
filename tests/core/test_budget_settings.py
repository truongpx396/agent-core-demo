"""The spend-limit settings (app/core/config.py) reject nonsense instead of silently disabling a cap.

`_env_file=None` and a scrubbed environment: `Settings()` otherwise reads the developer's own
`.env`, and a test of the DEFAULTS must not depend on whether someone happens to have set a cap."""
import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.mark.parametrize(
    "field",
    [
        "max_cost_usd_per_tenant_per_month",
        "max_cost_usd_per_principal_per_day",
        "max_cost_usd_per_principal_per_month",
    ],
)
def test_a_negative_limit_is_rejected_not_read_as_off(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: -1.0})


@pytest.mark.parametrize(
    "field",
    [
        "max_cost_usd_per_tenant_per_month",
        "max_cost_usd_per_principal_per_day",
        "max_cost_usd_per_principal_per_month",
    ],
)
def test_the_optional_limits_are_off_by_default(field, monkeypatch):
    """0 means "no cap": a deployment that sets none of them behaves as it did before they existed."""
    monkeypatch.delenv(field.upper(), raising=False)

    assert getattr(Settings(_env_file=None), field) == 0.0

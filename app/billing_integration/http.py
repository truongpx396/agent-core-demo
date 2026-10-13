"""The billing core's webhook route, as the agent app mounts it (specs/011-billing-module-boundary, D19).

The route itself is the core's (`app/billing/http.py`); the app reaches it through here so that no file outside the two
billing packages imports `app.billing`, and so that mounting it imports `app.billing_integration`, which wires the core
to the app's connection pool before the first webhook arrives.
"""
from app.billing.http import configured_providers, router, validate_configuration

__all__ = ["configured_providers", "router", "validate_configuration"]

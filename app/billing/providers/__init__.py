"""Registered billing-provider adapters, by the `{provider}` path segment of the webhook URL.

Adding a provider is adding a module here and one line in `FACTORIES`; nothing else in `app/billing/`
changes (SC-004). An adapter that has not passed `tests/billing/contract.py` must not be registered.
"""
from collections.abc import Callable, Mapping

from app.billing.providers.base import BillingProvider, Capability
from app.billing.providers.fake import FakeProvider
from app.billing.providers.stripe import StripeProvider

FACTORIES: dict[str, Callable[[str], BillingProvider]] = {
    FakeProvider.name: FakeProvider,
    StripeProvider.name: StripeProvider,
}


class UnknownProvider(ValueError):
    """A configured provider name no adapter is registered under."""


def build(name: str, secret: str) -> BillingProvider:
    try:
        factory = FACTORIES[name]
    except KeyError:
        raise UnknownProvider(f"no billing adapter is registered as {name!r}; registered: {sorted(FACTORIES)}") from None
    return factory(secret)


def build_configured(names: tuple[str, ...], secrets: Mapping[str, str]) -> dict[str, BillingProvider]:
    """The adapters for the providers a deployment enables. Raises (so the process refuses to start) for a
    name no adapter is registered under or one with no secret: a webhook endpoint that cannot verify a
    signature must not exist."""
    built: dict[str, BillingProvider] = {}
    for name in names:
        secret = secrets.get(name, "")
        if not secret:
            raise UnknownProvider(f"billing provider {name!r} is enabled but BILLING_WEBHOOK_SECRETS has no secret for it")
        built[name] = build(name, secret)
    return built


def usage_export_providers(names: tuple[str, ...]) -> tuple[str, ...]:
    """Which of the enabled providers bill on usage (declare USAGE_EXPORT). Read from the adapter CLASS, so it needs no
    secret and no instance: the usage-event write asks it on the hot path, and "none" must cost nothing."""
    return tuple(
        name for name in names if name in FACTORIES and Capability.USAGE_EXPORT in getattr(FACTORIES[name], "capabilities", frozenset())
    )

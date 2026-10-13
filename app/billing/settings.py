"""The billing core's own settings (specs/011-billing-module-boundary, contracts/billing-core-boundary.md).

These ten fields used to live in `app/core/config.py::Settings`, which the billing code imported. That made
`app/billing/` unable to import anywhere the agent's central settings were not: a billing module that
needs the whole host to be importable is a folder of a larger program, not a module another project could
reuse. They are here now, with their validators and their comments, and the host's `Settings` INHERITS this
class, so every field, default, bound, validator, error message and environment variable is exactly what it
was and `Settings()` is still the one object the rest of the app reads.

Two instances therefore parse the same `.env`: the host's `Settings` (a superset) and this module's own
`settings` below. They cannot disagree, because the fields are defined once, here. The constants at the
bottom are computed exactly as `app/core/config.py` computed them, and `config.py` re-exports them, so a
host module that did `from app.core.config import BILLING_PROVIDERS` is untouched. A core module imports
the same names from here and keeps them as module-level names, which is what lets a test re-point one with
`monkeypatch.setattr(webhooks, "BILLING_REFUND_HOLD_HOURS", ...)`.

What stays in the host's `Settings`: the price of a credit and whether to gate on it (`credits_per_usd`,
`markup`, `credits_enforcement`, `credit_check_failure_policy`), and everything for the export and
reconciliation adapters. They are host policy about the agent's meter, and no validator here reads them.

This module imports nothing from `app` (enforced by tests/billing/test_billing_core_boundary.py).
"""
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The webhook inbox keeps an event for at least this long before the retention sweep may delete it: longer
# than any payment provider keeps redelivering one, so a sweep never races a retry (app/billing/inbox.py).
BILLING_INBOX_MIN_RETENTION_DAYS = 30


class BillingSettings(BaseSettings):
    # `hide_input_in_errors`: a ValidationError normally prints the offending raw value (`input_value='...'`). This class
    # holds API keys and webhook signing secrets, and a settings error is printed at import time, to a terminal or a
    # container log, so a key pasted into the wrong variable (or a malformed BILLING_WEBHOOK_SECRETS) would be echoed
    # whole. The error still names the field and the rule; it just no longer repeats the value.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    # Money in (specs/010 PR 4, app/billing/webhooks.py, POST /billing/webhooks/{provider}). A provider is
    # served only if it is NAMED here, and then it MUST have a signing secret: a webhook endpoint that cannot
    # verify a signature must not exist, so the process refuses to start rather than accept unauthenticated
    # payments. Empty (the default) means no provider and no webhook route to speak of: every request 404s.
    # Names are the adapter's `name` (app/billing/providers/__init__.py), e.g. "stripe". "fake" is the in-repo
    # development and test adapter; do not enable it in production.
    billing_providers: str = ""
    # {"provider": "signing secret"} as JSON. SecretStr so the values never show in a repr or a log line.
    billing_webhook_secrets: dict[str, SecretStr] = Field(default_factory=dict)
    # Credentials for the payment-provider adapters (specs/010 T029). The Stripe adapter reads `stripe_api_key` and the Polar
    # adapter `polar_access_token` / `polar_environment` (to create a Checkout); both are secret-wrapped (never in a repr or a
    # log line) and checked for the obvious mistake at startup. Use SANDBOX credentials. A webhook signing secret is not here: it
    # goes in `billing_webhook_secrets` above, per provider.
    # There is no "usage event name" setting on purpose: the billing model is prepaid packs (spec D16), so no adapter sends
    # usage to a provider and none needs the name of a Stripe Meter or a Polar event. If that ever changes it comes back
    # with the `USAGE_EXPORT` adapter that needs it, not before.
    #
    # Stripe: a server-side key from the sandbox's API keys page. Stripe recommends a restricted key (`rk_`) over a secret
    # key (`sk_`); a publishable key (`pk_`) cannot do anything on a server and is refused. A webhook-only deployment may leave
    # it unset: the adapter then verifies deliveries but refuses to create a checkout.
    stripe_api_key: SecretStr | None = None
    # Polar: an Organization Access Token. Sandbox and production are separate accounts with separate tokens.
    polar_access_token: SecretStr | None = None
    # Which Polar API the adapter talks to. "sandbox" is the default, so nothing reaches production unless it is asked to.
    polar_environment: Literal["sandbox", "production"] = "sandbox"
    # A body larger than this is refused (413) BEFORE it is read: an unauthenticated endpoint must not buffer
    # whatever it is sent. A real payment event is a few KB.
    billing_webhook_max_body_bytes: int = Field(default=256 * 1024, ge=1024, le=10 * 1024 * 1024)
    # Per source address, per minute, on the webhook route only. Generous on purpose: a provider redelivering a
    # backlog must not be throttled into a longer outage, and the signature check, not this, is the defence.
    billing_webhook_rate_limit_per_minute: int = Field(default=600, ge=1)
    # How many times one event may fail to apply before it is quarantined (and alerted) instead of retried forever.
    billing_webhook_max_attempts: int = Field(default=5, ge=1)
    # A refund can arrive before the purchase it reverses. It is held (the provider is told to retry) until the
    # purchase has been applied, but only this long, then quarantined: a wait on a counterparty needs a deadline.
    billing_refund_hold_hours: int = Field(default=24, ge=1)
    # How long scripts/billing_inbox_sweep.py keeps applied/ignored inbox rows. The floor is not a style choice
    # (see BILLING_INBOX_MIN_RETENTION_DAYS). The wallet's own idempotency keys, not these rows, are what stop a
    # late redelivery from granting twice, so the sweep is safe, but the rows are the audit trail of what was received.
    billing_inbox_retention_days: int = Field(default=400, ge=BILLING_INBOX_MIN_RETENTION_DAYS)

    @field_validator("billing_providers")
    @classmethod
    def _provider_names_are_path_safe(cls, value: str) -> str:
        for name in (part.strip() for part in value.split(",") if part.strip()):
            if not name.replace("_", "").replace("-", "").isalnum() or not name.islower():
                raise ValueError(f"BILLING_PROVIDERS entry {name!r} must be lowercase letters, digits, '-' or '_'")
        return value

    @field_validator("stripe_api_key", "polar_access_token", mode="before")
    @classmethod
    def _a_blank_credential_is_unset(cls, value: Any) -> Any:
        """`STRIPE_API_KEY=` left in `.env` with no value arrives as an empty string. That means "not set", not a key
        that is empty. (Only a blank is touched here: a real value is never inspected before it is secret-wrapped.)"""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("stripe_api_key")
    @classmethod
    def _stripe_key_is_a_server_side_key(cls, value: SecretStr | None) -> SecretStr | None:
        """The mistake worth catching at startup is pasting the publishable key (`pk_`), which is the first one on Stripe's
        API keys page and which cannot create anything on a server. The message never repeats the value."""
        if value is not None and not value.get_secret_value().startswith(("sk_", "rk_")):
            raise ValueError(
                "STRIPE_API_KEY must be a secret (sk_) or restricted (rk_) key; a publishable key (pk_) cannot be used on a server"
            )
        return value

    @model_validator(mode="after")
    def _every_enabled_provider_has_a_secret(self) -> "BillingSettings":
        for name in (part.strip() for part in self.billing_providers.split(",") if part.strip()):
            secret = self.billing_webhook_secrets.get(name)
            if secret is None or not secret.get_secret_value().strip():
                raise ValueError(
                    f"BILLING_PROVIDERS enables {name!r} but BILLING_WEBHOOK_SECRETS has no secret for it: "
                    "an endpoint that cannot verify a signature must not start"
                )
        return self


settings = BillingSettings()

# The constants consumers import. Computed exactly as app/core/config.py computed them before the move.
STRIPE_API_KEY = settings.stripe_api_key.get_secret_value() if settings.stripe_api_key else ""
POLAR_ACCESS_TOKEN = settings.polar_access_token.get_secret_value() if settings.polar_access_token else ""
POLAR_ENVIRONMENT = settings.polar_environment
BILLING_PROVIDERS = tuple(part.strip() for part in settings.billing_providers.split(",") if part.strip())
BILLING_WEBHOOK_SECRETS = {name: secret.get_secret_value() for name, secret in settings.billing_webhook_secrets.items()}
BILLING_WEBHOOK_MAX_BODY_BYTES = settings.billing_webhook_max_body_bytes
BILLING_WEBHOOK_RATE_LIMIT_PER_MINUTE = settings.billing_webhook_rate_limit_per_minute
BILLING_WEBHOOK_MAX_ATTEMPTS = settings.billing_webhook_max_attempts
BILLING_REFUND_HOLD_HOURS = settings.billing_refund_hold_hours
BILLING_INBOX_RETENTION_DAYS = settings.billing_inbox_retention_days

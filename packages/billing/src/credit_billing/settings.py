"""The billing core's own settings (specs/011-billing-module-boundary, contracts/billing-core-boundary.md).

These ten fields used to live in `app/core/config.py::Settings`, which the billing code imported. That left
`packages/billing/src/credit_billing/` unable to import anywhere the agent's central settings weren't: a
billing module needing the whole host importable is a folder of a larger program, not something another
project could reuse. They are here now with their validators and comments, and the host's `Settings` INHERITS
this class, so every field, default, bound, validator, error message and environment variable is what it was,
and `Settings()` is still the one object the rest of the app reads.

Two instances therefore parse the same `.env`: the host's `Settings` (a superset) and this module's own
`settings` below. They can't disagree, because the fields are defined once, here. The constants at the bottom
are computed exactly as `app/core/config.py` computed them, and `config.py` re-exports them, so a host module
doing `from app.core.config import BILLING_PROVIDERS` is untouched. A core module imports the same names from
here and keeps them as module-level names, which lets a test re-point one with `monkeypatch.setattr(webhooks,
"BILLING_REFUND_HOLD_HOURS", ...)`.

Spec 013 moved the rest of billing's variables here, with validators, defaults and environment names
unchanged:
- the price of a credit and whether to gate on it (`credits_per_usd`, `markup`, `credits_enforcement`,
  `credit_check_failure_policy`);
- the usage export worker's bounds (`billing_export_*`);
- the wallet reconciliation's window (`credit_reconcile_*`);
- the retention of the package's own usage events.

They used to be host policy about the agent's meter; the package now owns the meter's billing half, so a
project installing it needs them. What stays in the host's `Settings` is about the host's own meter: the caps,
the retention of the agent's `usage_events`, and the gateway reconciliation's tolerance and page ceiling.

Imports nothing from `app` (enforced by tests/billing/test_billing_core_boundary.py).
"""
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The webhook inbox keeps an event for at least this long before the retention sweep may delete it: longer
# than any payment provider keeps redelivering one, so a sweep never races a retry (packages/billing/src/credit_billing/inbox.py).
BILLING_INBOX_MIN_RETENTION_DAYS = 30
# The package's usage events are read by the wallet reconciliation (up to 35 days back) and exported to a provider that rejects events
# older than 35 days (the outbox gives up at 30), so a sweep below this could delete usage that is still inside a window something reads.
BILLING_USAGE_EVENT_MIN_RETENTION_DAYS = 35


class BillingSettings(BaseSettings):
    # `hide_input_in_errors`: a ValidationError normally prints the offending raw value (`input_value='...'`). This class
    # holds API keys and webhook signing secrets, and a settings error is printed at import time, to a terminal or a
    # container log, so a key pasted into the wrong variable (or a malformed BILLING_WEBHOOK_SECRETS) would be echoed
    # whole. The error still names the field and the rule; it just no longer repeats the value.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    # Money in (specs/010 PR 4, packages/billing/src/credit_billing/webhooks.py, POST /billing/webhooks/{provider}). A provider is
    # served only if it is NAMED here, and then it MUST have a signing secret: a webhook endpoint that cannot
    # verify a signature must not exist, so the process refuses to start rather than accept unauthenticated
    # payments. Empty (the default) means no provider and no webhook route to speak of: every request 404s.
    # Names are the adapter's `name` (packages/billing/src/credit_billing/providers/__init__.py), e.g. "stripe". "fake" is the in-repo
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
    # How long src/credit_billing/cli/inbox_sweep.py keeps applied/ignored inbox rows. The floor is not a style choice
    # (see BILLING_INBOX_MIN_RETENTION_DAYS). The wallet's own idempotency keys, not these rows, are what stop a
    # late redelivery from granting twice, so the sweep is safe, but the rows are the audit trail of what was received.
    billing_inbox_retention_days: int = Field(default=400, ge=BILLING_INBOX_MIN_RETENTION_DAYS)

    # --- usage: rating, the gate, export, reconciliation, retention (specs/010, moved here by specs/013) -------------------------
    # A unit of usage's price in credits is `round_half_up(cost_usd x CREDITS_PER_USD x MARKUP, 6)`, computed when it is recorded
    # (credit_billing/usage.py) and stored on the event with the rate used, so changing either later never rewrites what a past
    # call was charged.
    #
    # `credits_per_usd` has NO shipped default, on purpose: a price is a product decision and a default would put an accidental one
    # live. Unset means credits are off for the whole deployment: nothing is rated, debited, gated or shown, and every tenant behaves
    # exactly as before. Set, a tenant WITH a wallet (a `credit_accounts` row) is debited per recorded usage; a tenant without one is
    # untouched. `markup` is a multiplier on cost (1 = at cost, 1.5 = cost + 50%).
    credits_per_usd: Decimal | None = Field(default=None, gt=0)
    markup: Decimal = Field(default=Decimal("1"), gt=0)
    # Whether a tenant WITH a wallet whose available credits (balance minus the caller's in-flight holds) are not positive is refused
    # by `admit` (credit_billing/admission.py). Off by default: with a rate set and this off the wallet is still debited ("shadow
    # mode"), so an operator can watch balances move before anyone is refused. Needs CREDITS_PER_USD (a gate with no price is meaningless).
    credits_enforcement: bool = False
    # What the gate does when it cannot READ the wallet. "open" serves the caller and counts it (alert CreditGateUnenforced): a wallet
    # outage must not also take every request down. "closed" refuses it as unavailable, for a deployment that would rather be unavailable
    # than serve a tenant it cannot confirm has credit. Only consulted when enforcing.
    credit_check_failure_policy: Literal["open", "closed"] = "open"

    # Money out (credit_billing/export.py, `credit-billing-export-worker`): usage events of a tenant linked to a provider that bills on
    # usage are exported through an outbox. All of it is bounded: a loop with no ceiling is how a provider's outage becomes a flood of
    # retries, or events the provider silently discards once they age out.
    billing_export_batch_size: int = Field(default=100, ge=1, le=1000)
    billing_export_interval_seconds: int = Field(default=30, ge=1)
    # Retryable failures of one event before it is given up on (`failed`, alert UsageExportFailed).
    billing_export_max_attempts: int = Field(default=10, ge=1)
    # After the k-th failed attempt an event waits min(base * 2**(k-1), cap) before the next: exponential, so a provider that is down is
    # not hammered, and capped, so a long outage still retries within hours rather than days.
    billing_export_backoff_base_seconds: int = Field(default=30, ge=1)
    billing_export_backoff_cap_seconds: int = Field(default=6 * 3600, ge=1)
    # An event still unsent after this many days is `expired`, counted and alerted, never silently dropped. The ceiling is a provider fact,
    # not a style choice: Stripe accepts a meter event only if its timestamp is within the past 35 days (research R1), so the age limit
    # must close BEFORE the provider's own window does or a "retry" is a silent discard.
    billing_export_max_age_days: int = Field(default=30, ge=1, le=34)
    # The deadline for ONE call to a provider. The worker holds the batch's row locks while it waits, so a wait with no ceiling would hold
    # them (and every other worker's view of those rows) for as long as a provider chooses to hang. A call that times out may still have
    # landed: it is retried with the same event id, which the provider dedupes.
    billing_export_call_timeout_seconds: int = Field(default=30, ge=1)

    # The wallet reconciliation (credit_billing/reconcile.py, `credit-billing-reconcile`): was every recorded usage worth credits actually
    # debited? The newest minutes are left out (a debit may still be in flight in another transaction); the lookback is short on purpose
    # (a run that re-reads a year is a load, and a gap older than this was already reported by every run since it appeared).
    credit_reconcile_settle_seconds: int = Field(default=900, ge=0)
    credit_reconcile_lookback_days: int = Field(default=2, ge=1, le=35)
    credit_reconcile_interval_seconds: int = Field(default=6 * 3600, ge=60)

    # How long `credit-billing-usage-sweep` keeps the package's usage events (an event whose export never finished is kept whatever its age,
    # and the wallet's debits are never touched). The floor is not a style choice (see BILLING_USAGE_EVENT_MIN_RETENTION_DAYS).
    billing_usage_event_retention_days: int = Field(default=400, ge=BILLING_USAGE_EVENT_MIN_RETENTION_DAYS)

    @field_validator("billing_providers")
    @classmethod
    def _provider_names_are_path_safe(cls, value: str) -> str:
        for name in (part.strip() for part in value.split(",") if part.strip()):
            if not name.replace("_", "").replace("-", "").isalnum() or not name.islower():
                raise ValueError(f"BILLING_PROVIDERS entry {name!r} must be lowercase letters, digits, '-' or '_'")
        return value

    @field_validator("credits_per_usd", "markup")
    @classmethod
    def _six_places_at_most(cls, value: Decimal | None) -> Decimal | None:
        """The columns that record these on each event are NUMERIC(18,6): a rate with more places would be stored rounded, and the row
        would no longer reproduce the credits it was charged."""
        if value is not None and value != value.quantize(Decimal("0.000001")):
            raise ValueError("at most 6 decimal places (the event row stores the rate as NUMERIC(18,6))")
        return value

    @model_validator(mode="after")
    def _enforcement_needs_a_rate(self) -> "BillingSettings":
        if self.credits_enforcement and self.credits_per_usd is None:
            raise ValueError("CREDITS_ENFORCEMENT=true needs CREDITS_PER_USD: a gate with no price is meaningless")
        return self

    @model_validator(mode="after")
    def _backoff_cap_is_not_below_its_base(self) -> "BillingSettings":
        if self.billing_export_backoff_cap_seconds < self.billing_export_backoff_base_seconds:
            raise ValueError("BILLING_EXPORT_BACKOFF_CAP_SECONDS must be at least BILLING_EXPORT_BACKOFF_BASE_SECONDS")
        return self

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
CREDITS_PER_USD = settings.credits_per_usd
MARKUP = settings.markup
CREDITS_ENFORCEMENT = settings.credits_enforcement
CREDIT_CHECK_FAILURE_POLICY = settings.credit_check_failure_policy
BILLING_EXPORT_BATCH_SIZE = settings.billing_export_batch_size
BILLING_EXPORT_INTERVAL_SECONDS = settings.billing_export_interval_seconds
BILLING_EXPORT_MAX_ATTEMPTS = settings.billing_export_max_attempts
BILLING_EXPORT_BACKOFF_BASE_SECONDS = settings.billing_export_backoff_base_seconds
BILLING_EXPORT_BACKOFF_CAP_SECONDS = settings.billing_export_backoff_cap_seconds
BILLING_EXPORT_MAX_AGE_DAYS = settings.billing_export_max_age_days
BILLING_EXPORT_CALL_TIMEOUT_SECONDS = settings.billing_export_call_timeout_seconds
CREDIT_RECONCILE_SETTLE_SECONDS = settings.credit_reconcile_settle_seconds
CREDIT_RECONCILE_LOOKBACK_DAYS = settings.credit_reconcile_lookback_days
CREDIT_RECONCILE_INTERVAL_SECONDS = settings.credit_reconcile_interval_seconds
BILLING_USAGE_EVENT_RETENTION_DAYS = settings.billing_usage_event_retention_days

"""The billing core's own counters (specs/011-billing-module-boundary, contracts/billing-core-boundary.md).

Four counters used to live in `app/core/metrics.py`, which the billing code imported. They are here now, with
their names, labels and help text exactly as they were, so every dashboard panel and alert rule on them keeps
working and `app/billing/` no longer needs the host's metrics module to be importable.

They are built on the OpenTelemetry **API** only. A library depends on the API and emits through whatever
provider the process installed; the SDK, the exporter, the collector endpoint and `service.name` stay the
process's concern (`app/core/telemetry.py::configure_telemetry` in the agent app). `get_meter` returns a proxy
until a provider is installed and replays the instruments created against it once one is, so the order in which
the host and this module are imported does not matter, and a process that never configures telemetry just
records nothing.

`Counter` below is a ~25-line copy of the host wrapper (`app/core/metrics.py::Counter`): the same
`x.labels(k=v).inc()` surface existing call sites use, and a `.name` attribute, which is how
`tests/conftest.py::metric_value` finds a counter's data points (an OTel instrument does not expose its own
name back). The copy is the price of the core not importing the host.

**The meter scope string is deliberately still "app.core.metrics".** The collector's Prometheus exporter turns the
scope into an `otel_scope_name` label and does NOT merge series: the same metric name emitted under two scopes
comes out as two series (specs/011 research.md P6, verified with a throwaway collector running this repo's
exporter config). Renaming the scope would fork every series below, and the two alert rules on
`agent_billing_webhook_total` are bare `increase(...)` expressions, which cannot count the first sample of a
series that did not exist before. Changing it is decided when this module becomes its own package, with the
known label sets pre-created at 0 and those rules audited.

Imports nothing from `app` (enforced by tests/billing/test_billing_core_boundary.py).
"""
from collections.abc import Mapping

from opentelemetry import metrics as metrics_api

# See the module docstring before changing this string.
_meter = metrics_api.get_meter("app.core.metrics")


class _BoundCounter:
    __slots__ = ("_instrument", "_attributes")

    def __init__(self, instrument: metrics_api.Counter, attributes: Mapping[str, str]):
        self._instrument = instrument
        self._attributes = attributes

    def inc(self, amount: float = 1) -> None:
        self._instrument.add(amount, attributes=self._attributes)


class Counter:
    """prometheus_client.Counter-shaped wrapper around an OTel Counter."""

    def __init__(self, name: str, description: str = "", labelnames=()):
        self.name = name  # read by tests/conftest.py::metric_value to look up this
        # instrument's recorded data points via an InMemoryMetricReader —
        # OTel instruments don't expose their own name back once created.
        self._instrument = _meter.create_counter(name, description=description)
        self._labelnames = tuple(labelnames)

    def labels(self, **kwargs: str) -> _BoundCounter:
        return _BoundCounter(self._instrument, kwargs)

    def inc(self, amount: float = 1) -> None:
        self._instrument.add(amount)


agent_credit_overdraft_total = Counter(
    "agent_credit_overdraft_total",
    "Debits larger than the tenant's available credits (app/billing/credits.py): the model call "
    "had already happened, so the shortfall was booked as overdraft instead of refused. With "
    "gating on this should stay near zero; sustained growth means usage is outrunning the wallet "
    "(a missed top-up, or gating off).",
)

agent_billing_webhook_total = Counter(
    "agent_billing_webhook_total",
    "Payment-provider webhook deliveries by outcome (POST /billing/webhooks/{provider}, app/billing/webhooks.py). "
    "`quarantined` means a customer PAID and was granted nothing and nobody is retrying: it pages "
    "(BillingWebhookQuarantined). `failed` means applying it raised and the provider will retry: it pages too "
    "(BillingWebhookFailing), because a sustained database fault would otherwise surface only as a provider's "
    "dashboard. `invalid_signature` is a forged or misconfigured delivery. `provider` is a configured adapter "
    "name or `unknown` (never the caller's own string: an unauthenticated route must not mint label values).",
    ["provider", "outcome"],
)  # outcome: applied | duplicate | ignored | quarantined | retry | failed | invalid_signature | invalid_payload | unknown_provider | too_large

agent_credit_granted_total = Counter(
    "agent_credit_granted_total",
    "Credits added to wallets (app/billing/credits.py), by lot source: purchase | subscription | promo | manual | "
    "adjustment. Counted when the grant is applied, inside the caller's transaction, so one that later rolls back "
    "is over-counted: this is a rate for a dashboard, and the wallet's entries are the record.",
    ["source"],
)

agent_credit_debited_total = Counter(
    "agent_credit_debited_total",
    "Credits taken from wallets, by transaction kind: debit (a model call) | clawback (a refund) | adjust (an "
    "operator correction). Expiry is not here: it is credits that left unused, not credits used. Counted at apply "
    "time like agent_credit_granted_total, with the same caveat.",
    ["kind"],
)

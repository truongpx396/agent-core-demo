"""The billing core's own counters and gauges (specs/011-billing-module-boundary, contracts/billing-core-boundary.md).

Four counters used to live in `app/core/metrics.py`, which the billing code imported. They are here now, with
their names, labels and help text exactly as they were, so every dashboard panel and alert rule on them keeps
working and the billing package no longer needs any host's metrics module to be importable. Spec 013 moved the rest of what the package
now owns the same way: the usage export's counter and age gauge, the wallet reconciliation's counter and gauges, the credit gate's
refusal counter, and `agent_cost_governance_degraded_total` for the paths billing counts (`billing_event_write`, `credit_debit`,
`export_enqueue`, `credit_read`). Names, labels and help are unchanged, so no alert or panel changed; the host keeps its own
instrument of that last name for the paths it counts, and the two are told apart only by the scope label, which `sum by` and the
alerts' `increase(...{path=...})` ignore.

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

**The meter scope is `credit_billing` and carries no version** (spec 011 D23). The collector's Prometheus exporter turns the scope
into an `otel_scope_name` label and does NOT merge series: the same metric name emitted under two scopes comes out as two
series (verified with a throwaway collector running this repo's exporter config; research.md P6). Until the package was
extracted these counters kept the agent app's scope, `app.core.metrics`, so that moving the code forked nothing. A package other
projects install should not emit another project's scope, so the scope changed once, here. Dashboards aggregate with `sum by`,
so they carry across; the two alert rules on `agent_billing_webhook_total` are bare `increase(...)` expressions, which cannot
count the FIRST sample of a series that did not exist before, so `initialise_webhook_series` creates every series the alerts
look at, at 0, when the process validates its configuration. That closes a gap that existed before the scope change too (the
first-ever quarantined event for a provider was invisible to the alert). No scope VERSION is set on purpose: a release number
in the scope would fork every series again at every release.

Imports nothing from `app` (enforced by tests/billing/test_billing_core_boundary.py).
"""
from collections.abc import Iterable, Mapping
from typing import Any

from opentelemetry import metrics as metrics_api

# See the module docstring before changing this string (and never add a version argument).
_meter = metrics_api.get_meter("credit_billing")


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


class _BoundGauge:
    __slots__ = ("_instrument", "_attributes")

    # `Any`: this OpenTelemetry release exposes the synchronous Gauge only under a private name (`_Gauge`), while
    # `Meter.create_gauge` that returns it is public; naming the private class here would break on the next release.
    def __init__(self, instrument: Any, attributes: Mapping[str, str]):
        self._instrument = instrument
        self._attributes = attributes

    def set(self, value: float) -> None:
        self._instrument.set(value, attributes=self._attributes)


class Gauge:
    """prometheus_client.Gauge-shaped wrapper around an OTel synchronous Gauge: the LAST value set wins, which is what a
    "how old is the oldest unsent thing right now" reading needs and a Counter cannot say."""

    def __init__(self, name: str, description: str = "", unit: str = "", labelnames=()):
        self.name = name
        self._instrument = _meter.create_gauge(name, description=description, unit=unit)
        self._labelnames = tuple(labelnames)

    def labels(self, **kwargs: str) -> _BoundGauge:
        return _BoundGauge(self._instrument, kwargs)

    def set(self, value: float) -> None:
        self._instrument.set(value)


agent_credit_overdraft_total = Counter(
    "agent_credit_overdraft_total",
    "Debits larger than the tenant's available credits (credit_billing/credits.py): the model call "
    "had already happened, so the shortfall was booked as overdraft instead of refused. With "
    "gating on this should stay near zero; sustained growth means usage is outrunning the wallet "
    "(a missed top-up, or gating off).",
)

agent_billing_webhook_total = Counter(
    "agent_billing_webhook_total",
    "Payment-provider webhook deliveries by outcome (POST /billing/webhooks/{provider}, credit_billing/webhooks.py). "
    "`quarantined` means a customer PAID and was granted nothing and nobody is retrying: it pages "
    "(BillingWebhookQuarantined). `failed` means applying it raised and the provider will retry: it pages too "
    "(BillingWebhookFailing), because a sustained database fault would otherwise surface only as a provider's "
    "dashboard. `invalid_signature` is a forged or misconfigured delivery. `provider` is a configured adapter "
    "name or `unknown` (never the caller's own string: an unauthenticated route must not mint label values).",
    ["provider", "outcome"],
)  # outcome: applied | duplicate | ignored | quarantined | retry | failed | invalid_signature | invalid_payload | unknown_provider | too_large

agent_credit_granted_total = Counter(
    "agent_credit_granted_total",
    "Credits added to wallets (credit_billing/credits.py), by lot source: purchase | subscription | promo | manual | "
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


# Every `outcome` label value `agent_billing_webhook_total` can carry: the six `webhooks.process_event` returns and the four the route
# itself counts before an event exists. tests/test_metrics.py fails if the code can emit one that is not listed here.
WEBHOOK_OUTCOMES = (
    "applied", "duplicate", "ignored", "quarantined", "retry", "failed",
    "invalid_signature", "invalid_payload", "unknown_provider", "too_large",
)
_NO_PROVIDER_OUTCOMES = ("unknown_provider",)  # counted under the fixed label `unknown`, never a caller's own string


def initialise_webhook_series(providers: Iterable[str]) -> None:
    """Creates, at 0, every `agent_billing_webhook_total` series the alerts and the dashboard look at: each configured provider with
    every outcome a delivery for a known provider can have, and `unknown` with `unknown_provider`. A counter series otherwise
    appears on its first increment, and an `increase(...)` over a series whose first sample is 1 is 0 (see the module docstring)."""
    for provider in providers:
        for outcome in WEBHOOK_OUTCOMES:
            if outcome not in _NO_PROVIDER_OUTCOMES:
                agent_billing_webhook_total.labels(provider=provider, outcome=outcome).inc(0)
    for outcome in _NO_PROVIDER_OUTCOMES:
        agent_billing_webhook_total.labels(provider="unknown", outcome=outcome).inc(0)


agent_credit_enforcement_refused_total = Counter(
    "agent_credit_enforcement_refused_total",
    "Requests refused before any work because the tenant's wallet had no available credits (credit_billing/admission.py). "
    "Normal operation for a prepaid tenant that has run out, so it is a rate to watch, not an alert. It has no tenant label "
    "(who is in the log line).",
)

agent_usage_export_total = Counter(
    "agent_usage_export_total",
    "Usage events handled by the export worker (credit_billing/export.py), by outcome. `expired` means an event aged past "
    "BILLING_EXPORT_MAX_AGE_DAYS unsent (alert UsageExportExpired: usage the provider will never bill); `failed` means a "
    "permanent refusal, the attempt budget spent, or no customer link (alert UsageExportFailed); `retry` is a retryable "
    "failure that will back off and try again; `sent` includes a provider-reported duplicate, which is success.",
    ["provider", "outcome"],
)  # outcome: sent | retry | failed | expired

agent_usage_export_oldest_pending_age_seconds = Gauge(
    "agent_usage_export_oldest_pending_age_seconds",
    "Age of the oldest usage event still waiting to be exported, per provider, set each worker pass (0 when nothing is "
    "waiting). Alert UsageExportStuck at 7 days: well inside the age limit, so someone is told while the events are "
    "still sendable. Disclosed (verified in spec 010 PR 6): a synchronous gauge is exported ONCE per set (the SDK "
    "clears it after collecting) and the collector's Prometheus exporter drops a series 5 minutes after its last update "
    "(`metric_expiration`), so a dead worker does not leave a stale value behind: the series DISAPPEARS and UsageExportStuck "
    "resolves on its own. The worker's own liveness is not alerted yet; Prometheus cannot tell a worker that never ran from one that died.",
    unit="s",
    labelnames=["provider"],
)

agent_credit_outstanding = Gauge(
    "agent_credit_outstanding",
    "Credits held across every wallet, set by each reconciliation pass (credit_billing/reconcile.py): `available` is what "
    "may still be consumed, `debt` is what overdraft lots owe (a positive number). Deliberately no tenant label: who "
    "holds what is `credit-billing show --tenant ...`. Only as fresh as the last pass.",
    labelnames=["state"],
)

agent_credit_reconcile_max_drift_usd = Gauge(
    "agent_credit_reconcile_max_drift_usd",
    "The largest per-tenant, per-day figure, in USD, of usage events worth credits that no debit was booked for, as of the last "
    "wallet reconciliation pass (0 when everything is charged). No tenant label: the report (`credit-billing-reconcile`) names "
    "them. Alert CreditReconcileDrift. The worker re-sets it every minute because the collector forgets a series 5 minutes after "
    "its last update.",
    unit="USD",
)

agent_credit_reconcile_total = Counter(
    "agent_credit_reconcile_total",
    "Wallet reconciliation passes by outcome: ok (every event worth credits was charged) | drift (some were not) | failed (the "
    "pass itself raised: the database was unreachable; alert CreditReconcileNotCompleting).",
    ["outcome"],
)  # outcome: ok | drift | failed

agent_cost_governance_degraded_total = Counter(
    "agent_cost_governance_degraded_total",
    "Cost-governance paths that failed and carried on instead of failing the caller (spec 008 A1). The billing package counts "
    "`billing_event_write` (the usage event could not be recorded for billing: lost revenue, alert BillingEventWriteFailing), "
    "`credit_debit` (the event was kept, its debit failed: alert CreditDebitFailing), `export_enqueue` (the event was kept, queuing "
    "it for export failed: alert UsageExportEnqueueFailing) and `credit_read` (the gate could not read a wallet: alert "
    "CreditGateUnenforced).",
    ["path"],
)  # path: billing_event_write | credit_debit | export_enqueue | credit_read

# Every `path` label value the package can count; tests/test_metrics.py fails if the code can emit one that is not listed here.
DEGRADED_PATHS = ("billing_event_write", "credit_debit", "export_enqueue", "credit_read")
RECONCILE_OUTCOMES = ("ok", "drift", "failed")

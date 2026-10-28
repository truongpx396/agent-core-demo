package telemetry

// The metric names are literals in ONE place, on purpose. tests/core/test_alert_rules.py reads this file with a regular expression and
// fails when an alert in observability/prometheus/alerts.yml names an `agent_authlimit_*` metric that is not defined here: an alert on a
// metric nothing records is silent forever and nothing about it looks wrong. Keep each name as a plain double-quoted literal.
const (
	NameRequests        = "agent_authlimit_requests_total"
	NameRequestDuration = "agent_authlimit_request_duration_seconds"
	NameKeyRefresh      = "agent_authlimit_key_refresh_total"
	NameKeysAge         = "agent_authlimit_keys_age_seconds"
	NameRevocationAge   = "agent_authlimit_revocation_age_seconds"
	NameStoreErrors     = "agent_authlimit_store_errors_total"
	NameReady           = "agent_authlimit_ready"
	NameSigningKeyAge   = "agent_authlimit_signing_key_age_seconds"
)

// The only attribute keys any metric carries. There is deliberately no tenant, principal, token or claim label: a label with one value
// per person is unbounded cardinality and an identifier in a metrics backend; telemetry_test.go fails if a key outside this set appears.
const (
	AttrRouteClass = "route_class"
	AttrOutcome    = "outcome"
	AttrReason     = "reason"
	AttrResult     = "result"
	AttrStore      = "store"
)

// Values of the `result` and `store` attributes.
const (
	ResultOK    = "ok"
	ResultError = "error"

	StoreLimiter    = "limiter"
	StoreRevocation = "revocation"
)

// latencyBuckets are explicit (the constitution asks for them): a decision is microseconds to a few milliseconds, with a 100 ms Redis
// ceiling and a 5 s key fetch as the long tail.
var latencyBuckets = []float64{0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1}

// Package reason is the one vocabulary for "why was this request refused (or how was it decided)".
//
// The same strings are metric label values (`agent_authlimit_requests_total{reason=...}`), log fields and test expectations, and
// observability/prometheus/alerts.yml names two of them. They are therefore constants in one place: a typo in a label would make an alert
// silent forever without anything looking wrong (the failure tests/core/test_alert_rules.py exists for).
//
// A reason is never a token, a claim value or an exception's text; it is one of these fixed words.
package reason

type Reason string

const (
	// None means the request was decided without a refusal reason (allowed, passed through, or public).
	None Reason = ""

	// Token refusals, in the order the verifier checks them.
	MissingToken    Reason = "missing_token"
	Malformed       Reason = "malformed"
	BadAlgorithm    Reason = "bad_algorithm"
	BadSignature    Reason = "bad_signature"
	BadIssuer       Reason = "bad_issuer"
	BadAudience     Reason = "bad_audience"
	Expired         Reason = "expired"
	NotYetValid     Reason = "not_yet_valid"
	NoIssueTime     Reason = "no_issue_time"
	LifetimeTooLong Reason = "lifetime_too_long"
	BadClaims       Reason = "bad_claims"
	BadScope        Reason = "bad_scope"

	// Policy refusals.
	Revoked Reason = "revoked"

	// Availability: the service cannot decide, so it refuses (fail closed) or, for the limiter only, serves.
	KeysUnavailable       Reason = "keys_unavailable"
	RevocationUnavailable Reason = "revocation_unavailable"
	// SigningUnavailable: no internal signing key is current, so no identity can be minted (the replica reports not ready).
	SigningUnavailable Reason = "signing_unavailable"

	// UnlistedRoute is not a refusal: a route the table does not list is verified and limited like a turn, and counted under this reason
	// so a route that drifted out of the table is visible.
	UnlistedRoute Reason = "unlisted_route"
)

// Outcome is the other label of the request counter.
type Outcome string

const (
	Allowed     Outcome = "allowed"     // served (the identity was minted, or the request was public)
	Passthrough Outcome = "passthrough" // MCP limit-only mode: served without an identity so the MCP gateway answers it itself
	Refused     Outcome = "refused"     // authentication failed, or the service cannot decide
	Limited     Outcome = "limited"     // a bucket was empty and the mode is enforce
	WouldLimit  Outcome = "would_limit" // a bucket was empty and the mode is observe: served, counted
)

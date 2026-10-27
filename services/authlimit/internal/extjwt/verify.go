// Package extjwt verifies the access token the authorization server (Casdoor) issued, offline, against its cached signing keys.
//
// The verifier answers one question for a token: valid, with this tenant and person, or refused, for one reason from the reason package.
// It is written to be hard to get wrong, from spec 017's list of verifier mistakes (no audience check, no issuer check, a huge clock
// leeway, a scope parser that raises, keys that never go stale) and the threat model:
//
//   - The algorithm comes from the allow-list (RS256), never from the token's own header. `alg: none`, and HMAC signed with the public
//     key, are refused by name before any key is used (threat T2).
//   - The library checks the signature only. Every claim check is done here, in a fixed order, so each refusal has one precise reason
//     and a token with several defects always gets the same one. (A library that validates claims in its own order and returns a joined
//     error makes the reason depend on error-wrapping details.)
//   - It never panics and never returns an error value: a hostile token is a refusal with a counted reason, not an exception. The
//     reason is a fixed word; the token, a claim value and an error's text never leave this package.
//
// Two profiles, because the service uses the token for two jobs. Chat checks everything the chat API needs. Identify, used on the MCP
// routes where the MCP gateway does the real verification, checks only enough to learn who is calling (signature, issuer, expiry,
// tenant and person), and must never refuse a token the MCP gateway accepts, or that token would escape the limiter: the shared vectors
// (testdata/auth-vectors) assert exactly that.
package extjwt

import (
	"context"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"errors"
	"math"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"

	"authlimit/internal/reason"
)

// Profile selects how much of the token is checked.
type Profile int

const (
	// Chat is the chat API: audience, scope, an issue time and the lifetime cap are required on top of Identify's checks.
	Chat Profile = iota
	// Identify is the MCP routes' limit-only mode.
	Identify
)

const (
	allowedAlg = "RS256"
	// maxTokenBytes bounds what is parsed. Casdoor's default token is ~3 KB; 16 KiB is generous and stops a megabyte of junk costing a
	// base64 decode and a JSON parse per request.
	maxTokenBytes = 16 << 10
)

// KeySource gives the verification key for a key id. ErrUnknownKey and ErrKeysUnavailable are the two answers the verifier understands;
// any other error is treated as keys unavailable.
type KeySource interface {
	Key(ctx context.Context, kid string) (*rsa.PublicKey, error)
}

// Settings are the verifier's inputs (config.Config supplies them).
type Settings struct {
	Issuer         string
	Audience       string
	RequiredScope  string
	TenantClaim    string
	PrincipalClaim string
	MaxLifetime    time.Duration
	Leeway         time.Duration
}

// Claims is what a valid token yields. Nothing else of the token is kept.
type Claims struct {
	Tenant    string
	Principal string
	Scopes    []string
	// IssuedAt is the token's `iat` in Unix seconds. In the Identify profile a token without one gets 0, which makes any revocation of
	// the person void it: the safe direction.
	IssuedAt  int64
	ExpiresAt int64
	KeyID     string
}

// Verifier checks tokens. It holds no per-request state and is safe for concurrent use.
type Verifier struct {
	s    Settings
	keys KeySource
	now  func() time.Time
}

// NewVerifier builds a verifier. `now` may be nil (the wall clock); tests and the vectors inject a fixed clock.
func NewVerifier(s Settings, keys KeySource, now func() time.Time) *Verifier {
	if now == nil {
		now = time.Now
	}
	return &Verifier{s: s, keys: keys, now: now}
}

// Verify checks a raw token (without the "Bearer " prefix). On success the reason is reason.None.
func (v *Verifier) Verify(ctx context.Context, raw string, p Profile) (Claims, reason.Reason) {
	if raw == "" {
		return Claims{}, reason.MissingToken
	}
	if len(raw) > maxTokenBytes {
		return Claims{}, reason.Malformed
	}
	kid, why := v.header(raw)
	if why != reason.None {
		return Claims{}, why
	}
	if kid == "" {
		// No key id names no key. Refused here, before the key source: asking it would cost a (rate-limited) refresh per such token.
		return Claims{}, reason.BadSignature
	}
	key, err := v.keys.Key(ctx, kid)
	switch {
	case errors.Is(err, ErrUnknownKey):
		return Claims{}, reason.BadSignature
	case err != nil:
		return Claims{}, reason.KeysUnavailable
	}

	// The signature, and nothing else: claims are checked below, in our order.
	parser := jwt.NewParser(jwt.WithValidMethods([]string{allowedAlg}), jwt.WithoutClaimsValidation())
	mc := jwt.MapClaims{}
	if _, err := parser.ParseWithClaims(raw, mc, func(*jwt.Token) (any, error) { return key, nil }); err != nil {
		switch {
		case errors.Is(err, jwt.ErrTokenSignatureInvalid):
			return Claims{}, reason.BadSignature
		case errors.Is(err, jwt.ErrTokenMalformed):
			return Claims{}, reason.Malformed
		default:
			return Claims{}, reason.BadSignature
		}
	}
	claims, why := v.claims(mc, p)
	if why != reason.None {
		return Claims{}, why
	}
	claims.KeyID = kid
	return claims, reason.None
}

// header decodes the unverified header far enough to pick the key and to refuse a wrong algorithm before touching any key.
func (v *Verifier) header(raw string) (kid string, why reason.Reason) {
	parts := strings.Split(raw, ".")
	if len(parts) != 3 || parts[0] == "" || parts[1] == "" {
		return "", reason.Malformed
	}
	hb, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return "", reason.Malformed
	}
	var h struct {
		Alg any `json:"alg"`
		Kid any `json:"kid"`
	}
	if err := json.Unmarshal(hb, &h); err != nil {
		return "", reason.Malformed
	}
	if alg, _ := h.Alg.(string); alg != allowedAlg {
		return "", reason.BadAlgorithm
	}
	kid, _ = h.Kid.(string)
	return kid, reason.None // an empty kid finds no key and is refused as a bad signature by the key source
}

// claims runs every claim check in the documented order.
func (v *Verifier) claims(mc jwt.MapClaims, p Profile) (Claims, reason.Reason) {
	now := v.now()

	if iss, _ := mc["iss"].(string); iss != v.s.Issuer {
		return Claims{}, reason.BadIssuer
	}
	if p == Chat && !hasAudience(mc["aud"], v.s.Audience) {
		return Claims{}, reason.BadAudience
	}

	exp, ok := numericDate(mc["exp"])
	if !ok {
		return Claims{}, reason.BadClaims // a token that never expires, or an exp that is not a number
	}
	if now.After(time.Unix(exp, 0).Add(v.s.Leeway)) {
		return Claims{}, reason.Expired
	}
	if raw, present := mc["nbf"]; present {
		nbf, ok := numericDate(raw)
		if !ok {
			return Claims{}, reason.BadClaims
		}
		if now.Add(v.s.Leeway).Before(time.Unix(nbf, 0)) {
			return Claims{}, reason.NotYetValid
		}
	}
	var iat int64
	if raw, present := mc["iat"]; present {
		var ok bool
		if iat, ok = numericDate(raw); !ok {
			return Claims{}, reason.BadClaims
		}
		if now.Add(v.s.Leeway).Before(time.Unix(iat, 0)) {
			return Claims{}, reason.NotYetValid
		}
	}

	// `sub` must be text whatever claim names the person; PyJWT refuses a non-text subject too, and the two verifiers must agree.
	if sub, _ := mc["sub"].(string); sub == "" {
		return Claims{}, reason.BadClaims
	}
	tenant, _ := mc[v.s.TenantClaim].(string)
	principal, _ := mc[v.s.PrincipalClaim].(string)
	if tenant == "" || principal == "" {
		return Claims{}, reason.BadClaims
	}

	out := Claims{Tenant: tenant, Principal: principal, IssuedAt: iat, ExpiresAt: exp}
	if p == Identify {
		return out, reason.None
	}

	if _, present := mc["iat"]; !present {
		return Claims{}, reason.NoIssueTime
	}
	if time.Duration(exp-iat)*time.Second > v.s.MaxLifetime {
		return Claims{}, reason.LifetimeTooLong
	}
	scope, _ := mc["scope"].(string)
	scopes := strings.Fields(scope)
	if !contains(scopes, v.s.RequiredScope) {
		return Claims{}, reason.BadScope
	}
	out.Scopes = scopes
	return out, reason.None
}

// hasAudience: `aud` is a string or a list of strings (RFC 7519); anything else matches nothing.
func hasAudience(aud any, want string) bool {
	switch a := aud.(type) {
	case string:
		return a == want
	case []any:
		for _, x := range a {
			if s, ok := x.(string); ok && s == want {
				return true
			}
		}
	}
	return false
}

// numericDate reads a JWT NumericDate. It accepts only a finite number within a sane range; a string, a bool or a number like 1e300
// (which would overflow a time conversion) is refused rather than guessed at.
func numericDate(v any) (int64, bool) {
	var f float64
	switch n := v.(type) {
	case float64:
		f = n
	case json.Number:
		var err error
		if f, err = n.Float64(); err != nil {
			return 0, false
		}
	default:
		return 0, false
	}
	if math.IsNaN(f) || math.IsInf(f, 0) || f < 0 || f > 1e12 { // 1e12 s is the year 33658
		return 0, false
	}
	return int64(f), true
}

func contains(list []string, want string) bool {
	for _, s := range list {
		if s == want {
			return true
		}
	}
	return false
}

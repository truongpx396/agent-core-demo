// Package server is the HTTP face of the service: the check Caddy's forward_auth calls on every request, plus liveness, readiness and the
// public keys of the internal identity.
//
// The decision for one request, in order (contracts/authlimit.md section A):
//
//	classify the route  →  verify the token  →  revocation  →  rate limit  →  mint the internal identity
//
// The ORDER is a security property, not a style choice:
//
//   - Revocation comes before the limiter. A revoked person must be refused without consuming a token from any bucket; the other order
//     lets a revoked token drain its tenant's bucket (the mutation list has "revocation checked after the limiter").
//   - The limiter only ever sees a verified tenant and person. A bucket keyed on an unverified claim is a bucket anyone can fill on
//     someone else's behalf (threat T12).
//   - Nothing is minted for a refused or limited request: the internal identity is the only thing the API trusts, so it is produced last.
//
// Failure policy (Principle V), decided here in one place:
//
//	verification cannot be done (no usable keys)   refuse, 503, never serve   (a security check fails closed)
//	revocation list too old to trust               refuse, 503 (default; configurable)
//	the limiter's store is down or slow            SERVE, count it            (a protection fails open)
package server

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"authlimit/internal/config"
	"authlimit/internal/errenv"
	"authlimit/internal/extjwt"
	"authlimit/internal/identity"
	"authlimit/internal/limiter"
	"authlimit/internal/reason"
	"authlimit/internal/revoke"
	"authlimit/internal/telemetry"
)

// Realm is the HTTP authentication realm named in WWW-Authenticate.
const Realm = "agent-core-api"

// IdentityHeader is the header Caddy copies from the 2xx answer onto the request that goes to the API.
const IdentityHeader = "X-Internal-Identity"

// KeyStatus is the part of the key cache the server reads (readiness).
type KeyStatus interface {
	State() (extjwt.KeyState, time.Duration)
}

// Revocations is the part of the revocation list the server reads.
type Revocations interface {
	Check(tenant, principal string, iat int64) revoke.Verdict
	Usable() bool
}

// Deps is everything the server needs, injected so a test can replace the store, the clock and the lists.
type Deps struct {
	Cfg      config.Config
	Table    *limiter.Table
	Verifier *extjwt.Verifier
	Keys     KeyStatus
	Keyset   *identity.Keyset
	Minter   *identity.Minter
	Limiter  limiter.Limiter
	Revoked  Revocations
	Metrics  *telemetry.Metrics
	Log      *slog.Logger
	Now      func() time.Time
}

// Server answers the check and the health endpoints.
type Server struct {
	Deps
}

// New builds the server. Metrics and Log may be nil.
func New(d Deps) *Server {
	if d.Now == nil {
		d.Now = time.Now
	}
	if d.Log == nil {
		d.Log = slog.New(slog.DiscardHandler)
	}
	return &Server{Deps: d}
}

// Handler is the HTTP handler.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/check", s.check)
	mux.HandleFunc("GET /healthz", s.healthz)
	mux.HandleFunc("GET /readyz", s.readyz)
	mux.HandleFunc("GET /internal/jwks.json", s.jwks)
	return mux
}

// HTTPServer wraps the handler with the timeouts a service on the request path needs: every wait has a ceiling (Principle V).
func (s *Server) HTTPServer(addr string) *http.Server {
	return &http.Server{
		Addr: addr, Handler: s.Handler(),
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 15 * time.Second, IdleTimeout: 60 * time.Second,
		MaxHeaderBytes: 32 << 10, // a Casdoor token is ~1-3 KB; 32 KiB leaves room and bounds a hostile header
	}
}

// --- the check ---------------------------------------------------------------------------------------------------------------------

func (s *Server) check(w http.ResponseWriter, r *http.Request) {
	start := s.Now()
	// A forged value cannot decide the class: if Caddy's header was duplicated (a client added its own), or is missing, the request is
	// classified as an unclean path, which can only ever mean the fallback (verified and limited), never public.
	method, uri := "", ""
	if m, u := r.Header.Values("X-Forwarded-Method"), r.Header.Values("X-Forwarded-Uri"); len(m) == 1 && len(u) == 1 {
		method, uri = m[0], u[0]
	}
	class, listed := s.Table.Classify(method, uri)

	// The whole check, including a forced key refresh on an unknown key id, is bounded.
	ctx, cancel := context.WithTimeout(r.Context(), s.Cfg.JWKSFetchTimeout+time.Second)
	defer cancel()

	switch class {
	case limiter.Public:
		s.done(w, class, reason.Allowed, reason.None, start)
	case limiter.MCP:
		s.checkMCP(ctx, w, r, class, uri, start)
	default:
		s.checkVerified(ctx, w, r, class, listed, start)
	}
}

func (s *Server) checkVerified(ctx context.Context, w http.ResponseWriter, r *http.Request, class limiter.Class, listed bool, start time.Time) {
	routeReason := reason.None
	if !listed {
		routeReason = reason.UnlistedRoute
	}
	token, why := bearer(r)
	if why != reason.None {
		s.refuse(w, class, why, start)
		return
	}
	claims, why := s.Verifier.Verify(ctx, token, extjwt.Chat)
	if why != reason.None {
		s.refuse(w, class, why, start)
		return
	}
	if v := s.Revoked.Check(claims.Tenant, claims.Principal, claims.IssuedAt); v != revoke.OK {
		s.refuse(w, class, revocationReason(v), start) // BEFORE the limiter: a revoked person consumes nothing
		return
	}
	if blocked := s.limit(ctx, w, class, claims, start); blocked {
		return
	}
	id, err := s.Minter.Mint(claims.Tenant, claims.Principal, claims.Scopes)
	if err != nil {
		s.Log.Error("identity_mint_failed", "route_class", string(class)) // the error text is the signer's; not logged with request data
		s.refuse(w, class, reason.SigningUnavailable, start)
		return
	}
	w.Header().Set(IdentityHeader, id)
	s.done(w, class, reason.Allowed, routeReason, start)
}

// checkMCP is limit-only mode. The MCP gateway verifies the token itself (the MCP authorization specification requires it), so this
// path never refuses a request for authentication reasons: a missing or invalid token is passed through so the gateway can answer it with
// its own 401 and discovery metadata, which an MCP client depends on. The one thing it does refuse is a revoked person.
func (s *Server) checkMCP(ctx context.Context, w http.ResponseWriter, r *http.Request, class limiter.Class, uri string, start time.Time) {
	token, why := bearer(r)
	if why != reason.None {
		s.done(w, class, reason.Passthrough, why, start)
		return
	}
	claims, why := s.Verifier.Verify(ctx, token, extjwt.Identify)
	if why != reason.None {
		s.done(w, class, reason.Passthrough, why, start)
		return
	}
	if v := s.Revoked.Check(claims.Tenant, claims.Principal, claims.IssuedAt); v != revoke.OK {
		if v == revoke.Unavailable {
			s.refuse(w, class, reason.RevocationUnavailable, start)
			return
		}
		s.refuseMCPRevoked(w, class, uri, start)
		return
	}
	if blocked := s.limit(ctx, w, class, claims, start); blocked {
		return
	}
	s.done(w, class, reason.Allowed, reason.None, start)
}

// limit charges the request to the tenant's and the person's buckets. It reports whether the request was refused (and the response
// written). A store failure serves the request.
func (s *Server) limit(ctx context.Context, w http.ResponseWriter, class limiter.Class, c extjwt.Claims, start time.Time) (blocked bool) {
	lim, limited := s.Table.Limits(class)
	if !limited {
		return false // read, cancel: verified and never limited
	}
	d, err := s.Limiter.Take(ctx, c.Tenant, c.Principal, class, lim)
	if err != nil {
		s.Metrics.StoreError(telemetry.StoreLimiter)
		s.Log.Warn("limiter_store_failed", "route_class", string(class)) // fail open: serve; the counter and the alert are the signal
		return false
	}
	if d.Allowed {
		return false
	}
	secs := int(d.RetryAfter / time.Second)
	if s.Cfg.LimitMode == "observe" {
		s.Log.Info("would_limit", "route_class", string(class), "tenant", c.Tenant, "retry_after_seconds", secs)
		s.Metrics.Request(string(class), string(reason.WouldLimit), "", s.Now().Sub(start))
		return false
	}
	s.Log.Info("limited", "route_class", string(class), "tenant", c.Tenant, "retry_after_seconds", secs)
	s.Metrics.Request(string(class), string(reason.Limited), "", s.Now().Sub(start))
	errenv.Write(w, errenv.RateLimited, map[string]any{errenv.DetailRetryAfterSeconds: secs}, http.Header{"Retry-After": {strconv.Itoa(secs)}})
	return true
}

// --- answers -----------------------------------------------------------------------------------------------------------------------

// done records an answered request that is not a refusal (allowed, or passed through) and sends 200.
func (s *Server) done(w http.ResponseWriter, class limiter.Class, outcome reason.Outcome, why reason.Reason, start time.Time) {
	s.Metrics.Request(string(class), string(outcome), string(why), s.Now().Sub(start))
	w.WriteHeader(http.StatusOK)
}

// refuse sends the refusal for a reason (the status table of contracts/authlimit.md section A) and records it.
func (s *Server) refuse(w http.ResponseWriter, class limiter.Class, why reason.Reason, start time.Time) {
	s.Metrics.Request(string(class), string(reason.Refused), string(why), s.Now().Sub(start))
	switch why {
	case reason.MissingToken:
		errenv.Write(w, errenv.Unauthenticated, nil, http.Header{"WWW-Authenticate": {`Bearer realm="` + Realm + `"`}})
	case reason.BadScope:
		errenv.Write(w, errenv.Forbidden, map[string]any{errenv.DetailRequiredScope: s.Cfg.RequiredScope},
			http.Header{"WWW-Authenticate": {`Bearer realm="` + Realm + `", error="insufficient_scope", scope="` + s.Cfg.RequiredScope + `"`}})
	case reason.KeysUnavailable, reason.RevocationUnavailable, reason.SigningUnavailable:
		errenv.Write(w, errenv.AuthUnavailable, nil, http.Header{"Retry-After": {"30"}})
	default: // every other reason is a token that is not acceptable, or a revoked person
		errenv.Write(w, errenv.Unauthenticated, nil, http.Header{"WWW-Authenticate": {`Bearer realm="` + Realm + `", error="invalid_token"`}})
	}
}

// refuseMCPRevoked is the revoked person's 401 on an MCP route: it carries the discovery URL an MCP client needs to start sign-in again.
func (s *Server) refuseMCPRevoked(w http.ResponseWriter, class limiter.Class, uri string, start time.Time) {
	s.Metrics.Request(string(class), string(reason.Refused), string(reason.Revoked), s.Now().Sub(start))
	challenge := `Bearer error="invalid_token"`
	if path, ok := limiter.CleanPath(uri); ok && s.Cfg.MCPMetadataURLTemplate != "" {
		// `path` is canonical (decoded once, no dot segments, no query), and an MCP route matches an exact literal pattern, so it
		// contains only the characters of that pattern: nothing from the request is reflected into the header unchecked.
		challenge += `, resource_metadata="` + strings.ReplaceAll(s.Cfg.MCPMetadataURLTemplate, "{path}", path) + `"`
	}
	errenv.Write(w, errenv.Unauthenticated, nil, http.Header{"WWW-Authenticate": {challenge}})
}

func revocationReason(v revoke.Verdict) reason.Reason {
	if v == revoke.Unavailable {
		return reason.RevocationUnavailable
	}
	return reason.Revoked
}

// bearer extracts the access token. Exactly one Authorization header with the Bearer scheme is accepted; anything else is "no usable
// token": a second header would let a proxy and the service read different tokens.
func bearer(r *http.Request) (string, reason.Reason) {
	vals := r.Header.Values("Authorization")
	switch len(vals) {
	case 0:
		return "", reason.MissingToken
	case 1:
	default:
		return "", reason.Malformed
	}
	scheme, token, ok := strings.Cut(vals[0], " ")
	if !ok || !strings.EqualFold(scheme, "Bearer") {
		return "", reason.MissingToken
	}
	token = strings.TrimSpace(token)
	if token == "" {
		return "", reason.MissingToken
	}
	return token, reason.None
}

// --- health, readiness, keys -------------------------------------------------------------------------------------------------------

func (s *Server) healthz(w http.ResponseWriter, _ *http.Request) {
	// Liveness depends on nothing external: a replica that cannot reach Casdoor or Redis is not dead, and restarting it would not help.
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// Ready reports whether this replica can decide requests and, if not, the one word why.
func (s *Server) Ready() (ok bool, why string) {
	if _, _, err := s.Keyset.Current(); err != nil {
		return false, "no_signing_key"
	}
	if st, _ := s.Keys.State(); st == extjwt.KeysUnusable {
		return false, "keys_unavailable"
	}
	if !s.Revoked.Usable() {
		return false, "revocation_unavailable"
	}
	return true, ""
}

func (s *Server) readyz(w http.ResponseWriter, _ *http.Request) {
	if ok, why := s.Ready(); !ok {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "not_ready", "reason": why})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) jwks(w http.ResponseWriter, _ *http.Request) {
	body, err := s.Keyset.PublicJWKS()
	if err != nil {
		errenv.Write(w, errenv.Internal, nil, nil)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "max-age=10")
	_, _ = w.Write(body)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// Watch keeps the gauges and the key directory current until ctx ends: readiness and signing-key age every second, and a re-read of the
// key directory every five seconds so a scheduled rotation is picked up without a restart.
func (s *Server) Watch(ctx context.Context) {
	t := time.NewTicker(time.Second)
	defer t.Stop()
	for n := 0; ; n++ {
		ok, _ := s.Ready()
		s.Metrics.SetReady(ok)
		if since, have := s.Keyset.CurrentSince(); have {
			s.Metrics.SetSigningKeyAge(s.Now().Sub(since))
		}
		if n%5 == 4 {
			if err := s.Keyset.Reload(); err != nil {
				s.Log.Warn("keyset_reload_failed", "error", err.Error()) // keeps the keys it has
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

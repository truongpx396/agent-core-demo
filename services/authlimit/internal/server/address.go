package server

import (
	"net/http"
	"net/netip"
	"strconv"
	"strings"
	"time"

	"authlimit/internal/errenv"
	"authlimit/internal/limiter"
	"authlimit/internal/reason"
	"authlimit/internal/telemetry"
)

// The address limits: the one question Caddy asks about a request that carries no token and cannot (a sign-in, a webhook).
//
// There is nothing to verify and nobody to charge, so the request is charged to the network address it came from, and that is all this
// does: no token is read, no identity is minted, no revocation is consulted. It is a protection, not a check, so a store failure serves
// the request (the same policy as the tenant and person buckets). The address is the one thing a client cannot choose: it is Caddy's own
// `{client_ip}`, set on the request to this service, so a header a client sends never reaches here.

// ClientAddressHeader is the header Caddy sets from `{client_ip}` on the request it makes to this service. When Caddy sits behind a
// trusted proxy (Cloudflare), `{client_ip}` is already the address behind it: that is Caddy's `trusted_proxies`, not a setting here.
const ClientAddressHeader = "X-Client-Ip"

// unknownAddress is the one shared bucket for a request whose address could not be read. A proxy that stopped sending the header then
// shows up at once as everyone being limited (and counted under reason.NoAddress), not as nobody being limited.
const unknownAddress = "unknown"

// LimitSignin is GET /v1/limit/signin.
func (s *Server) LimitSignin(w http.ResponseWriter, r *http.Request) { s.limitAddress(w, r, "signin") }

// LimitWebhook is GET /v1/limit/webhook.
func (s *Server) LimitWebhook(w http.ResponseWriter, r *http.Request) {
	s.limitAddress(w, r, "webhook")
}

func (s *Server) limitAddress(w http.ResponseWriter, r *http.Request, name string) {
	start := s.Now()
	label := limiter.Class("address_" + name) // the metric's route_class: bounded, because `name` is one of the handlers' literals above
	bucket, ok := s.Table.AddressLimit(name)
	if !ok {
		// routes.json and openapi.json disagree (a test holds them together), so this is a build that should not have shipped. Refuse
		// loudly: a sign-in that is not limited at all is worse than one that fails where an operator will see it.
		s.Log.Error("address_limit_not_in_table", "limit", name)
		s.Metrics.Request(string(label), string(reason.Refused), string(reason.UnknownLimit), s.Now().Sub(start))
		errenv.Write(w, errenv.Internal, nil, nil)
		return
	}
	address, known := clientAddress(r)
	why := reason.None
	if !known {
		why = reason.NoAddress
	}

	d, err := s.Limiter.TakeAddress(r.Context(), name, address, bucket)
	if err != nil {
		s.Metrics.StoreError(telemetry.StoreLimiter)
		s.Log.Warn("limiter_store_failed", "route_class", string(label)) // fail open: the counter and the alert are the signal
		s.done(w, label, reason.Allowed, why, start)
		return
	}
	if d.Allowed {
		s.done(w, label, reason.Allowed, why, start)
		return
	}

	secs := int(d.RetryAfter / time.Second)
	if s.Cfg.LimitMode == "observe" {
		s.Log.Info("would_limit", "route_class", string(label), "address", address, "retry_after_seconds", secs)
		s.Metrics.Request(string(label), string(reason.WouldLimit), string(why), s.Now().Sub(start))
		w.WriteHeader(http.StatusOK)
		return
	}
	s.Log.Info("limited", "route_class", string(label), "address", address, "retry_after_seconds", secs)
	s.Metrics.Request(string(label), string(reason.Limited), string(why), s.Now().Sub(start))
	errenv.Write(w, errenv.RateLimited, map[string]any{errenv.DetailRetryAfterSeconds: secs}, http.Header{"Retry-After": {strconv.Itoa(secs)}})
}

// clientAddress is the bucket key for a request: the address in the single X-Client-Ip header, in the form that is one client.
//
//   - An IPv4 address is its own key. An IPv4-mapped IPv6 address is the IPv4 address it carries.
//   - An IPv6 address is keyed by its /64. One customer is given a whole /64, so a bucket per address would let one client take a fresh
//     bucket for every request.
//
// known is false for a missing, repeated or unparseable header, which is charged to unknownAddress.
func clientAddress(r *http.Request) (key string, known bool) {
	vals := r.Header.Values(ClientAddressHeader)
	if len(vals) != 1 {
		return unknownAddress, false
	}
	a, err := netip.ParseAddr(strings.TrimSpace(vals[0]))
	if err != nil {
		return unknownAddress, false
	}
	a = a.Unmap().WithZone("")
	if a.Is4() {
		return a.String(), true
	}
	p, err := a.Prefix(64)
	if err != nil {
		return unknownAddress, false
	}
	return p.String(), true
}

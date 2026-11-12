package server

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"authlimit/internal/limiter"
)

// limit calls an address limit the way Caddy's forward_auth does: a GET whose X-Client-Ip is Caddy's own {client_ip}. An empty address
// sends no header.
func (b *bed) limit(path, address string, mutate ...func(*http.Request)) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, path, nil)
	if address != "" {
		req.Header.Set(ClientAddressHeader, address)
	}
	for _, m := range mutate {
		m(req)
	}
	rec := httptest.NewRecorder()
	b.h.ServeHTTP(rec, req)
	return rec
}

func TestASigninRequestIsChargedToTheAddressCaddySaidAndNothingIsVerified(t *testing.T) {
	b := newBed(t)
	// A bearer header that would be refused on /v1/check, and a client-sent identity: neither is read.
	rec := b.limit("/v1/limit/signin", "203.0.113.7", func(r *http.Request) {
		r.Header.Set("Authorization", "Bearer not-a-token")
		r.Header.Set(IdentityHeader, "forged")
	})
	if rec.Code != 200 || rec.Header().Get(IdentityHeader) != "" || rec.Body.Len() != 0 {
		t.Fatalf("%d identity %q body %q: an address limit answers a bare 200", rec.Code, rec.Header().Get(IdentityHeader), rec.Body.String())
	}
	want := addressCall{name: "signin", address: "203.0.113.7", bucket: limiter.Bucket{Capacity: 60, PerMinute: 120}}
	if len(b.lim.addressCalls) != 1 || b.lim.addressCalls[0] != want {
		t.Fatalf("charged %+v, want exactly %+v", b.lim.addressCalls, want)
	}
	if b.lim.count() != 0 {
		t.Error("a tenant or person bucket was charged: there is no tenant or person here")
	}
	if b.counted(t, "address_signin", "allowed", "") != 1 {
		t.Error("not counted")
	}
}

func TestEachAddressLimitUsesItsOwnBucketShape(t *testing.T) {
	b := newBed(t)
	b.limit("/v1/limit/webhook", "203.0.113.7")
	want := addressCall{name: "webhook", address: "203.0.113.7", bucket: limiter.Bucket{Capacity: 100, PerMinute: 600}}
	if len(b.lim.addressCalls) != 1 || b.lim.addressCalls[0] != want {
		t.Fatalf("charged %+v, want %+v", b.lim.addressCalls, want)
	}
}

func TestTheBucketKeyIsOneClientNotOneAddress(t *testing.T) {
	for _, tc := range []struct{ header, key string }{
		{"203.0.113.7", "203.0.113.7"},
		{"  203.0.113.7 ", "203.0.113.7"},
		{"::ffff:198.51.100.9", "198.51.100.9"}, // an IPv4 client seen through a dual-stack socket is the same client
		{"2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"},
		{"2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"}, // the same /64: one customer, one bucket
		{"fe80::1%eth0", "fe80::/64"},                             // a zone is the interface, not the client
	} {
		b := newBed(t)
		b.limit("/v1/limit/signin", tc.header)
		if len(b.lim.addressCalls) != 1 || b.lim.addressCalls[0].address != tc.key {
			t.Errorf("X-Client-Ip %q was charged to %+v, want the key %q", tc.header, b.lim.addressCalls, tc.key)
		}
	}
}

func TestAnAddressThatCannotBeReadIsOneSharedBucketAndIsCounted(t *testing.T) {
	// Not "no limit": a Caddy that lost the header would otherwise switch the protection off without anything looking wrong. One shared
	// bucket makes it loud instead (everyone is limited together) and the reason says why.
	for name, mutate := range map[string]func(*http.Request){
		"missing":     func(*http.Request) {},
		"garbage":     func(r *http.Request) { r.Header.Set(ClientAddressHeader, "not-an-address") },
		"a list":      func(r *http.Request) { r.Header.Set(ClientAddressHeader, "203.0.113.7, 198.51.100.9") },
		"with a port": func(r *http.Request) { r.Header.Set(ClientAddressHeader, "203.0.113.7:443") },
		"repeated": func(r *http.Request) {
			r.Header.Add(ClientAddressHeader, "203.0.113.7")
			r.Header.Add(ClientAddressHeader, "198.51.100.9")
		},
		"empty string": func(r *http.Request) { r.Header.Set(ClientAddressHeader, "") },
	} {
		t.Run(name, func(t *testing.T) {
			b := newBed(t)
			rec := b.limit("/v1/limit/signin", "", mutate)
			if rec.Code != 200 {
				t.Fatalf("%d: an unreadable address is charged, not refused", rec.Code)
			}
			if len(b.lim.addressCalls) != 1 || b.lim.addressCalls[0].address != "unknown" {
				t.Fatalf("charged %+v, want the shared %q bucket", b.lim.addressCalls, "unknown")
			}
			if b.counted(t, "address_signin", "allowed", "no_address") != 1 {
				t.Error("the missing address was not counted under its reason")
			}
		})
	}
}

func TestAnEmptyAddressBucketIsA429WithRetryAfterAndNoIdentity(t *testing.T) {
	b := newBed(t)
	b.lim.decision = limiter.Decision{Allowed: false, RetryAfter: 4 * time.Second}
	rec := b.limit("/v1/limit/signin", "203.0.113.7")
	e := decode(t, rec)
	if rec.Code != 429 || rec.Header().Get("Retry-After") != "4" || e.Code != "rate_limited" || e.Details["retry_after_seconds"] != float64(4) {
		t.Fatalf("%d retry-after %q %+v", rec.Code, rec.Header().Get("Retry-After"), e)
	}
	if rec.Header().Get(IdentityHeader) != "" {
		t.Fatal("a limited request must not be handed an identity")
	}
	if b.counted(t, "address_signin", "limited", "") != 1 {
		t.Error("not counted")
	}
}

func TestObserveModeServesAnAddressLimitAndCountsTheWouldBeRefusal(t *testing.T) {
	b := newBed(t)
	b.srv.Cfg.LimitMode = "observe"
	b.lim.decision = limiter.Decision{Allowed: false, RetryAfter: 4 * time.Second}
	if rec := b.limit("/v1/limit/webhook", "203.0.113.7"); rec.Code != 200 {
		t.Fatalf("observe mode must serve: %d", rec.Code)
	}
	if b.counted(t, "address_webhook", "would_limit", "") != 1 || b.counted(t, "address_webhook", "limited", "") != 0 {
		t.Error("observe mode counts would_limit and nothing else")
	}
}

func TestAStoreFailureOnAnAddressLimitFailsOpen(t *testing.T) {
	b := newBed(t)
	b.lim.err = errors.New("redis: connection refused")
	if rec := b.limit("/v1/limit/signin", "203.0.113.7"); rec.Code != 200 {
		t.Fatalf("a limiter outage must not stop people signing in: %d", rec.Code)
	}
	if b.counted(t, "address_signin", "allowed", "") != 1 {
		t.Error("the request must still be counted as allowed")
	}
}

func TestAnAddressLimitTheTableLacksIsRefusedNotServedUnlimited(t *testing.T) {
	b := newBed(t)
	table, err := limiter.ParseTable([]byte(`{"fallback":"turn","limits":{
		"turn":{"tenant":{"capacity":1,"per_minute":1},"person":{"capacity":1,"per_minute":1}},
		"upload":{"tenant":{"capacity":1,"per_minute":1},"person":{"capacity":1,"per_minute":1}},
		"mcp":{"tenant":{"capacity":1,"per_minute":1},"person":{"capacity":1,"per_minute":1}}},"routes":[]}`))
	if err != nil {
		t.Fatal(err)
	}
	b.srv.Table = table
	rec := b.limit("/v1/limit/signin", "203.0.113.7")
	if rec.Code != 500 || decode(t, rec).Code != "internal" {
		t.Fatalf("%d %s: a sign-in with no limit defined must fail where an operator sees it", rec.Code, rec.Body.String())
	}
	if len(b.lim.addressCalls) != 0 || b.counted(t, "address_signin", "refused", "unknown_limit") != 1 {
		t.Error("nothing is charged, and the refusal is counted under its reason")
	}
}

func TestOnlyGETIsAllowedOnAnAddressLimit(t *testing.T) {
	b := newBed(t)
	req := httptest.NewRequest(http.MethodPost, "/v1/limit/signin", nil)
	rec := httptest.NewRecorder()
	b.h.ServeHTTP(rec, req)
	if rec.Code == 200 || len(b.lim.addressCalls) != 0 {
		t.Fatalf("a POST was served (%d) or charged", rec.Code)
	}
}

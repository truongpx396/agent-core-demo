package server

import (
	"context"
	"crypto/ed25519"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"go.opentelemetry.io/otel/attribute"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"authlimit"
	"authlimit/internal/config"
	"authlimit/internal/extjwt"
	"authlimit/internal/identity"
	"authlimit/internal/limiter"
	"authlimit/internal/reason"
	"authlimit/internal/revoke"
	"authlimit/internal/telemetry"
)

// --- fakes ---------------------------------------------------------------------------------------------------------------------

type fakeLimiter struct {
	mu       sync.Mutex
	calls    []call
	decision limiter.Decision
	err      error
}

type call struct {
	tenant, person string
	class          limiter.Class
}

func (f *fakeLimiter) Take(_ context.Context, tenant, person string, class limiter.Class, _ limiter.Limits) (limiter.Decision, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, call{tenant, person, class})
	return f.decision, f.err
}

func (f *fakeLimiter) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.calls)
}

type fakeRevoked struct {
	verdict revoke.Verdict
	usable  bool
	lastIAT int64
	checks  int
}

func (f *fakeRevoked) Check(_, _ string, iat int64) revoke.Verdict {
	f.checks++
	f.lastIAT = iat
	return f.verdict
}
func (f *fakeRevoked) Usable() bool { return f.usable }

type fakeKeys struct{ state extjwt.KeyState }

func (f *fakeKeys) State() (extjwt.KeyState, time.Duration) { return f.state, 0 }

// --- the bed ---------------------------------------------------------------------------------------------------------------------

type vectorFile struct {
	Now     int64 `json:"now"`
	Vectors []struct {
		Name   string `json:"name"`
		Token  string `json:"token"`
		Expect struct {
			Chat struct {
				Result string `json:"result"`
				Reason string `json:"reason"`
			} `json:"chat"`
		} `json:"expect"`
	} `json:"vectors"`
	Config struct {
		Issuer         string `json:"issuer"`
		Audience       string `json:"audience"`
		RequiredScope  string `json:"required_scope"`
		TenantClaim    string `json:"tenant_claim"`
		PrincipalClaim string `json:"principal_claim"`
		LeewaySeconds  int    `json:"leeway_seconds"`
		MaxLifetime    int    `json:"max_token_lifetime_seconds"`
	} `json:"config"`
}

func (v vectorFile) token(t *testing.T, name string) string {
	t.Helper()
	for _, x := range v.Vectors {
		if x.Name == name {
			return x.Token
		}
	}
	t.Fatalf("no vector %q", name)
	return ""
}

type bed struct {
	srv    *Server
	h      http.Handler
	lim    *fakeLimiter
	rev    *fakeRevoked
	keys   *fakeKeys
	vf     vectorFile
	ks     *identity.Keyset
	reader *sdkmetric.ManualReader
	cfg    config.Config
}

func newBed(t *testing.T) *bed {
	t.Helper()
	dir := filepath.Join("..", "..", "..", "..", "testdata", "auth-vectors")
	raw, err := os.ReadFile(filepath.Join(dir, "vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var vf vectorFile
	if err := json.Unmarshal(raw, &vf); err != nil {
		t.Fatal(err)
	}
	jwks, err := os.ReadFile(filepath.Join(dir, "jwks.json"))
	if err != nil {
		t.Fatal(err)
	}
	pubs, err := extjwt.ParseJWKS(jwks)
	if err != nil {
		t.Fatal(err)
	}
	now := func() time.Time { return time.Unix(vf.Now, 0) }

	keyDir := filepath.Join(t.TempDir(), "keys")
	if _, err := identity.InitKeyset(keyDir, now()); err != nil {
		t.Fatal(err)
	}
	ks, err := identity.LoadKeyset(keyDir, false, now)
	if err != nil {
		t.Fatal(err)
	}
	table, err := limiter.ParseTable(authlimit.RoutesJSON)
	if err != nil {
		t.Fatal(err)
	}
	cfg := config.Config{
		Audience: vf.Config.Audience, RequiredScope: vf.Config.RequiredScope, LimitMode: "enforce",
		JWKSFetchTimeout:       5 * time.Second,
		MCPMetadataURLTemplate: "https://mcp.localhost:8443/.well-known/oauth-protected-resource{path}",
	}
	reader := sdkmetric.NewManualReader()
	metrics, err := telemetry.New(sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader)).Meter("test"))
	if err != nil {
		t.Fatal(err)
	}
	b := &bed{
		lim: &fakeLimiter{decision: limiter.Decision{Allowed: true}}, rev: &fakeRevoked{usable: true}, keys: &fakeKeys{state: extjwt.KeysFresh},
		vf: vf, ks: ks, reader: reader, cfg: cfg,
	}
	b.srv = New(Deps{
		Cfg: cfg, Table: table,
		Verifier: extjwt.NewVerifier(extjwt.Settings{
			Issuer: vf.Config.Issuer, Audience: vf.Config.Audience, RequiredScope: vf.Config.RequiredScope, TenantClaim: vf.Config.TenantClaim,
			PrincipalClaim: vf.Config.PrincipalClaim, MaxLifetime: time.Duration(vf.Config.MaxLifetime) * time.Second,
			Leeway: time.Duration(vf.Config.LeewaySeconds) * time.Second,
		}, extjwt.StaticKeys(pubs), now),
		Keys: b.keys, Keyset: ks, Minter: identity.NewMinter(ks, 30*time.Second, now),
		Limiter: b.lim, Revoked: b.rev, Metrics: metrics, Now: now,
	})
	b.h = b.srv.Handler()
	return b
}

// check calls /v1/check the way Caddy's forward_auth does: a GET carrying the original method and URI in X-Forwarded-*.
func (b *bed) check(method, uri, token string, mutate ...func(*http.Request)) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, "/v1/check", nil)
	req.Header.Set("X-Forwarded-Method", method)
	req.Header.Set("X-Forwarded-Uri", uri)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	for _, m := range mutate {
		m(req)
	}
	rec := httptest.NewRecorder()
	b.h.ServeHTTP(rec, req)
	return rec
}

type envelope struct {
	Code    string         `json:"code"`
	Message string         `json:"message"`
	Details map[string]any `json:"details"`
}

func decode(t *testing.T, rec *httptest.ResponseRecorder) envelope {
	t.Helper()
	var e envelope
	if err := json.Unmarshal(rec.Body.Bytes(), &e); err != nil {
		t.Fatalf("body is not an envelope: %q (%v)", rec.Body.String(), err)
	}
	return e
}

// counted returns the request counter for one (class, outcome, reason).
func (b *bed) counted(t *testing.T, class, outcome, why string) int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := b.reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != telemetry.NameRequests {
				continue
			}
			for _, dp := range m.Data.(metricdata.Sum[int64]).DataPoints {
				get := func(k string) string { v, _ := dp.Attributes.Value(attribute.Key(k)); return v.AsString() }
				if get("route_class") == class && get("outcome") == outcome && get("reason") == why {
					return dp.Value
				}
			}
		}
	}
	return 0
}

// --- public routes and the identity --------------------------------------------------------------------------------------------------

func TestPublicRoutesAreNotChecked(t *testing.T) {
	b := newBed(t)
	for _, uri := range []string{"/", "/health", "/health/ready", "/static/auth.js", "/billing/webhooks/polar", "/.well-known/oauth-protected-resource/ecorp/mcp"} {
		rec := b.check("GET", uri, "") // no token at all
		if rec.Code != 200 || rec.Header().Get(IdentityHeader) != "" {
			t.Errorf("%s: status %d identity %q", uri, rec.Code, rec.Header().Get(IdentityHeader))
		}
	}
	if b.lim.count() != 0 {
		t.Error("a public route must not touch the limiter")
	}
}

func TestAValidTokenGetsAnIdentityForThatTenantAndPerson(t *testing.T) {
	b := newBed(t)
	rec := b.check("POST", "/chat/stream/queued", b.vf.token(t, "valid_chat"))
	if rec.Code != 200 {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	id := rec.Header().Get(IdentityHeader)
	if id == "" {
		t.Fatal("no identity header")
	}
	// Verify it the way the API will: against the PUBLISHED keys.
	var pub ed25519.PublicKey
	for _, p := range b.ks.Published() {
		pub = p.Key
	}
	mc := jwt.MapClaims{}
	if _, err := jwt.NewParser(jwt.WithValidMethods([]string{"EdDSA"}), jwt.WithIssuer(identity.Issuer), jwt.WithAudience(identity.Audience),
		jwt.WithTimeFunc(func() time.Time { return time.Unix(b.vf.Now, 0) })).ParseWithClaims(id, mc, func(*jwt.Token) (any, error) { return pub, nil }); err != nil {
		t.Fatalf("the identity does not verify: %v", err)
	}
	if mc["tenant"] != "ecorp" || mc["sub"] != "user-1" || !strings.Contains(mc["scope"].(string), "api:chat") {
		t.Fatalf("claims = %v", mc)
	}
	if got := b.lim.calls; len(got) != 1 || got[0] != (call{"ecorp", "user-1", limiter.Turn}) {
		t.Fatalf("the limiter saw %+v; it must see the VERIFIED tenant and person, and the class", got)
	}
	if b.counted(t, "turn", "allowed", "") != 1 {
		t.Error("the allowed request was not counted")
	}
}

func TestReadAndCancelAreVerifiedAndNeverLimited(t *testing.T) {
	b := newBed(t)
	b.lim.decision = limiter.Decision{Allowed: false, RetryAfter: 5 * time.Second} // would refuse anything it is asked about
	for _, r := range [][2]string{{"POST", "/chat/cancel"}, {"GET", "/chat/sessions"}, {"GET", "/usage"}, {"GET", "/chat/sessions/t1/messages"}} {
		rec := b.check(r[0], r[1], b.vf.token(t, "valid_chat"))
		if rec.Code != 200 || rec.Header().Get(IdentityHeader) == "" {
			t.Errorf("%s %s: status %d (a throttled tenant must still be able to cancel and read)", r[0], r[1], rec.Code)
		}
	}
	if b.lim.count() != 0 {
		t.Error("read and cancel routes must never reach the limiter")
	}
	// ...but they still need a token.
	if rec := b.check("POST", "/chat/cancel", ""); rec.Code != 401 {
		t.Errorf("cancel without a token: %d", rec.Code)
	}
}

func TestAnUnlistedRouteIsVerifiedAndLimitedLikeATurn(t *testing.T) {
	b := newBed(t)
	rec := b.check("GET", "/brand-new-endpoint", b.vf.token(t, "valid_chat"))
	if rec.Code != 200 || len(b.lim.calls) != 1 || b.lim.calls[0].class != limiter.Turn {
		t.Fatalf("status %d calls %+v", rec.Code, b.lim.calls)
	}
	if b.counted(t, "turn", "allowed", "unlisted_route") != 1 {
		t.Error("a route that drifted out of the table must be visible under its own reason")
	}
	if rec := b.check("GET", "/brand-new-endpoint", ""); rec.Code != 401 {
		t.Errorf("unlisted without a token: %d", rec.Code)
	}
}

func TestForgedForwardedHeadersCannotMakeARoutePublic(t *testing.T) {
	// Caddy sets X-Forwarded-Method/Uri; if a client's own copy rides along (a second value), the check must not trust either: the
	// request is classified as an unclean path, which is the verified-and-limited fallback, never public.
	b := newBed(t)
	dup := func(r *http.Request) { r.Header.Add("X-Forwarded-Uri", "/health") }
	if rec := b.check("POST", "/chat/stream/queued", "", dup); rec.Code != 401 {
		t.Errorf("a duplicated X-Forwarded-Uri without a token: %d, want 401", rec.Code)
	}
	dupMethod := func(r *http.Request) { r.Header.Add("X-Forwarded-Method", "OPTIONS") }
	if rec := b.check("POST", "/chat/stream/queued", "", dupMethod); rec.Code != 401 {
		t.Errorf("a duplicated X-Forwarded-Method without a token: %d, want 401 (OPTIONS is public)", rec.Code)
	}
	missing := func(r *http.Request) { r.Header.Del("X-Forwarded-Uri") }
	if rec := b.check("GET", "/health", "", missing); rec.Code != 401 {
		t.Errorf("no X-Forwarded-Uri at all: %d, want 401", rec.Code)
	}
}

func TestAForgedForwardedValueThatArrivesFirstCannotDecideTheClassEither(t *testing.T) {
	// Found by the mutation run: the test above appends the forged value AFTER the real one, so a server that simply read the first value
	// would still have been right. A forged value can equally arrive first (a proxy that appends instead of replacing, a client header
	// that precedes the proxy's). Either order must be refused as ambiguous, not resolved by picking one.
	b := newBed(t)
	forgedFirst := func(r *http.Request) {
		r.Header["X-Forwarded-Uri"] = []string{"/health", "/chat/stream/queued"}
	}
	if rec := b.check("POST", "/chat/stream/queued", "", forgedFirst); rec.Code != 401 {
		t.Errorf("a forged X-Forwarded-Uri first: %d, want 401", rec.Code)
	}
	forgedMethodFirst := func(r *http.Request) {
		r.Header["X-Forwarded-Method"] = []string{"OPTIONS", "POST"}
	}
	if rec := b.check("POST", "/chat/stream/queued", "", forgedMethodFirst); rec.Code != 401 {
		t.Errorf("a forged X-Forwarded-Method first: %d, want 401", rec.Code)
	}
}

func TestAPathTrickIsNeverPublic(t *testing.T) {
	b := newBed(t)
	for _, uri := range []string{"/static/../chat/stream/queued", "/static/%2e%2e/chat/resume", "/health/..%2fchat/resume"} {
		if rec := b.check("POST", uri, ""); rec.Code != 401 {
			t.Errorf("%s: %d, want 401", uri, rec.Code)
		}
	}
}

// --- refusals ------------------------------------------------------------------------------------------------------------------------

func TestEveryRefusedVectorGetsTheDocumentedAnswerAndNothingElse(t *testing.T) {
	b := newBed(t)
	statusFor := func(why string) (int, string) {
		switch reason.Reason(why) {
		case reason.BadScope:
			return 403, "forbidden"
		case reason.KeysUnavailable:
			return 503, "auth_unavailable"
		default:
			return 401, "unauthenticated"
		}
	}
	refused := 0
	for _, v := range b.vf.Vectors {
		if v.Expect.Chat.Result != "refused" {
			continue
		}
		refused++
		rec := b.check("POST", "/chat/stream/queued", v.Token)
		wantStatus, wantCode := statusFor(v.Expect.Chat.Reason)
		if rec.Code != wantStatus {
			t.Errorf("%s: status %d, want %d", v.Name, rec.Code, wantStatus)
			continue
		}
		if e := decode(t, rec); e.Code != wantCode || e.Message == "" {
			t.Errorf("%s: envelope %+v, want code %s", v.Name, e, wantCode)
		}
		if rec.Header().Get(IdentityHeader) != "" {
			t.Errorf("%s: a refusal carried an identity", v.Name)
		}
		if rec.Header().Get("WWW-Authenticate") == "" {
			t.Errorf("%s: no WWW-Authenticate", v.Name)
		}
		// The response must contain no part of the token, however it is split.
		for _, part := range strings.Split(v.Token, ".") {
			if len(part) > 12 && strings.Contains(rec.Body.String()+rec.Header().Get("WWW-Authenticate"), part[:12]) {
				t.Errorf("%s: the response echoes part of the token", v.Name)
			}
		}
	}
	if refused < 20 {
		t.Fatalf("only %d refused vectors exercised", refused)
	}
	if b.lim.count() != 0 {
		t.Error("a request refused for its token must never reach the limiter")
	}
}

func TestScopeRefusalNamesTheScope(t *testing.T) {
	b := newBed(t)
	rec := b.check("POST", "/chat/stream/queued", b.vf.token(t, "missing_scope"))
	e := decode(t, rec)
	if rec.Code != 403 || e.Code != "forbidden" || e.Details["required_scope"] != "api:chat" {
		t.Fatalf("%d %+v", rec.Code, e)
	}
	if !strings.Contains(rec.Header().Get("WWW-Authenticate"), `error="insufficient_scope"`) {
		t.Fatalf("WWW-Authenticate = %q", rec.Header().Get("WWW-Authenticate"))
	}
}

func TestMissingAndUnusableAuthorizationHeaders(t *testing.T) {
	b := newBed(t)
	rec := b.check("POST", "/chat/stream/queued", "")
	if rec.Code != 401 || rec.Header().Get("WWW-Authenticate") != `Bearer realm="agent-core-api"` {
		t.Fatalf("no token: %d %q (RFC 6750: no error parameter when no credentials were sent)", rec.Code, rec.Header().Get("WWW-Authenticate"))
	}
	token := b.vf.token(t, "valid_chat")
	cases := map[string]func(*http.Request){
		"another scheme":               func(r *http.Request) { r.Header.Set("Authorization", "Basic dXNlcjpwYXNz") },
		"bearer with nothing":          func(r *http.Request) { r.Header.Set("Authorization", "Bearer ") },
		"lower-case scheme (accepted)": func(r *http.Request) { r.Header.Set("Authorization", "bearer "+token) },
		"two headers":                  func(r *http.Request) { r.Header.Add("Authorization", "Bearer "+token) },
	}
	want := map[string]int{"another scheme": 401, "bearer with nothing": 401, "lower-case scheme (accepted)": 200, "two headers": 401}
	for name, mut := range cases {
		rec := b.check("POST", "/chat/stream/queued", token, mut)
		if rec.Code != want[name] {
			t.Errorf("%s: %d, want %d", name, rec.Code, want[name])
		}
	}
}

func TestKeysUnavailableIsA503NotAnAnswerAboutTheToken(t *testing.T) {
	b := newBed(t)
	// Replace the verifier's key source with one that is down.
	b.srv.Verifier = extjwt.NewVerifier(extjwt.Settings{Issuer: b.vf.Config.Issuer, Audience: b.vf.Config.Audience, RequiredScope: "api:chat", TenantClaim: "owner",
		PrincipalClaim: "sub", MaxLifetime: 168 * time.Hour, Leeway: 10 * time.Second}, downKeys{}, func() time.Time { return time.Unix(b.vf.Now, 0) })
	rec := b.check("POST", "/chat/stream/queued", b.vf.token(t, "valid_chat"))
	if rec.Code != 503 || decode(t, rec).Code != "auth_unavailable" || rec.Header().Get("Retry-After") != "30" {
		t.Fatalf("%d %s retry-after %q", rec.Code, rec.Body.String(), rec.Header().Get("Retry-After"))
	}
	if b.counted(t, "turn", "refused", "keys_unavailable") != 1 {
		t.Error("the refusal reason is the alert's signal and must be counted")
	}
}

type downKeys struct{}

func (downKeys) Key(context.Context, string) (*rsa.PublicKey, error) {
	return nil, extjwt.ErrKeysUnavailable
}

// --- revocation comes before the limiter ----------------------------------------------------------------------------------------------

func TestARevokedPersonIsRefusedAndConsumesNoBucket(t *testing.T) {
	b := newBed(t)
	b.rev.verdict = revoke.Revoked
	rec := b.check("POST", "/chat/stream/queued", b.vf.token(t, "valid_chat"))
	if rec.Code != 401 || decode(t, rec).Code != "unauthenticated" {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
	if b.lim.count() != 0 {
		t.Fatal("a revoked token reached the limiter: it could drain its tenant's bucket")
	}
	if b.rev.lastIAT == 0 {
		t.Error("revocation was not given the token's issue time")
	}
	if b.counted(t, "turn", "refused", "revoked") != 1 {
		t.Error("revoked refusals must be counted")
	}
}

func TestAnUntrustworthyRevocationCopyIsA503(t *testing.T) {
	b := newBed(t)
	b.rev.verdict = revoke.Unavailable
	rec := b.check("POST", "/chat/stream/queued", b.vf.token(t, "valid_chat"))
	if rec.Code != 503 || rec.Header().Get("Retry-After") == "" || b.lim.count() != 0 {
		t.Fatalf("%d retry-after %q limiter calls %d", rec.Code, rec.Header().Get("Retry-After"), b.lim.count())
	}
	if b.counted(t, "turn", "refused", "revocation_unavailable") != 1 {
		t.Error("not counted")
	}
}

// --- the limiter -----------------------------------------------------------------------------------------------------------------

func TestAnEmptyBucketIsA429WithRetryAfterAndNoIdentity(t *testing.T) {
	b := newBed(t)
	b.lim.decision = limiter.Decision{Allowed: false, RetryAfter: 6 * time.Second}
	rec := b.check("POST", "/chat/stream/queued", b.vf.token(t, "valid_chat"))
	e := decode(t, rec)
	if rec.Code != 429 || rec.Header().Get("Retry-After") != "6" || e.Code != "rate_limited" || e.Details["retry_after_seconds"] != float64(6) {
		t.Fatalf("%d retry-after %q %+v", rec.Code, rec.Header().Get("Retry-After"), e)
	}
	if rec.Header().Get(IdentityHeader) != "" {
		t.Fatal("a limited request must not be handed an identity")
	}
	if b.counted(t, "turn", "limited", "") != 1 {
		t.Error("not counted")
	}
}

func TestObserveModeServesAndCountsTheWouldBeRefusal(t *testing.T) {
	b := newBed(t)
	b.srv.Cfg.LimitMode = "observe"
	b.lim.decision = limiter.Decision{Allowed: false, RetryAfter: 6 * time.Second}
	rec := b.check("POST", "/chat/stream/queued", b.vf.token(t, "valid_chat"))
	if rec.Code != 200 || rec.Header().Get(IdentityHeader) == "" {
		t.Fatalf("observe mode must serve: %d", rec.Code)
	}
	if b.counted(t, "turn", "would_limit", "") != 1 || b.counted(t, "turn", "limited", "") != 0 {
		t.Error("observe mode counts would_limit and nothing else")
	}
}

func TestAStoreFailureFailsOpen(t *testing.T) {
	b := newBed(t)
	b.lim.err = errors.New("redis: connection refused")
	rec := b.check("POST", "/chat/stream/queued", b.vf.token(t, "valid_chat"))
	if rec.Code != 200 || rec.Header().Get(IdentityHeader) == "" {
		t.Fatalf("a limiter outage must not become an API outage: %d", rec.Code)
	}
	if b.counted(t, "turn", "allowed", "") != 1 {
		t.Error("the request must still be counted as allowed")
	}
}

// --- MCP limit-only mode -------------------------------------------------------------------------------------------------------------

func TestMCPWithoutAnyTokenIsPassedThroughForTheGatewayToAnswer(t *testing.T) {
	b := newBed(t)
	rec := b.check("POST", "/ecorp/mcp", "")
	if rec.Code != 200 || rec.Header().Get(IdentityHeader) != "" || b.lim.count() != 0 {
		t.Fatalf("%d identity %q limiter calls %d: the MCP gateway must be the one to answer a missing token, with its own metadata",
			rec.Code, rec.Header().Get(IdentityHeader), b.lim.count())
	}
	if b.counted(t, "mcp", "passthrough", "missing_token") != 1 {
		t.Error("passthroughs must be counted")
	}
}

func TestMCPWithAnInvalidTokenIsPassedThrough(t *testing.T) {
	b := newBed(t)
	for _, name := range []string{"expired", "wrong_issuer", "tampered_payload", "alg_none", "not_a_jwt"} {
		rec := b.check("POST", "/ecorp/mcp", b.vf.token(t, name))
		if rec.Code != 200 || b.lim.count() != 0 {
			t.Errorf("%s: %d limiter calls %d", name, rec.Code, b.lim.count())
		}
	}
}

func TestMCPWithAValidTokenIsLimitedButGetsNoIdentity(t *testing.T) {
	b := newBed(t)
	rec := b.check("POST", "/ecorp/mcp", b.vf.token(t, "valid_mcp"))
	if rec.Code != 200 || rec.Header().Get(IdentityHeader) != "" {
		t.Fatalf("%d identity %q: the MCP gateway verifies the external token itself; there is no identity for it", rec.Code, rec.Header().Get(IdentityHeader))
	}
	if got := b.lim.calls; len(got) != 1 || got[0] != (call{"ecorp", "user-1", limiter.MCP}) {
		t.Fatalf("limiter calls %+v", got)
	}
	b.lim.decision = limiter.Decision{Allowed: false, RetryAfter: 2 * time.Second}
	if rec := b.check("POST", "/ecorp/mcp", b.vf.token(t, "valid_mcp")); rec.Code != 429 {
		t.Fatalf("an empty bucket on an MCP route: %d", rec.Code)
	}
}

func TestMCPAcceptsATokenForTheChatAPIToo(t *testing.T) {
	// Limit-only mode does not check the audience: the MCP gateway decides that. It only needs to know who is calling.
	b := newBed(t)
	if rec := b.check("POST", "/ops/mcp", b.vf.token(t, "valid_chat")); rec.Code != 200 || b.lim.count() != 1 {
		t.Fatalf("%d calls %d", rec.Code, b.lim.count())
	}
}

func TestMCPTokenWithoutAnIssueTimeIsRevokedByAnyRevocationOfThePerson(t *testing.T) {
	b := newBed(t)
	b.check("POST", "/ecorp/mcp", b.vf.token(t, "valid_mcp_without_issue_time"))
	if b.rev.checks != 1 || b.rev.lastIAT != 0 {
		t.Fatalf("revocation saw iat %d after %d checks; a token without an issue time must be checked as iat 0", b.rev.lastIAT, b.rev.checks)
	}
	if b.lim.count() != 1 {
		t.Error("and it must still be limited: it is a token the MCP gateway accepts")
	}
}

func TestMCPRevokedPersonGetsTheDiscoveryBearing401(t *testing.T) {
	b := newBed(t)
	b.rev.verdict = revoke.Revoked
	rec := b.check("POST", "/ecorp/mcp?x=1", b.vf.token(t, "valid_mcp"))
	want := `Bearer error="invalid_token", resource_metadata="https://mcp.localhost:8443/.well-known/oauth-protected-resource/ecorp/mcp"`
	if rec.Code != 401 || rec.Header().Get("WWW-Authenticate") != want {
		t.Fatalf("%d %q\nwant %q", rec.Code, rec.Header().Get("WWW-Authenticate"), want)
	}
	if b.lim.count() != 0 {
		t.Error("a revoked person must not reach the limiter on an MCP route either")
	}
}

func TestMCPRevocationUnavailableIs503(t *testing.T) {
	b := newBed(t)
	b.rev.verdict = revoke.Unavailable
	if rec := b.check("POST", "/ecorp/mcp", b.vf.token(t, "valid_mcp")); rec.Code != 503 {
		t.Fatalf("%d", rec.Code)
	}
}

// --- health, readiness, keys -------------------------------------------------------------------------------------------------------------

func get(h http.Handler, path string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
	return rec
}

func TestLivenessNeverDependsOnTheOutsideWorld(t *testing.T) {
	b := newBed(t)
	b.keys.state = extjwt.KeysUnusable
	b.rev.usable = false
	if rec := get(b.h, "/healthz"); rec.Code != 200 {
		t.Fatalf("a replica that cannot reach Casdoor or Redis is not dead, and restarting it would not help: %d", rec.Code)
	}
}

func TestReadinessNamesWhatIsMissing(t *testing.T) {
	b := newBed(t)
	if rec := get(b.h, "/readyz"); rec.Code != 200 {
		t.Fatalf("ready bed: %d", rec.Code)
	}
	b.keys.state = extjwt.KeysStale
	if rec := get(b.h, "/readyz"); rec.Code != 200 {
		t.Fatalf("stale keys inside the grace are degraded, not unready: %d", rec.Code)
	}
	b.keys.state = extjwt.KeysUnusable
	rec := get(b.h, "/readyz")
	if rec.Code != 503 || !strings.Contains(rec.Body.String(), "keys_unavailable") {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
	b.keys.state = extjwt.KeysFresh
	b.rev.usable = false
	rec = get(b.h, "/readyz")
	if rec.Code != 503 || !strings.Contains(rec.Body.String(), "revocation_unavailable") {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
}

func TestPublicKeysAreServedAsAnOKPSet(t *testing.T) {
	b := newBed(t)
	rec := get(b.h, "/internal/jwks.json")
	var set struct {
		Keys []map[string]string `json:"keys"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &set); err != nil || rec.Code != 200 || len(set.Keys) != 1 {
		t.Fatalf("%d %s %v", rec.Code, rec.Body.String(), err)
	}
	x, _ := base64.RawURLEncoding.DecodeString(set.Keys[0]["x"])
	if set.Keys[0]["kty"] != "OKP" || len(x) != ed25519.PublicKeySize {
		t.Fatalf("jwk = %v", set.Keys[0])
	}
	if strings.Contains(rec.Body.String(), `"d"`) {
		t.Fatal("private material in the public key set")
	}
}

func TestOnlyGETIsAllowedOnTheCheck(t *testing.T) {
	b := newBed(t)
	rec := httptest.NewRecorder()
	b.h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/check", strings.NewReader("x")))
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST /v1/check = %d", rec.Code)
	}
}

func TestBearerParsing(t *testing.T) {
	mk := func(vals ...string) *http.Request {
		r := httptest.NewRequest(http.MethodGet, "/", nil)
		for _, v := range vals {
			r.Header.Add("Authorization", v)
		}
		return r
	}
	cases := []struct {
		name string
		req  *http.Request
		tok  string
		why  reason.Reason
	}{
		{"ok", mk("Bearer abc"), "abc", reason.None},
		{"any case", mk("BEARER abc"), "abc", reason.None},
		{"none", mk(), "", reason.MissingToken},
		{"basic", mk("Basic abc"), "", reason.MissingToken},
		{"empty", mk("Bearer "), "", reason.MissingToken},
		{"no space", mk("Bearerabc"), "", reason.MissingToken},
		{"two", mk("Bearer a", "Bearer b"), "", reason.Malformed},
	}
	for _, tc := range cases {
		tok, why := bearer(tc.req)
		if tok != tc.tok || why != tc.why {
			t.Errorf("%s: (%q, %q), want (%q, %q)", tc.name, tok, why, tc.tok, tc.why)
		}
	}
}

func TestReadinessLoopFollowsTheState(t *testing.T) {
	b := newBed(t)
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	b.srv.Watch(ctx) // returns when the context ends; must not panic with a nil logger-free setup
}

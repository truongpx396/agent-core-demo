package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/golang-jwt/jwt/v5"
	"github.com/redis/go-redis/v9"

	"authlimit/internal/config"
	"authlimit/internal/identity"
	"authlimit/internal/revoke"
)

// captureStdout runs f and returns what it printed.
func captureStdout(t *testing.T, f func() int) (string, int) {
	t.Helper()
	old := os.Stdout
	r, w, _ := os.Pipe()
	os.Stdout = w
	code := f()
	_ = w.Close()
	os.Stdout = old
	var buf bytes.Buffer
	_, _ = io.Copy(&buf, r)
	return buf.String(), code
}

func TestKeysInitRotateListPrintNoKeyMaterial(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "keys")
	out, code := captureStdout(t, func() int { return keys([]string{"init", "--dir", dir}) })
	if code != 0 || !strings.Contains(out, "created the key directory") {
		t.Fatalf("init: %d %q", code, out)
	}
	if _, code := captureStdout(t, func() int { return keys([]string{"init", "--dir", dir}) }); code == 0 {
		t.Fatal("a second init must refuse")
	}
	out, code = captureStdout(t, func() int { return keys([]string{"rotate", "--dir", dir, "--overlap", "5m"}) })
	if code != 0 || !strings.Contains(out, "added key") {
		t.Fatalf("rotate: %d %q", code, out)
	}
	out, code = captureStdout(t, func() int { return keys([]string{"list", "--dir", dir}) })
	if code != 0 || !strings.Contains(out, "current") || !strings.Contains(out, "published") {
		t.Fatalf("list: %d %q", code, out)
	}
	for _, banned := range []string{"PRIVATE", "BEGIN", "MC4C"} { // MC4C is how an Ed25519 PKCS#8 body begins in base64
		if strings.Contains(out, banned) {
			t.Errorf("list printed %q", banned)
		}
	}
	if _, code := captureStdout(t, func() int { return keys([]string{"list"}) }); code == 0 {
		t.Error("list with no --dir and no AUTHLIMIT_KEYS_DIR must fail")
	}
	if code := keys([]string{"bogus", "--dir", dir}); code == 0 {
		t.Error("an unknown subcommand must fail")
	}
}

func TestServeRefusesToStartWithoutAUsableConfiguration(t *testing.T) {
	for _, k := range []string{"AUTHLIMIT_ISSUER", "AUTHLIMIT_JWKS_URL", "AUTHLIMIT_AUDIENCE", "AUTHLIMIT_KEYS_DIR", "AUTHLIMIT_REDIS_URL"} {
		t.Setenv(k, "")
	}
	if code := serve(nil); code != 2 {
		t.Fatalf("serve with an empty environment returned %d, want 2 (fail closed at start)", code)
	}
}

func TestHealthcheckFollowsTheEndpoint(t *testing.T) {
	status := http.StatusOK
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/readyz" {
			w.WriteHeader(status)
			return
		}
		w.WriteHeader(200)
	}))
	defer srv.Close()
	t.Setenv("AUTHLIMIT_LISTEN", strings.TrimPrefix(srv.URL, "http://"))
	if healthcheck(nil) != 0 || healthcheck([]string{"--ready"}) != 0 {
		t.Fatal("a healthy replica must pass both")
	}
	status = http.StatusServiceUnavailable
	if healthcheck(nil) != 0 {
		t.Error("liveness does not depend on readiness")
	}
	if healthcheck([]string{"--ready"}) == 0 {
		t.Error("an unready replica must fail --ready")
	}
	t.Setenv("AUTHLIMIT_LISTEN", "127.0.0.1:1")
	if healthcheck(nil) == 0 {
		t.Error("nothing listening must fail")
	}
}

// --- the real wiring ---------------------------------------------------------------------------------------------------------------

func freePort(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	return fmt.Sprint(l.Addr().(*net.TCPAddr).Port)
}

func rsaJWKS(kid string, k *rsa.PublicKey) []byte {
	b, _ := json.Marshal(map[string]any{"keys": []map[string]string{{
		"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
		"n": base64.RawURLEncoding.EncodeToString(k.N.Bytes()), "e": base64.RawURLEncoding.EncodeToString(big.NewInt(int64(k.E)).Bytes()),
	}}})
	return b
}

// TestTheRealServiceEndToEnd runs `run` itself: the real configuration, the real key cache against a fake authorization server, the real
// limiter and revocation list against an in-memory Redis, the real HTTP server on a real port. Unit tests prove each part; this proves they
// are connected the way the service connects them.
func TestTheRealServiceEndToEnd(t *testing.T) {
	signer, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	casdoor := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write(rsaJWKS("k1", &signer.PublicKey)) }))
	defer casdoor.Close()
	mr := miniredis.RunT(t)
	keyDir := filepath.Join(t.TempDir(), "keys")
	if _, err := identity.InitKeyset(keyDir, time.Now().Add(-time.Minute)); err != nil {
		t.Fatal(err)
	}
	port := freePort(t)
	env := map[string]string{
		"AUTHLIMIT_ISSUER": "https://auth.localhost:8443", "AUTHLIMIT_JWKS_URL": casdoor.URL, "AUTHLIMIT_AUDIENCE": "https://api.localhost:8443",
		"AUTHLIMIT_KEYS_DIR": keyDir, "AUTHLIMIT_REDIS_URL": "redis://" + mr.Addr(), "AUTHLIMIT_LISTEN": "127.0.0.1:" + port,
		"AUTHLIMIT_REVOCATION_POLL_MS": "100", "AUTHLIMIT_REVOCATION_MAX_STALENESS_SECONDS": "5",
		"AUTHLIMIT_MCP_METADATA_URL_TEMPLATE": "https://mcp.localhost:8443/.well-known/oauth-protected-resource{path}",
		"AUTHLIMIT_LOG_LEVEL":                 "error",
	}
	cfg, err := config.Load(func(k string) string { return env[k] })
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan int, 1)
	go func() { done <- run(ctx, cfg) }()
	base := "http://127.0.0.1:" + port

	// Ready once the keys are fetched and the revocation list is loaded.
	deadline := time.Now().Add(10 * time.Second)
	for {
		resp, err := http.Get(base + "/readyz")
		if err == nil {
			ok := resp.StatusCode == 200
			resp.Body.Close()
			if ok {
				break
			}
		}
		if time.Now().After(deadline) {
			t.Fatal("the service never became ready")
		}
		time.Sleep(50 * time.Millisecond)
	}

	token := func(sub string, over map[string]any) string {
		now := time.Now()
		claims := jwt.MapClaims{
			"iss": "https://auth.localhost:8443", "aud": []string{"https://api.localhost:8443"}, "sub": sub, "owner": "ecorp",
			"scope": "api:chat", "iat": now.Add(-30 * time.Second).Unix(), "exp": now.Add(time.Hour).Unix(),
		}
		for k, v := range over {
			claims[k] = v
		}
		tok := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
		tok.Header["kid"] = "k1"
		s, err := tok.SignedString(signer)
		if err != nil {
			t.Fatal(err)
		}
		return s
	}
	check := func(method, uri, tok string) (*http.Response, string) {
		req, _ := http.NewRequest(http.MethodGet, base+"/v1/check", nil)
		req.Header.Set("X-Forwarded-Method", method)
		req.Header.Set("X-Forwarded-Uri", uri)
		if tok != "" {
			req.Header.Set("Authorization", "Bearer "+tok)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		return resp, string(b)
	}

	// 1. A valid token: 200 and an identity.
	resp, _ := check("POST", "/chat/stream/queued", token("alice", nil))
	if resp.StatusCode != 200 || resp.Header.Get("X-Internal-Identity") == "" {
		t.Fatalf("valid token: %d", resp.StatusCode)
	}
	// 2. No token / a bad token: 401 with the envelope.
	if resp, body := check("POST", "/chat/stream/queued", ""); resp.StatusCode != 401 || !strings.Contains(body, `"unauthenticated"`) {
		t.Fatalf("no token: %d %s", resp.StatusCode, body)
	}
	if resp, _ := check("POST", "/chat/stream/queued", token("alice", map[string]any{"iss": "https://evil.example"})); resp.StatusCode != 401 {
		t.Fatalf("wrong issuer: %d", resp.StatusCode)
	}
	// 3. The person bucket (capacity 5): the 1st request above used one; drain the rest, then 429 with Retry-After. Cancel still works.
	limited := false
	for i := 0; i < 10 && !limited; i++ {
		resp, body := check("POST", "/chat/stream/queued", token("alice", nil))
		if resp.StatusCode == 429 {
			limited = true
			if resp.Header.Get("Retry-After") == "" || !strings.Contains(body, `"rate_limited"`) {
				t.Fatalf("429 without Retry-After or envelope: %q %s", resp.Header.Get("Retry-After"), body)
			}
		}
	}
	if !limited {
		t.Fatal("the person was never limited")
	}
	if resp, _ := check("POST", "/chat/cancel", token("alice", nil)); resp.StatusCode != 200 {
		t.Fatalf("cancel while limited: %d", resp.StatusCode)
	}
	if resp, _ := check("POST", "/chat/stream/queued", token("bob", nil)); resp.StatusCode != 200 {
		t.Fatalf("a colleague must not be limited by alice: %d", resp.StatusCode)
	}
	// 4. Revocation through the real Redis, picked up by the real poller.
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()
	if err := revoke.New(rdb, revoke.Config{Prefix: cfg.KeyPrefix, Timeout: time.Second}, nil, nil, nil).Add(context.Background(), "ecorp", "carol", time.Now(), cfg.MaxTokenLifetime); err != nil {
		t.Fatal(err)
	}
	revoked := false
	for end := time.Now().Add(3 * time.Second); time.Now().Before(end); time.Sleep(50 * time.Millisecond) {
		if resp, _ := check("GET", "/chat/sessions", token("carol", nil)); resp.StatusCode == 401 {
			revoked = true
			break
		}
	}
	if !revoked {
		t.Fatal("a revoked person was still served after the poll interval")
	}
	// 5. The MCP route: no token passes through; a valid one is limited-but-anonymous.
	if resp, _ := check("POST", "/ecorp/mcp", ""); resp.StatusCode != 200 || resp.Header.Get("X-Internal-Identity") != "" {
		t.Fatalf("mcp without a token: %d", resp.StatusCode)
	}
	// 6. The public key set is served, and the container health endpoints answer.
	for _, path := range []string{"/healthz", "/readyz", "/internal/jwks.json"} {
		resp, err := http.Get(base + path)
		if err != nil || resp.StatusCode != 200 {
			t.Fatalf("%s: %v", path, err)
		}
		resp.Body.Close()
	}

	cancel()
	select {
	case code := <-done:
		if code != 0 {
			t.Fatalf("the service exited with %d on shutdown", code)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("the service did not stop within its drain ceiling")
	}
}

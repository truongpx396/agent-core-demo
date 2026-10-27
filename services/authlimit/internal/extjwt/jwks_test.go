package extjwt

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"errors"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func newKey(t testing.TB, bits int) *rsa.PrivateKey {
	t.Helper()
	k, err := rsa.GenerateKey(rand.Reader, bits)
	if err != nil {
		t.Fatal(err)
	}
	return k
}

func b64(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }

func jwksJSON(keys map[string]*rsa.PublicKey) []byte {
	var out []map[string]string
	for kid, k := range keys {
		out = append(out, map[string]string{
			"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
			"n": b64(k.N.Bytes()), "e": b64(big.NewInt(int64(k.E)).Bytes()),
		})
	}
	b, _ := json.Marshal(map[string]any{"keys": out})
	return b
}

// fakeClock is a settable clock safe for concurrent reads.
type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func newClock() *fakeClock { return &fakeClock{t: time.Unix(1_790_000_000, 0)} }
func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}
func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

// keyServer serves a swappable key set and counts requests.
type keyServer struct {
	*httptest.Server
	hits   atomic.Int64
	mu     sync.Mutex
	body   []byte
	status int
	delay  time.Duration
}

func newKeyServer(body []byte) *keyServer {
	ks := &keyServer{body: body, status: 200}
	ks.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ks.hits.Add(1)
		ks.mu.Lock()
		body, status, delay := ks.body, ks.status, ks.delay
		ks.mu.Unlock()
		if delay > 0 {
			select {
			case <-time.After(delay):
			case <-r.Context().Done():
				return
			}
		}
		w.WriteHeader(status)
		_, _ = w.Write(body)
	}))
	return ks
}

func (ks *keyServer) set(body []byte, status int) {
	ks.mu.Lock()
	defer ks.mu.Unlock()
	ks.body, ks.status = body, status
}

func cacheFor(ks *keyServer, clock *fakeClock) *KeyCache {
	return NewKeyCache(KeyCacheConfig{
		URL: ks.URL, TTL: 300 * time.Second, MinRefresh: 30 * time.Second, Grace: 6 * time.Hour, Timeout: 2 * time.Second,
	}, nil, clock.Now, nil, nil)
}

func TestFetchesOnceAndServesFromTheCache(t *testing.T) {
	k1 := newKey(t, 2048)
	ks := newKeyServer(jwksJSON(map[string]*rsa.PublicKey{"k1": &k1.PublicKey}))
	defer ks.Close()
	clock := newClock()
	c := cacheFor(ks, clock)

	if st, _ := c.State(); st != KeysUnusable {
		t.Fatalf("a cache that never fetched must be unusable, got %v", st)
	}
	if err := c.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	for range 5 {
		if _, err := c.Key(context.Background(), "k1"); err != nil {
			t.Fatal(err)
		}
	}
	if ks.hits.Load() != 1 {
		t.Fatalf("%d fetches for 5 lookups of a known key, want 1", ks.hits.Load())
	}
}

func TestAnUnknownKeyIdTriggersOneRefreshPerInterval(t *testing.T) {
	// Threat T10: unlimited tokens with unknown ids must not turn into unlimited fetches from the authorization server.
	k1 := newKey(t, 2048)
	ks := newKeyServer(jwksJSON(map[string]*rsa.PublicKey{"k1": &k1.PublicKey}))
	defer ks.Close()
	clock := newClock()
	c := cacheFor(ks, clock)
	_ = c.Refresh(context.Background())
	base := ks.hits.Load()

	var wg sync.WaitGroup
	for range 50 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := c.Key(context.Background(), "ghost"); !errors.Is(err, ErrUnknownKey) {
				t.Errorf("err = %v, want ErrUnknownKey", err)
			}
		}()
	}
	wg.Wait()
	// The initial Refresh was just made, so inside minRefresh no forced refresh is allowed at all.
	if got := ks.hits.Load() - base; got != 0 {
		t.Fatalf("%d refreshes inside the minimum interval, want 0", got)
	}

	clock.Advance(31 * time.Second)
	for range 50 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, _ = c.Key(context.Background(), "ghost")
		}()
	}
	wg.Wait()
	if got := ks.hits.Load() - base; got != 1 {
		t.Fatalf("%d refreshes for 50 concurrent unknown ids after the interval, want exactly 1", got)
	}
}

func TestARotatedKeyIsPickedUp(t *testing.T) {
	k1, k2 := newKey(t, 2048), newKey(t, 2048)
	ks := newKeyServer(jwksJSON(map[string]*rsa.PublicKey{"k1": &k1.PublicKey}))
	defer ks.Close()
	clock := newClock()
	c := cacheFor(ks, clock)
	_ = c.Refresh(context.Background())

	ks.set(jwksJSON(map[string]*rsa.PublicKey{"k1": &k1.PublicKey, "k2": &k2.PublicKey}), 200)
	clock.Advance(31 * time.Second)
	got, err := c.Key(context.Background(), "k2")
	if err != nil || got.N.Cmp(k2.N) != 0 {
		t.Fatalf("the rotated key was not picked up: %v", err)
	}
}

func TestStateMovesFreshStaleUnusableAndRecovers(t *testing.T) {
	k1 := newKey(t, 2048)
	ks := newKeyServer(jwksJSON(map[string]*rsa.PublicKey{"k1": &k1.PublicKey}))
	defer ks.Close()
	clock := newClock()
	c := cacheFor(ks, clock)
	_ = c.Refresh(context.Background())
	ks.set([]byte("down"), 503)

	steps := []struct {
		advance time.Duration
		want    KeyState
	}{
		{299 * time.Second, KeysFresh},
		{2 * time.Second, KeysStale}, // 301 s: past the TTL
		{5 * time.Hour, KeysStale},   // still inside TTL + 6 h
		{time.Hour, KeysUnusable},    // past TTL + grace
	}
	for _, s := range steps {
		clock.Advance(s.advance)
		if st, _ := c.State(); st != s.want {
			t.Fatalf("after +%s the state is %v, want %v", s.advance, st, s.want)
		}
	}
	if _, err := c.Key(context.Background(), "k1"); !errors.Is(err, ErrKeysUnavailable) {
		t.Fatalf("an unusable cache must say so, got %v", err)
	}

	// The authorization server comes back: the next lookup (a forced refresh, rate-limited) recovers without waiting for the loop.
	ks.set(jwksJSON(map[string]*rsa.PublicKey{"k1": &k1.PublicKey}), 200)
	clock.Advance(31 * time.Second)
	if _, err := c.Key(context.Background(), "k1"); err != nil {
		t.Fatalf("recovery failed: %v", err)
	}
	if st, _ := c.State(); st != KeysFresh {
		t.Fatalf("after recovery the state is %v", st)
	}
}

func TestStaleKeysStillVerifyInsideTheGrace(t *testing.T) {
	k1 := newKey(t, 2048)
	ks := newKeyServer(jwksJSON(map[string]*rsa.PublicKey{"k1": &k1.PublicKey}))
	defer ks.Close()
	clock := newClock()
	c := cacheFor(ks, clock)
	_ = c.Refresh(context.Background())
	ks.set([]byte("down"), 503)
	clock.Advance(2 * time.Hour)
	if _, err := c.Key(context.Background(), "k1"); err != nil {
		t.Fatalf("a stale key inside the grace must still be served: %v", err)
	}
}

func TestABadResponseNeverEvictsTheCachedSet(t *testing.T) {
	k1 := newKey(t, 2048)
	good := jwksJSON(map[string]*rsa.PublicKey{"k1": &k1.PublicKey})
	ks := newKeyServer(good)
	defer ks.Close()
	clock := newClock()
	c := cacheFor(ks, clock)
	_ = c.Refresh(context.Background())

	bad := map[string][]byte{
		"empty set":        []byte(`{"keys":[]}`),
		"not json":         []byte(`<html>`),
		"only a small key": jwksJSON(map[string]*rsa.PublicKey{"tiny": &newKey(t, 1024).PublicKey}),
		"oversize":         []byte(strings.Repeat("x", maxJWKSBytes+10)),
	}
	for name, body := range bad {
		ks.set(body, 200)
		clock.Advance(31 * time.Second)
		if err := c.Refresh(context.Background()); err == nil {
			t.Errorf("%s: accepted", name)
		}
		if _, err := c.Key(context.Background(), "k1"); err != nil {
			t.Errorf("%s: the good set was evicted: %v", name, err)
		}
	}
}

func TestAHangingServerCostsOnlyTheDeadline(t *testing.T) {
	ks := newKeyServer(nil)
	ks.delay = 5 * time.Second
	defer ks.Close()
	c := NewKeyCache(KeyCacheConfig{URL: ks.URL, TTL: time.Minute, MinRefresh: time.Second, Grace: time.Hour, Timeout: 150 * time.Millisecond}, nil, nil, nil, nil)
	start := time.Now()
	err := c.Refresh(context.Background())
	if err == nil {
		t.Fatal("a hanging server must fail the fetch")
	}
	if took := time.Since(start); took > 2*time.Second {
		t.Fatalf("the fetch took %s; the deadline is 150ms", took)
	}
}

func TestTickRefreshesWhenDueAndRetriesAfterAFailure(t *testing.T) {
	k1 := newKey(t, 2048)
	ks := newKeyServer([]byte("down"))
	ks.set([]byte("down"), 503)
	defer ks.Close()
	clock := newClock()
	c := cacheFor(ks, clock)

	c.Tick(context.Background()) // first tick: never attempted, so it attempts (and fails)
	if ks.hits.Load() != 1 {
		t.Fatalf("hits = %d", ks.hits.Load())
	}
	c.Tick(context.Background()) // too soon to retry
	if ks.hits.Load() != 1 {
		t.Fatalf("retried inside the retry interval: %d hits", ks.hits.Load())
	}
	ks.set(jwksJSON(map[string]*rsa.PublicKey{"k1": &k1.PublicKey}), 200)
	clock.Advance(31 * time.Second) // the retry cadence after a failure is minRefresh
	c.Tick(context.Background())
	if st, _ := c.State(); st != KeysFresh {
		t.Fatalf("state = %v after a successful retry", st)
	}
	hits := ks.hits.Load()
	clock.Advance(100 * time.Second) // fresh and well inside the TTL: nothing to do
	c.Tick(context.Background())
	if ks.hits.Load() != hits {
		t.Fatal("refreshed while fresh")
	}
	clock.Advance(201 * time.Second) // 301 s since the last attempt: due
	c.Tick(context.Background())
	if ks.hits.Load() != hits+1 {
		t.Fatalf("did not refresh when the TTL passed")
	}
}

func TestParseJWKSSkipsWhatItCannotUse(t *testing.T) {
	good := newKey(t, 2048)
	ec := `{"kty":"EC","kid":"ec1","crv":"P-256","x":"AA","y":"AA"}`
	enc := `{"kty":"RSA","use":"enc","kid":"enc1","n":"` + b64(good.N.Bytes()) + `","e":"AQAB"}`
	nokid := `{"kty":"RSA","use":"sig","n":"` + b64(good.N.Bytes()) + `","e":"AQAB"}`
	ok := `{"kty":"RSA","use":"sig","kid":"ok","n":"` + b64(good.N.Bytes()) + `","e":"AQAB"}`
	keys, err := ParseJWKS([]byte(`{"keys":[` + strings.Join([]string{ec, enc, nokid, ok}, ",") + `]}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(keys) != 1 || keys["ok"] == nil {
		t.Fatalf("keys = %v, want only \"ok\"", keys)
	}
	if _, err := ParseJWKS([]byte(`{"keys":[` + ec + `]}`)); err == nil {
		t.Fatal("a set with no usable RSA key must be an error, or it would replace a good cached set with nothing")
	}
	if _, err := ParseJWKS([]byte(`{"keys":[{"kty":"RSA","kid":"weak-e","n":"` + b64(good.N.Bytes()) + `","e":"AQ"}]}`)); err == nil {
		t.Fatal("exponent 1 must be refused")
	}
}

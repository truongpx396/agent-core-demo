package extjwt

import (
	"context"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net/http"
	"sync"
	"time"

	"authlimit/internal/telemetry"
)

// ErrUnknownKey means the token names a key id the (usable) key set does not hold. ErrKeysUnavailable means there is no usable key set
// at all: it was never fetched, or it is older than the freshness TTL plus the grace period.
var (
	ErrUnknownKey      = errors.New("unknown key id")
	ErrKeysUnavailable = errors.New("signing keys unavailable")
)

const (
	maxJWKSBytes = 1 << 20 // a key set is a few kilobytes; a megabyte is a misconfigured or hostile address
	minRSABits   = 2048
)

// ParseJWKS reads a JWK Set and returns its RSA signing keys by key id. Keys that are not RSA, are marked for another use, have no key
// id, or are shorter than 2048 bits are skipped, not errors: an authorization server's set can hold encryption keys and old keys. A set
// with no usable key at all is an error, so a bad response can never replace a good cached set with an empty one.
func ParseJWKS(data []byte) (map[string]*rsa.PublicKey, error) {
	var set struct {
		Keys []struct {
			Kty string `json:"kty"`
			Use string `json:"use"`
			Kid string `json:"kid"`
			N   string `json:"n"`
			E   string `json:"e"`
		} `json:"keys"`
	}
	if err := json.Unmarshal(data, &set); err != nil {
		return nil, fmt.Errorf("not a JWK set: %w", err)
	}
	out := map[string]*rsa.PublicKey{}
	for _, k := range set.Keys {
		if k.Kty != "RSA" || k.Kid == "" || (k.Use != "" && k.Use != "sig") {
			continue
		}
		n, errN := base64.RawURLEncoding.DecodeString(k.N)
		e, errE := base64.RawURLEncoding.DecodeString(k.E)
		if errN != nil || errE != nil || len(n) == 0 || len(e) == 0 || len(e) > 4 {
			continue
		}
		modulus := new(big.Int).SetBytes(n)
		exponent := int(new(big.Int).SetBytes(e).Int64())
		if modulus.BitLen() < minRSABits || exponent < 3 {
			continue
		}
		out[k.Kid] = &rsa.PublicKey{N: modulus, E: exponent}
	}
	if len(out) == 0 {
		return nil, errors.New("the JWK set holds no usable RSA signing key")
	}
	return out, nil
}

// StaticKeys is a fixed key set (tests, and the vectors).
type StaticKeys map[string]*rsa.PublicKey

// Key implements KeySource.
func (s StaticKeys) Key(_ context.Context, kid string) (*rsa.PublicKey, error) {
	if k, ok := s[kid]; ok {
		return k, nil
	}
	return nil, ErrUnknownKey
}

// KeyState is how usable the cached set is.
type KeyState int

const (
	// KeysUnusable: never fetched, or older than the TTL plus the grace. Every token is refused.
	KeysUnusable KeyState = iota
	// KeysStale: past the TTL but inside the grace. Tokens are still checked against the cached keys; this is the degraded state.
	KeysStale
	// KeysFresh: fetched within the TTL.
	KeysFresh
)

// KeyCache holds the authorization server's signing keys.
//
// Rules, each from a real failure in spec 017's equivalent (app/mcp/gateway_auth.py::JwksCache) or from the threat model:
//
//   - A token naming an unknown key id triggers one forced refresh, which is how a rotated key is picked up; but at most one per
//     minRefresh however many tokens arrive, because an attacker can mint unlimited tokens with unknown ids and must not be able to turn
//     that into a way to make this service hammer the authorization server (threat T10).
//   - Concurrent requests share one fetch (refreshMu), so a burst after a rotation is one request upstream, not a hundred.
//   - A failed fetch never evicts the cached set. The set is served stale for `grace` beyond the TTL, then refused (fail closed): the
//     cost of a longer grace is trusting a key the server may have withdrawn for compromise (threat T8).
//   - Every fetch has a deadline and a size bound.
type KeyCache struct {
	url        string
	client     *http.Client
	ttl        time.Duration
	minRefresh time.Duration
	grace      time.Duration
	timeout    time.Duration
	now        func() time.Time
	metrics    *telemetry.Metrics
	log        *slog.Logger

	refreshMu sync.Mutex // serialises fetches; held across the HTTP call

	mu          sync.RWMutex
	keys        map[string]*rsa.PublicKey
	fetchedAt   time.Time // last SUCCESSFUL fetch; zero until one succeeds
	lastAttempt time.Time // last attempt, successful or not
	lastOK      bool
	startedAt   time.Time
}

// KeyCacheConfig groups the cache's settings.
type KeyCacheConfig struct {
	URL        string
	TTL        time.Duration
	MinRefresh time.Duration
	Grace      time.Duration
	Timeout    time.Duration
}

// NewKeyCache builds a cache. `client` may be nil (a default client is used; the per-request deadline is the context's, not the client's).
func NewKeyCache(cfg KeyCacheConfig, client *http.Client, now func() time.Time, m *telemetry.Metrics, log *slog.Logger) *KeyCache {
	if client == nil {
		client = &http.Client{}
	}
	if now == nil {
		now = time.Now
	}
	if log == nil {
		log = slog.New(slog.DiscardHandler)
	}
	return &KeyCache{
		url: cfg.URL, client: client, ttl: cfg.TTL, minRefresh: cfg.MinRefresh, grace: cfg.Grace, timeout: cfg.Timeout,
		now: now, metrics: m, log: log, startedAt: now(),
	}
}

// State reports how usable the cache is and how old its keys are (zero when never fetched).
func (c *KeyCache) State() (KeyState, time.Duration) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.stateLocked()
}

func (c *KeyCache) stateLocked() (KeyState, time.Duration) {
	if c.fetchedAt.IsZero() {
		return KeysUnusable, 0
	}
	age := c.now().Sub(c.fetchedAt)
	switch {
	case age <= c.ttl:
		return KeysFresh, age
	case age <= c.ttl+c.grace:
		return KeysStale, age
	default:
		return KeysUnusable, age
	}
}

// Key implements KeySource.
func (c *KeyCache) Key(ctx context.Context, kid string) (*rsa.PublicKey, error) {
	if key, state := c.lookup(kid); state != KeysUnusable && key != nil {
		return key, nil
	}
	// Either the key is unknown, or the whole set is unusable. In both cases one forced refresh may fix it; it is rate-limited.
	c.refreshIfAllowed(ctx)
	key, state := c.lookup(kid)
	switch {
	case state == KeysUnusable:
		return nil, ErrKeysUnavailable
	case key == nil:
		return nil, ErrUnknownKey
	}
	return key, nil
}

func (c *KeyCache) lookup(kid string) (*rsa.PublicKey, KeyState) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	state, _ := c.stateLocked()
	return c.keys[kid], state
}

// refreshIfAllowed fetches unless an attempt was made less than minRefresh ago. Concurrent callers queue on refreshMu and, finding the
// attempt just made, return without fetching again.
func (c *KeyCache) refreshIfAllowed(ctx context.Context) {
	c.refreshMu.Lock()
	defer c.refreshMu.Unlock()
	c.mu.RLock()
	last := c.lastAttempt
	c.mu.RUnlock()
	if !last.IsZero() && c.now().Sub(last) < c.minRefresh {
		return
	}
	c.fetch(ctx)
}

// Refresh fetches now, ignoring the rate limit. Used by the background loop and at start.
func (c *KeyCache) Refresh(ctx context.Context) error {
	c.refreshMu.Lock()
	defer c.refreshMu.Unlock()
	return c.fetch(ctx)
}

// fetch must be called with refreshMu held.
func (c *KeyCache) fetch(ctx context.Context) error {
	c.mu.Lock()
	c.lastAttempt = c.now()
	c.mu.Unlock()

	keys, err := c.download(ctx)
	if err != nil {
		c.mu.Lock()
		c.lastOK = false
		c.mu.Unlock()
		c.metrics.KeyRefresh(false)
		// The error text is the HTTP client's or the parser's, never request data; the address is configuration.
		c.log.Warn("jwks_refresh_failed", "error", err.Error())
		return err
	}
	c.mu.Lock()
	c.keys, c.fetchedAt, c.lastOK = keys, c.now(), true
	c.mu.Unlock()
	c.metrics.KeyRefresh(true)
	return nil
}

func (c *KeyCache) download(ctx context.Context) (map[string]*rsa.PublicKey, error) {
	ctx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	resp, err := c.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("key endpoint answered HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxJWKSBytes+1))
	if err != nil {
		return nil, err
	}
	if len(body) > maxJWKSBytes {
		return nil, errors.New("key endpoint answered with more than 1 MiB")
	}
	return ParseJWKS(body)
}

// Tick is one pass of the background loop: update the age gauge and refresh when due. Due means the TTL has passed since the last
// successful fetch, or, after a failure, minRefresh has passed since the last attempt (retry cadence).
func (c *KeyCache) Tick(ctx context.Context) {
	c.mu.RLock()
	last, lastOK := c.lastAttempt, c.lastOK
	c.mu.RUnlock()
	now := c.now()
	interval := c.ttl
	if !lastOK {
		interval = c.minRefresh
	}
	if last.IsZero() || now.Sub(last) >= interval {
		_ = c.Refresh(ctx) // outcome is counted and logged inside
	}
	// A replica that has NEVER fetched keys reports the time since it started, not 0: a gauge stuck at 0 would hide exactly the replica
	// that cannot reach the authorization server (the alert is on this gauge being above the TTL).
	c.mu.RLock()
	since := c.fetchedAt
	if since.IsZero() {
		since = c.startedAt
	}
	c.mu.RUnlock()
	c.metrics.SetKeysAge(c.now().Sub(since))
}

// Run drives Tick once a second until ctx ends.
func (c *KeyCache) Run(ctx context.Context) {
	t := time.NewTicker(time.Second)
	defer t.Stop()
	c.Tick(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			c.Tick(ctx)
		}
	}
}

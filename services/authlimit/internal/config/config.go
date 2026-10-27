// Package config reads and validates the service's settings from the environment (contracts/authlimit.md, section E).
//
// Two rules shape it:
//
//   - Fail closed at start. A missing or invalid value stops the process with a message naming the VARIABLE, never its value. The
//     Redis URL can carry a password and the key directory is a secret's address; an error that echoed them would put them in a log.
//     All problems are reported together, so an operator fixes the environment once instead of once per restart.
//   - Defaults are the ones the contract lists, and bounds are enforced, not hoped for: a clock leeway of an hour or an identity lifetime
//     of a day would pass a naive parser and quietly turn two security properties off (spec 017's mutation list has "a huge clock leeway"
//     for the same reason).
package config

import (
	"errors"
	"fmt"
	"net"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// Config is the validated configuration. Durations are already durations; nothing downstream parses a string.
type Config struct {
	Listen string

	// Authorization server (Casdoor).
	Issuer         string // canonical: no trailing slash
	JWKSURL        string
	Audience       string // the API's own resource identifier; canonical: no trailing slash
	RequiredScope  string
	TenantClaim    string
	PrincipalClaim string

	MaxTokenLifetime time.Duration
	ClockLeeway      time.Duration

	JWKSCacheTTL     time.Duration
	JWKSMinRefresh   time.Duration
	JWKSFetchTimeout time.Duration
	JWKSStaleGrace   time.Duration

	// Internal identity.
	KeysDir        string
	KeysLoosePerms bool
	IdentityTTL    time.Duration
	KeyOverlap     time.Duration // used by `keys rotate`, kept here so the default has one home

	// Redis (token buckets, revocation list).
	RedisURL     string
	RedisTimeout time.Duration
	KeyPrefix    string

	LimitMode string // "enforce" or "observe"

	RevocationPoll         time.Duration
	RevocationMaxStaleness time.Duration
	RevocationUnavailable  string // "closed" or "open"

	RoutesFile             string // empty: the embedded table
	MCPMetadataURLTemplate string

	LogLevel     string
	OTLPEndpoint string // empty: metrics are not exported
	InstanceID   string
}

// Getenv is os.Getenv in production and a map lookup in tests.
type Getenv func(string) string

// Load reads the environment. Every problem is reported in one joined error.
func Load(get Getenv) (Config, error) {
	l := &loader{get: get}
	c := Config{
		Listen: l.str("AUTHLIMIT_LISTEN", ":8080"),

		RequiredScope:  l.token("AUTHLIMIT_REQUIRED_SCOPE", "api:chat"),
		TenantClaim:    l.token("AUTHLIMIT_TENANT_CLAIM", "owner"),
		PrincipalClaim: l.token("AUTHLIMIT_PRINCIPAL_CLAIM", "sub"),

		MaxTokenLifetime: l.duration("AUTHLIMIT_MAX_TOKEN_LIFETIME", 168*time.Hour, time.Minute, 8760*time.Hour),
		ClockLeeway:      l.duration("AUTHLIMIT_CLOCK_LEEWAY", 10*time.Second, 0, 2*time.Minute),

		JWKSCacheTTL:     l.seconds("AUTHLIMIT_JWKS_CACHE_SECONDS", 300, 1, 86400),
		JWKSMinRefresh:   l.seconds("AUTHLIMIT_JWKS_MIN_REFRESH_SECONDS", 30, 1, 3600),
		JWKSFetchTimeout: l.seconds("AUTHLIMIT_JWKS_FETCH_TIMEOUT_SECONDS", 5, 1, 60),
		JWKSStaleGrace:   l.seconds("AUTHLIMIT_JWKS_STALE_GRACE_SECONDS", 21600, 0, 7*86400),

		KeysDir:        l.required("AUTHLIMIT_KEYS_DIR"),
		KeysLoosePerms: l.boolean("AUTHLIMIT_KEYS_ALLOW_LOOSE_PERMS", false),
		IdentityTTL:    l.seconds("AUTHLIMIT_IDENTITY_TTL_SECONDS", 30, 1, 120),
		KeyOverlap:     l.duration("AUTHLIMIT_KEY_OVERLAP", 10*time.Minute, time.Second, 24*time.Hour),

		RedisURL:     l.required("AUTHLIMIT_REDIS_URL"),
		RedisTimeout: l.millis("AUTHLIMIT_REDIS_TIMEOUT_MS", 100, 10, 5000),
		KeyPrefix:    l.str("AUTHLIMIT_KEY_PREFIX", "authlimit:v1:"),

		LimitMode: l.oneOf("AUTHLIMIT_LIMIT_MODE", "enforce", "enforce", "observe"),

		RevocationPoll:         l.millis("AUTHLIMIT_REVOCATION_POLL_MS", 1000, 100, 60000),
		RevocationMaxStaleness: l.seconds("AUTHLIMIT_REVOCATION_MAX_STALENESS_SECONDS", 30, 1, 3600),
		RevocationUnavailable:  l.oneOf("AUTHLIMIT_REVOCATION_UNAVAILABLE", "closed", "closed", "open"),

		RoutesFile: l.str("AUTHLIMIT_ROUTES_FILE", ""),

		LogLevel:     l.oneOf("AUTHLIMIT_LOG_LEVEL", "info", "debug", "info", "warn", "error"),
		OTLPEndpoint: strings.TrimSpace(get("OTEL_EXPORTER_OTLP_ENDPOINT")),
		InstanceID:   firstNonEmpty(get("HOSTNAME"), "authlimit"),
	}

	c.Issuer = l.url("AUTHLIMIT_ISSUER", true, isLoopback, true)
	c.JWKSURL = l.url("AUTHLIMIT_JWKS_URL", true, func(h string) bool { return isLoopback(h) || !strings.Contains(h, ".") }, true)
	c.Audience = l.url("AUTHLIMIT_AUDIENCE", true, isLoopback, true)

	if t := strings.TrimSpace(get("AUTHLIMIT_MCP_METADATA_URL_TEMPLATE")); t != "" {
		c.MCPMetadataURLTemplate = l.template("AUTHLIMIT_MCP_METADATA_URL_TEMPLATE", t)
	}
	l.redisURL("AUTHLIMIT_REDIS_URL", c.RedisURL)

	if c.RevocationMaxStaleness < 2*c.RevocationPoll {
		l.fail("AUTHLIMIT_REVOCATION_MAX_STALENESS_SECONDS", "must be at least twice AUTHLIMIT_REVOCATION_POLL_MS, or one slow poll makes the list look stale")
	}
	if c.JWKSMinRefresh > c.JWKSCacheTTL {
		l.fail("AUTHLIMIT_JWKS_MIN_REFRESH_SECONDS", "must not exceed AUTHLIMIT_JWKS_CACHE_SECONDS")
	}
	if !strings.HasSuffix(c.KeyPrefix, ":") || strings.TrimSpace(c.KeyPrefix) == ":" {
		l.fail("AUTHLIMIT_KEY_PREFIX", "must be a non-empty prefix ending in ':' (the limiter's keys must never collide with the work queue's)")
	}

	if err := l.err(); err != nil {
		return Config{}, err
	}
	return c, nil
}

// --- the loader ---------------------------------------------------------------------------------------------------------------

type loader struct {
	get  Getenv
	errs []error
}

func (l *loader) fail(name, problem string) {
	l.errs = append(l.errs, fmt.Errorf("%s: %s", name, problem)) // the name and the problem; never the value
}

func (l *loader) err() error { return errors.Join(l.errs...) }

func (l *loader) raw(name string) string { return strings.TrimSpace(l.get(name)) }

func (l *loader) str(name, def string) string {
	if v := l.raw(name); v != "" {
		return v
	}
	return def
}

func (l *loader) required(name string) string {
	v := l.raw(name)
	if v == "" {
		l.fail(name, "is required")
	}
	return v
}

// token is a string that must be a single word (a claim name, a scope): scopes are space-separated, so a space inside one would
// let it match two.
func (l *loader) token(name, def string) string {
	v := l.str(name, def)
	if strings.ContainsAny(v, " \t\r\n") {
		l.fail(name, "must be a single word with no whitespace")
	}
	return v
}

func (l *loader) oneOf(name, def string, allowed ...string) string {
	v := l.str(name, def)
	for _, a := range allowed {
		if v == a {
			return v
		}
	}
	l.fail(name, "must be one of: "+strings.Join(allowed, ", "))
	return def
}

func (l *loader) boolean(name string, def bool) bool {
	v := l.raw(name)
	if v == "" {
		return def
	}
	switch strings.ToLower(v) {
	case "true", "1", "yes":
		return true
	case "false", "0", "no":
		return false
	}
	l.fail(name, "must be true or false")
	return def
}

func (l *loader) intIn(name string, def, lo, hi int) int {
	v := l.raw(name)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		l.fail(name, "must be a whole number")
		return def
	}
	if n < lo || n > hi {
		l.fail(name, fmt.Sprintf("must be between %d and %d", lo, hi))
		return def
	}
	return n
}

func (l *loader) seconds(name string, def, lo, hi int) time.Duration {
	return time.Duration(l.intIn(name, def, lo, hi)) * time.Second
}

func (l *loader) millis(name string, def, lo, hi int) time.Duration {
	return time.Duration(l.intIn(name, def, lo, hi)) * time.Millisecond
}

func (l *loader) duration(name string, def, lo, hi time.Duration) time.Duration {
	v := l.raw(name)
	if v == "" {
		return def
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		l.fail(name, "must be a duration such as 168h or 10s")
		return def
	}
	if d < lo || d > hi {
		l.fail(name, fmt.Sprintf("must be between %s and %s", lo, hi))
		return def
	}
	return d
}

// url validates an absolute http(s) URL and returns it without a trailing slash. Plain http is accepted only where allowPlain says so
// for the host: the issuer and the audience are shown to clients and must be https unless the host is loopback; the key address is
// fetched by this process over the compose network, so an in-network (dotless) name may use http, exactly as in spec 017.
func (l *loader) url(name string, required bool, allowPlain func(host string) bool, allowPath bool) string {
	v := l.raw(name)
	if v == "" {
		if required {
			l.fail(name, "is required")
		}
		return ""
	}
	v = strings.TrimRight(v, "/")
	u, err := url.Parse(v)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" {
		l.fail(name, "must be an absolute http(s) URL")
		return ""
	}
	if u.RawQuery != "" || u.Fragment != "" || u.User != nil || (u.Path != "" && !allowPath) {
		l.fail(name, "must not carry credentials, a query or a fragment")
		return ""
	}
	if u.Scheme == "http" && !allowPlain(u.Hostname()) {
		l.fail(name, "must be https (plain http is accepted for a loopback host only)")
		return ""
	}
	return v
}

// template is the revoked-person 401's resource_metadata URL; {path} stands for the request path.
func (l *loader) template(name, v string) string {
	if !strings.Contains(v, "{path}") {
		l.fail(name, "must contain {path}, which stands for the request path")
		return ""
	}
	probe := strings.ReplaceAll(v, "{path}", "/x")
	u, err := url.Parse(probe)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" {
		l.fail(name, "must be an absolute http(s) URL containing {path}")
		return ""
	}
	if u.Scheme == "http" && !isLoopback(u.Hostname()) {
		l.fail(name, "must be https (plain http is accepted for a loopback host only)")
		return ""
	}
	return v
}

func (l *loader) redisURL(name, v string) {
	if v == "" {
		return // already reported as missing
	}
	u, err := url.Parse(v)
	if err != nil || (u.Scheme != "redis" && u.Scheme != "rediss" && u.Scheme != "unix") {
		l.fail(name, "must be a redis://, rediss:// or unix:// URL") // the value may carry a password: it is not echoed
	}
}

// isLoopback is spec 017's rule: localhost, any *.localhost name, or a loopback address.
func isLoopback(host string) bool {
	if host == "localhost" || strings.HasSuffix(host, ".localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func firstNonEmpty(vs ...string) string {
	for _, v := range vs {
		if s := strings.TrimSpace(v); s != "" {
			return s
		}
	}
	return ""
}

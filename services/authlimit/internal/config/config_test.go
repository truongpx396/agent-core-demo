package config

import (
	"strings"
	"testing"
	"time"
)

func env(m map[string]string) Getenv { return func(k string) string { return m[k] } }

// minimal is the smallest valid environment: only the values with no default.
func minimal() map[string]string {
	return map[string]string{
		"AUTHLIMIT_ISSUER":    "https://auth.localhost:8443",
		"AUTHLIMIT_JWKS_URL":  "http://casdoor:8000/.well-known/jwks",
		"AUTHLIMIT_AUDIENCE":  "https://api.localhost:8443",
		"AUTHLIMIT_KEYS_DIR":  "/run/keys",
		"AUTHLIMIT_REDIS_URL": "redis://redis:6379/0",
	}
}

func with(over map[string]string) map[string]string {
	m := minimal()
	for k, v := range over {
		m[k] = v
	}
	return m
}

func TestDefaultsAreTheContractsDefaults(t *testing.T) {
	c, err := Load(env(minimal()))
	if err != nil {
		t.Fatal(err)
	}
	checks := []struct {
		name      string
		got, want any
	}{
		{"listen", c.Listen, ":8080"},
		{"scope", c.RequiredScope, "api:chat"},
		{"tenant claim", c.TenantClaim, "owner"},
		{"principal claim", c.PrincipalClaim, "sub"},
		{"max lifetime", c.MaxTokenLifetime, 168 * time.Hour},
		{"leeway", c.ClockLeeway, 10 * time.Second},
		{"jwks cache", c.JWKSCacheTTL, 300 * time.Second},
		{"jwks min refresh", c.JWKSMinRefresh, 30 * time.Second},
		{"jwks fetch timeout", c.JWKSFetchTimeout, 5 * time.Second},
		{"jwks grace", c.JWKSStaleGrace, 6 * time.Hour},
		{"identity ttl", c.IdentityTTL, 30 * time.Second},
		{"key overlap", c.KeyOverlap, 10 * time.Minute},
		{"redis timeout", c.RedisTimeout, 100 * time.Millisecond},
		{"prefix", c.KeyPrefix, "authlimit:v1:"},
		{"limit mode", c.LimitMode, "enforce"},
		{"revocation poll", c.RevocationPoll, time.Second},
		{"revocation staleness", c.RevocationMaxStaleness, 30 * time.Second},
		{"revocation policy", c.RevocationUnavailable, "closed"},
		{"loose perms", c.KeysLoosePerms, false},
	}
	for _, ck := range checks {
		if ck.got != ck.want {
			t.Errorf("%s = %v, want %v", ck.name, ck.got, ck.want)
		}
	}
}

func TestRequiredVariablesAreReportedTogetherAndByName(t *testing.T) {
	_, err := Load(env(map[string]string{}))
	if err == nil {
		t.Fatal("an empty environment must not start")
	}
	for _, name := range []string{"AUTHLIMIT_ISSUER", "AUTHLIMIT_JWKS_URL", "AUTHLIMIT_AUDIENCE", "AUTHLIMIT_KEYS_DIR", "AUTHLIMIT_REDIS_URL"} {
		if !strings.Contains(err.Error(), name) {
			t.Errorf("error does not name %s: %v", name, err)
		}
	}
}

func TestErrorsNeverEchoValues(t *testing.T) {
	// The Redis URL carries a password and the key directory is a secret's address: neither may reach a log through an error.
	_, err := Load(env(with(map[string]string{
		"AUTHLIMIT_REDIS_URL": "http://:hunter2@redis:6379",
		"AUTHLIMIT_KEYS_DIR":  "",
		"AUTHLIMIT_ISSUER":    "http://evil.example/secret-path",
	})))
	if err == nil {
		t.Fatal("expected an error")
	}
	for _, leaked := range []string{"hunter2", "evil.example", "secret-path"} {
		if strings.Contains(err.Error(), leaked) {
			t.Errorf("error echoes %q: %v", leaked, err)
		}
	}
}

func TestURLRules(t *testing.T) {
	tests := []struct {
		name string
		env  map[string]string
		ok   bool
	}{
		{"issuer https", map[string]string{"AUTHLIMIT_ISSUER": "https://auth.example.com"}, true},
		{"issuer plain http on a public host", map[string]string{"AUTHLIMIT_ISSUER": "http://auth.example.com"}, false},
		{"issuer plain http on loopback", map[string]string{"AUTHLIMIT_ISSUER": "http://127.0.0.1:8000"}, true},
		{"issuer plain http on a .localhost name", map[string]string{"AUTHLIMIT_ISSUER": "http://auth.localhost:8000"}, true},
		{"issuer with a path is fine", map[string]string{"AUTHLIMIT_ISSUER": "https://auth.example.com/tenant"}, true},
		{"issuer with a query", map[string]string{"AUTHLIMIT_ISSUER": "https://auth.example.com/?a=b"}, false},
		{"issuer not a url", map[string]string{"AUTHLIMIT_ISSUER": "auth.example.com"}, false},
		{"issuer with credentials", map[string]string{"AUTHLIMIT_ISSUER": "https://u:p@auth.example.com"}, false},
		{"jwks over http to a dotless in-network name", map[string]string{"AUTHLIMIT_JWKS_URL": "http://casdoor:8000/.well-known/jwks"}, true},
		{"jwks over http to a public host", map[string]string{"AUTHLIMIT_JWKS_URL": "http://keys.example.com/jwks"}, false},
		{"jwks over https to a public host", map[string]string{"AUTHLIMIT_JWKS_URL": "https://keys.example.com/jwks"}, true},
		{"audience plain http public", map[string]string{"AUTHLIMIT_AUDIENCE": "http://api.example.com"}, false},
		{"redis url wrong scheme", map[string]string{"AUTHLIMIT_REDIS_URL": "http://redis:6379"}, false},
		{"redis tls", map[string]string{"AUTHLIMIT_REDIS_URL": "rediss://redis:6380"}, true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Load(env(with(tc.env)))
			if tc.ok && err != nil {
				t.Fatalf("expected valid, got %v", err)
			}
			if !tc.ok && err == nil {
				t.Fatal("expected an error")
			}
		})
	}
}

func TestTrailingSlashIsNormalised(t *testing.T) {
	// A resource identifier compared as a string must have one spelling (spec 017 found a library that appended a slash).
	c, err := Load(env(with(map[string]string{"AUTHLIMIT_AUDIENCE": "https://api.localhost:8443/", "AUTHLIMIT_ISSUER": "https://auth.localhost:8443/"})))
	if err != nil {
		t.Fatal(err)
	}
	if c.Audience != "https://api.localhost:8443" || c.Issuer != "https://auth.localhost:8443" {
		t.Fatalf("audience %q issuer %q", c.Audience, c.Issuer)
	}
}

func TestBounds(t *testing.T) {
	bad := map[string]string{
		"AUTHLIMIT_IDENTITY_TTL_SECONDS":       "121", // an identity that lives minutes is a replayable credential
		"AUTHLIMIT_CLOCK_LEEWAY":               "1h",  // a leeway of an hour turns expiry into a suggestion
		"AUTHLIMIT_MAX_TOKEN_LIFETIME":         "0s",
		"AUTHLIMIT_REDIS_TIMEOUT_MS":           "0",
		"AUTHLIMIT_JWKS_FETCH_TIMEOUT_SECONDS": "0",
		"AUTHLIMIT_LIMIT_MODE":                 "shadow",
		"AUTHLIMIT_REVOCATION_UNAVAILABLE":     "maybe",
		"AUTHLIMIT_REQUIRED_SCOPE":             "api:chat api:admin", // two scopes in one value would match either
		"AUTHLIMIT_JWKS_CACHE_SECONDS":         "abc",
		"AUTHLIMIT_KEY_PREFIX":                 "authlimit",
	}
	for name, value := range bad {
		if _, err := Load(env(with(map[string]string{name: value}))); err == nil {
			t.Errorf("%s=%q must be refused", name, value)
		} else if !strings.Contains(err.Error(), name) {
			t.Errorf("%s=%q: error does not name the variable: %v", name, value, err)
		}
	}
	// The edges that are allowed.
	good := map[string]string{
		"AUTHLIMIT_IDENTITY_TTL_SECONDS": "120",
		"AUTHLIMIT_CLOCK_LEEWAY":         "2m",
		"AUTHLIMIT_LIMIT_MODE":           "observe",
	}
	for name, value := range good {
		if _, err := Load(env(with(map[string]string{name: value}))); err != nil {
			t.Errorf("%s=%q must be accepted: %v", name, value, err)
		}
	}
}

func TestStalenessMustCoverTwoPolls(t *testing.T) {
	_, err := Load(env(with(map[string]string{"AUTHLIMIT_REVOCATION_POLL_MS": "20000", "AUTHLIMIT_REVOCATION_MAX_STALENESS_SECONDS": "30"})))
	if err == nil || !strings.Contains(err.Error(), "AUTHLIMIT_REVOCATION_MAX_STALENESS_SECONDS") {
		t.Fatalf("a staleness ceiling under two polls must be refused, got %v", err)
	}
}

func TestMCPTemplateNeedsAPlaceholder(t *testing.T) {
	if _, err := Load(env(with(map[string]string{"AUTHLIMIT_MCP_METADATA_URL_TEMPLATE": "https://mcp.localhost:8443/.well-known/oauth-protected-resource"}))); err == nil {
		t.Fatal("a template without {path} must be refused")
	}
	c, err := Load(env(with(map[string]string{"AUTHLIMIT_MCP_METADATA_URL_TEMPLATE": "https://mcp.localhost:8443/.well-known/oauth-protected-resource{path}"})))
	if err != nil || c.MCPMetadataURLTemplate == "" {
		t.Fatalf("valid template refused: %v", err)
	}
}

func TestOTLPAndInstance(t *testing.T) {
	c, err := Load(env(with(map[string]string{"OTEL_EXPORTER_OTLP_ENDPOINT": " http://host.docker.internal:4318 ", "HOSTNAME": "authlimit-2"})))
	if err != nil {
		t.Fatal(err)
	}
	if c.OTLPEndpoint != "http://host.docker.internal:4318" || c.InstanceID != "authlimit-2" {
		t.Fatalf("otlp %q instance %q", c.OTLPEndpoint, c.InstanceID)
	}
}

package extjwt

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"

	"authlimit/internal/reason"
)

// vectorsDir is the shared vectors directory at the repository root (this package is services/authlimit/internal/extjwt).
var vectorsDir = filepath.Join("..", "..", "..", "..", "testdata", "auth-vectors")

type expectation struct {
	Result    string `json:"result"`
	Reason    string `json:"reason"`
	Tenant    string `json:"tenant"`
	Principal string `json:"principal"`
}

type vectorFile struct {
	Now    int64 `json:"now"`
	Config struct {
		Issuer         string `json:"issuer"`
		Audience       string `json:"audience"`
		RequiredScope  string `json:"required_scope"`
		TenantClaim    string `json:"tenant_claim"`
		PrincipalClaim string `json:"principal_claim"`
		LeewaySeconds  int    `json:"leeway_seconds"`
		MaxLifetime    int    `json:"max_token_lifetime_seconds"`
	} `json:"config"`
	Vectors []struct {
		Name   string `json:"name"`
		Token  string `json:"token"`
		Expect struct {
			Chat     expectation `json:"chat"`
			Identify expectation `json:"identify"`
			MCP      expectation `json:"mcp"`
		} `json:"expect"`
	} `json:"vectors"`
}

func loadVectors(t testing.TB) (vectorFile, StaticKeys) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(vectorsDir, "vectors.json"))
	if err != nil {
		t.Fatalf("the shared vectors are missing (scripts/gen_auth_vectors.py): %v", err)
	}
	var f vectorFile
	if err := json.Unmarshal(raw, &f); err != nil {
		t.Fatal(err)
	}
	jwks, err := os.ReadFile(filepath.Join(vectorsDir, "jwks.json"))
	if err != nil {
		t.Fatal(err)
	}
	keys, err := ParseJWKS(jwks)
	if err != nil {
		t.Fatal(err)
	}
	return f, StaticKeys(keys)
}

func settingsFor(f vectorFile) Settings {
	return Settings{
		Issuer: f.Config.Issuer, Audience: f.Config.Audience, RequiredScope: f.Config.RequiredScope,
		TenantClaim: f.Config.TenantClaim, PrincipalClaim: f.Config.PrincipalClaim,
		MaxLifetime: time.Duration(f.Config.MaxLifetime) * time.Second, Leeway: time.Duration(f.Config.LeewaySeconds) * time.Second,
	}
}

func verifierFor(f vectorFile, keys KeySource) *Verifier {
	return NewVerifier(settingsFor(f), keys, func() time.Time { return time.Unix(f.Now, 0) })
}

func check(t *testing.T, name, profile string, got Claims, why reason.Reason, want expectation) {
	t.Helper()
	if want.Result == "valid" {
		if why != reason.None {
			t.Errorf("%s [%s]: refused (%s), the vector says valid", name, profile, why)
			return
		}
		if got.Tenant != want.Tenant || got.Principal != want.Principal {
			t.Errorf("%s [%s]: identity = %s/%s, want %s/%s", name, profile, got.Tenant, got.Principal, want.Tenant, want.Principal)
		}
		return
	}
	if string(why) != want.Reason {
		t.Errorf("%s [%s]: reason = %q, want %q", name, profile, why, want.Reason)
	}
	if got.Tenant != "" || got.Principal != "" || got.Scopes != nil {
		t.Errorf("%s [%s]: a refused token must yield no claims, got %+v", name, profile, got)
	}
}

func TestEverySharedVector(t *testing.T) {
	f, keys := loadVectors(t)
	if len(f.Vectors) < 25 {
		t.Fatalf("only %d vectors; the spec asks for at least 25 covering every refusal reason", len(f.Vectors))
	}
	v := verifierFor(f, keys)
	for _, vec := range f.Vectors {
		got, why := v.Verify(context.Background(), vec.Token, Chat)
		check(t, vec.Name, "chat", got, why, vec.Expect.Chat)
		got, why = v.Verify(context.Background(), vec.Token, Identify)
		check(t, vec.Name, "identify", got, why, vec.Expect.Identify)
	}
}

func TestLimitOnlyModeNeverRefusesWhatTheMCPGatewayAccepts(t *testing.T) {
	// The bypass rule (testdata/auth-vectors/README.md): a token the MCP gateway accepts that limit-only mode refuses would pass
	// through un-metered. The generator enforces it on write; this re-checks the verifier against the vectors it actually ships.
	f, keys := loadVectors(t)
	v := verifierFor(f, keys)
	checked := 0
	for _, vec := range f.Vectors {
		if vec.Expect.MCP.Result != "valid" {
			continue
		}
		checked++
		if _, why := v.Verify(context.Background(), vec.Token, Identify); why != reason.None {
			t.Errorf("%s: the MCP gateway accepts it but limit-only mode refuses it (%s)", vec.Name, why)
		}
	}
	if checked == 0 {
		t.Fatal("no vector exercises the rule")
	}
}

func TestEveryRefusalReasonIsCoveredByAVector(t *testing.T) {
	// The spec asks the vectors to cover every refusal reason of FR-002. The reasons that need no token to produce (missing, and the
	// availability ones) are not token vectors.
	f, _ := loadVectors(t)
	seen := map[string]bool{}
	for _, vec := range f.Vectors {
		seen[vec.Expect.Chat.Reason] = true
	}
	for _, r := range []reason.Reason{
		reason.Malformed, reason.BadAlgorithm, reason.BadSignature, reason.BadIssuer, reason.BadAudience, reason.Expired,
		reason.NotYetValid, reason.NoIssueTime, reason.LifetimeTooLong, reason.BadClaims, reason.BadScope,
	} {
		if !seen[string(r)] {
			t.Errorf("no vector produces the reason %q", r)
		}
	}
}

func TestEmptyAndOversizedInput(t *testing.T) {
	f, keys := loadVectors(t)
	v := verifierFor(f, keys)
	if _, why := v.Verify(context.Background(), "", Chat); why != reason.MissingToken {
		t.Errorf("empty = %s", why)
	}
	if _, why := v.Verify(context.Background(), strings.Repeat("a", maxTokenBytes+1), Chat); why != reason.Malformed {
		t.Errorf("oversized = %s", why)
	}
}

type keySourceFunc func(ctx context.Context, kid string) (*rsa.PublicKey, error)

func (f keySourceFunc) Key(ctx context.Context, kid string) (*rsa.PublicKey, error) {
	return f(ctx, kid)
}

func TestKeySourceFailuresAreAvailabilityNotAnAnswerAboutTheToken(t *testing.T) {
	f, _ := loadVectors(t)
	var token string
	for _, vec := range f.Vectors {
		if vec.Name == "valid_chat" {
			token = vec.Token
		}
	}
	if token == "" {
		t.Fatal("no valid_chat vector")
	}
	for _, err := range []error{ErrKeysUnavailable, errors.New("connection refused")} {
		src := keySourceFunc(func(context.Context, string) (*rsa.PublicKey, error) { return nil, err })
		if _, why := verifierFor(f, src).Verify(context.Background(), token, Chat); why != reason.KeysUnavailable {
			t.Errorf("key source error %q gave %s, want keys_unavailable (the token must not be blamed)", err, why)
		}
	}
}

func TestAnEmptyKidNeverReachesTheKeySource(t *testing.T) {
	// Asking the cache for an empty id would cost a (rate-limited) forced refresh per such token.
	f, _ := loadVectors(t)
	var token string
	for _, vec := range f.Vectors {
		if vec.Name == "no_kid" {
			token = vec.Token
		}
	}
	called := false
	src := keySourceFunc(func(context.Context, string) (*rsa.PublicKey, error) { called = true; return nil, ErrUnknownKey })
	if _, why := verifierFor(f, src).Verify(context.Background(), token, Chat); why != reason.BadSignature || called {
		t.Fatalf("reason %s, key source called %v", why, called)
	}
}

// Fuzzing: the verifier must never panic, and a valid result must always carry a tenant and a person. Run longer with
// `go test -fuzz=FuzzVerify ./internal/extjwt`; a plain `go test` runs the seeds.
func FuzzVerify(f *testing.F) {
	vf, keys := loadVectors(f)
	v := verifierFor(vf, keys)
	for _, vec := range vf.Vectors {
		f.Add(vec.Token)
	}
	for _, s := range []string{"", ".", "..", "a.b.c", "e30.e30.e30", "eyJhbGciOiJSUzI1NiIsImtpZCI6ImsxIn0.e30.AA", "\x00\x00\x00"} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, token string) {
		for _, p := range []Profile{Chat, Identify} {
			claims, why := v.Verify(context.Background(), token, p)
			if why == reason.None && (claims.Tenant == "" || claims.Principal == "") {
				t.Fatalf("a valid result with an empty identity: %+v", claims)
			}
			if why != reason.None && (claims.Tenant != "" || claims.Principal != "") {
				t.Fatalf("a refusal that leaked claims: %+v", claims)
			}
		}
	})
}

// signedWith builds a validly signed token with arbitrary claims, so claim handling (not just the signature check) is exercised.
func signedWith(t *testing.T, key *rsa.PrivateKey, claims map[string]any) string {
	t.Helper()
	tok := jwt.NewWithClaims(jwt.SigningMethodRS256, jwt.MapClaims(claims))
	tok.Header["kid"] = "k1"
	s, err := tok.SignedString(key)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

// A claim of every JSON type in every claim position must be a refusal or a correct acceptance, never a panic (the scope-parser failure in
// spec 017 was exactly this: a number where text was expected).
func TestEveryClaimOfEveryTypeIsHandled(t *testing.T) {
	f, _ := loadVectors(t)
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	v := verifierFor(f, StaticKeys{"k1": &key.PublicKey})
	base := func() map[string]any {
		return map[string]any{"iss": f.Config.Issuer, "aud": []any{f.Config.Audience}, "sub": "u", "owner": "t", "scope": f.Config.RequiredScope,
			"iat": f.Now - 60, "nbf": f.Now - 60, "exp": f.Now + 3000}
	}
	if _, why := v.Verify(context.Background(), signedWith(t, key, base()), Chat); why != reason.None {
		t.Fatalf("the control token must be valid, got %s", why)
	}
	values := []any{nil, true, 0, 1.5, -1, 1e300, "x", "", []any{}, []any{1, "a"}, map[string]any{"a": 1}}
	for _, name := range []string{"iss", "aud", "exp", "nbf", "iat", "sub", "owner", "scope"} {
		for _, val := range values {
			claims := base()
			claims[name] = val
			token := signedWith(t, key, claims)
			for _, p := range []Profile{Chat, Identify} {
				got, why := v.Verify(context.Background(), token, p) // the assertion is that this returns
				if why == reason.None && (got.Tenant == "" || got.Principal == "") {
					t.Errorf("claim %s=%v: valid with an empty identity", name, val)
				}
			}
		}
	}
	// The specific shapes that must be refused.
	for name, val := range map[string]any{"owner": float64(123), "sub": float64(123), "scope": float64(5), "exp": "tomorrow", "iat": "now", "aud": float64(1)} {
		claims := base()
		claims[name] = val
		if _, why := v.Verify(context.Background(), signedWith(t, key, claims), Chat); why == reason.None {
			t.Errorf("%s=%v must be refused", name, val)
		}
	}
}

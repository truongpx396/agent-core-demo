package identity

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"

	"authlimit/internal/extjwt"
	"authlimit/internal/reason"
)

var updateGolden = flag.Bool("update-golden", false, "rewrite testdata/auth-vectors/identity.json with a freshly minted identity")

func mustMinter(t *testing.T, ttl time.Duration) (*Minter, *Keyset, *clock) {
	t.Helper()
	_, c, ks := newKeyset(t)
	return NewMinter(ks, ttl, c.now), ks, c
}

// verifyWith checks a minted identity the way the API will: EdDSA only, against the PUBLISHED keys, with issuer, audience and time.
func verifyWith(t *testing.T, ks *Keyset, token string, now time.Time) (jwt.MapClaims, *jwt.Token, error) {
	t.Helper()
	keys := map[string]ed25519.PublicKey{}
	for _, p := range ks.Published() {
		keys[p.KID] = p.Key
	}
	mc := jwt.MapClaims{}
	tok, err := jwt.NewParser(
		jwt.WithValidMethods([]string{Algorithm}), jwt.WithIssuer(Issuer), jwt.WithAudience(Audience),
		jwt.WithExpirationRequired(), jwt.WithTimeFunc(func() time.Time { return now }),
	).ParseWithClaims(token, mc, func(tk *jwt.Token) (any, error) {
		kid, _ := tk.Header["kid"].(string)
		if k, ok := keys[kid]; ok {
			return k, nil
		}
		return nil, jwt.ErrTokenUnverifiable
	})
	return mc, tok, err
}

func TestAMintedIdentityMatchesTheContract(t *testing.T) {
	m, ks, c := mustMinter(t, 30*time.Second)
	token, err := m.Mint("ecorp", "user-1", []string{"api:chat", "openid"})
	if err != nil {
		t.Fatal(err)
	}
	mc, tok, err := verifyWith(t, ks, token, c.t)
	if err != nil {
		t.Fatalf("a minted identity must verify: %v", err)
	}
	if tok.Header["alg"] != "EdDSA" || tok.Header["typ"] != "authlimit-identity+jwt" || tok.Header["kid"] == "" {
		t.Fatalf("header = %v", tok.Header)
	}
	if mc["iss"] != "urn:agent-core:authlimit" || mc["aud"] != "agent-core-api" || mc["sub"] != "user-1" || mc["tenant"] != "ecorp" || mc["scope"] != "api:chat openid" {
		t.Fatalf("claims = %v", mc)
	}
	iat, exp, nbf := mc["iat"].(float64), mc["exp"].(float64), mc["nbf"].(float64)
	if exp-iat != 30 || nbf != iat {
		t.Fatalf("iat %v nbf %v exp %v: the lifetime must be exactly the configured TTL", iat, nbf, exp)
	}
	if jti, _ := mc["jti"].(string); len(jti) < 20 {
		t.Fatalf("jti = %q", jti)
	}
}

func TestAnIdentityExpiresAndIsNotValidBeforeItIsMinted(t *testing.T) {
	m, ks, c := mustMinter(t, 30*time.Second)
	token, _ := m.Mint("ecorp", "u", nil)
	if _, _, err := verifyWith(t, ks, token, c.t.Add(29*time.Second)); err != nil {
		t.Fatalf("inside its lifetime: %v", err)
	}
	if _, _, err := verifyWith(t, ks, token, c.t.Add(31*time.Second)); err == nil {
		t.Fatal("an identity must not outlive its TTL: a captured one would be a replayable credential")
	}
	if _, _, err := verifyWith(t, ks, token, c.t.Add(-time.Minute)); err == nil {
		t.Fatal("an identity must not be valid before it was minted")
	}
}

func TestEveryIdentityHasItsOwnJTI(t *testing.T) {
	m, _, _ := mustMinter(t, 30*time.Second)
	seen := map[string]bool{}
	for range 200 {
		token, err := m.Mint("ecorp", "u", nil)
		if err != nil {
			t.Fatal(err)
		}
		parts := strings.Split(token, ".")
		raw, _ := base64.RawURLEncoding.DecodeString(parts[1])
		var claims map[string]any
		_ = json.Unmarshal(raw, &claims)
		jti := claims["jti"].(string)
		if seen[jti] {
			t.Fatal("a repeated jti")
		}
		seen[jti] = true
	}
}

func TestMintRefusesWhatItWouldHaveToGuessAbout(t *testing.T) {
	m, _, _ := mustMinter(t, 30*time.Second)
	bad := []string{"", strings.Repeat("a", MaxIDBytes+1), "a\nb", "a\x00b", "tab\there", "\x7f", string([]byte{0xff, 0xfe})}
	for _, id := range bad {
		if _, err := m.Mint(id, "u", nil); err == nil {
			t.Errorf("tenant %q accepted", id)
		}
		if _, err := m.Mint("t", id, nil); err == nil {
			t.Errorf("principal %q accepted", id)
		}
	}
	if _, err := m.Mint(strings.Repeat("a", MaxIDBytes), "ünï-ユーザー", nil); err != nil {
		t.Errorf("a maximum-length tenant and a unicode principal are legitimate: %v", err)
	}
}

func TestTheInternalIdentityCanNeverBeAcceptedAsAnExternalToken(t *testing.T) {
	// Threat T3: the two token kinds differ in algorithm, issuer, audience, `typ` and key set, and the external verifier pins the
	// algorithm by name. This runs the real external verifier over a real internal identity.
	m, _, c := mustMinter(t, 30*time.Second)
	token, _ := m.Mint("ecorp", "user-1", []string{"api:chat"})
	v := extjwt.NewVerifier(extjwt.Settings{
		Issuer: "https://auth.localhost:8443", Audience: "https://api.localhost:8443", RequiredScope: "api:chat",
		TenantClaim: "owner", PrincipalClaim: "sub", MaxLifetime: 168 * time.Hour, Leeway: 10 * time.Second,
	}, extjwt.StaticKeys{}, c.now)
	for _, p := range []extjwt.Profile{extjwt.Chat, extjwt.Identify} {
		if _, why := v.Verify(context.Background(), token, p); why != reason.BadAlgorithm {
			t.Errorf("profile %d: reason %q, want bad_algorithm", p, why)
		}
	}
}

func TestPublicJWKSShape(t *testing.T) {
	_, ks, _ := mustMinter(t, 30*time.Second)
	raw, err := ks.PublicJWKS()
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "\"d\"") || strings.Contains(strings.ToLower(string(raw)), "private") {
		t.Fatalf("the public key set must carry no private material: %s", raw)
	}
	var set struct {
		Keys []map[string]string `json:"keys"`
	}
	if err := json.Unmarshal(raw, &set); err != nil || len(set.Keys) != 1 {
		t.Fatalf("%v %s", err, raw)
	}
	k := set.Keys[0]
	x, _ := base64.RawURLEncoding.DecodeString(k["x"])
	if k["kty"] != "OKP" || k["crv"] != "Ed25519" || k["use"] != "sig" || k["alg"] != "EdDSA" || k["kid"] == "" || len(x) != ed25519.PublicKeySize {
		t.Fatalf("jwk = %v", k)
	}
}

func TestMintFailsWithoutACurrentKey(t *testing.T) {
	dir, c, ks := newKeyset(t)
	m := NewMinter(ks, 30*time.Second, c.now)
	c.t = t0.Add(1000 * 24 * time.Hour)
	// The only key was never given a retirement, so it is still current; schedule one in the past to prove the error path.
	st := ks.Statuses()[0]
	raw, _ := json.Marshal(manifest{Keys: []Entry{{KID: st.KID, PublishAt: st.PublishAt, UseFrom: st.UseFrom, RetireAt: t0.Add(time.Hour).Unix()}}})
	if err := os.WriteFile(filepath.Join(dir, manifestName), raw, 0o600); err != nil {
		t.Fatal(err)
	}
	future := time.Now().Add(2 * time.Second)
	_ = os.Chtimes(filepath.Join(dir, manifestName), future, future)
	_ = ks.Reload()
	if _, err := m.Mint("t", "u", nil); err == nil {
		t.Fatal("minting with no current key must fail, not sign with a retired one")
	}
}

// TestGolden keeps testdata/auth-vectors/identity.json valid, and (with -update-golden) rewrites it. PR 2's Python verifier of the
// internal identity reads that file, so a format change here that the Python side has not followed fails there. The file holds a minted
// token and the public key set only: the key is generated for the run and discarded.
func TestGolden(t *testing.T) {
	path := filepath.Join("..", "..", "..", "..", "testdata", "auth-vectors", "identity.json")
	now := time.Unix(1_790_000_000, 0)
	if *updateGolden {
		_, priv, err := ed25519.GenerateKey(rand.Reader)
		if err != nil {
			t.Fatal(err)
		}
		dir := filepath.Join(t.TempDir(), "k")
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
		der, _ := x509.MarshalPKCS8PrivateKey(priv)
		if err := os.WriteFile(filepath.Join(dir, "golden.pem"), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), 0o600); err != nil {
			t.Fatal(err)
		}
		raw, _ := json.Marshal(manifest{Keys: []Entry{{KID: "golden", PublishAt: now.Unix() - 3600, UseFrom: now.Unix() - 3600}}})
		if err := os.WriteFile(filepath.Join(dir, manifestName), raw, 0o600); err != nil {
			t.Fatal(err)
		}
		ks, err := LoadKeyset(dir, false, func() time.Time { return now })
		if err != nil {
			t.Fatal(err)
		}
		token, err := NewMinter(ks, 30*time.Second, func() time.Time { return now }).Mint("ecorp", "user-1", []string{"api:chat"})
		if err != nil {
			t.Fatal(err)
		}
		jwks, _ := ks.PublicJWKS()
		doc, _ := json.MarshalIndent(map[string]any{
			"description": "A real internal identity minted by services/authlimit (go test ./internal/identity -update-golden), with the public key set that verifies it. No private key is kept. For PR 2's Python verifier (app/core/internal_identity.py).",
			"now":         now.Unix(), "issuer": Issuer, "audience": Audience, "typ": TokenType,
			"token": token, "jwks": json.RawMessage(jwks),
			"expect": map[string]any{"tenant": "ecorp", "principal": "user-1", "scope": "api:chat"},
		}, "", "  ")
		if err := os.WriteFile(path, append(doc, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Skipf("no golden file yet (run with -update-golden): %v", err)
	}
	var g struct {
		Now   int64           `json:"now"`
		Token string          `json:"token"`
		JWKS  json.RawMessage `json:"jwks"`
	}
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatal(err)
	}
	var set struct {
		Keys []struct {
			Kid string `json:"kid"`
			X   string `json:"x"`
		} `json:"keys"`
	}
	if err := json.Unmarshal(g.JWKS, &set); err != nil || len(set.Keys) == 0 {
		t.Fatalf("jwks: %v", err)
	}
	pub, _ := base64.RawURLEncoding.DecodeString(set.Keys[0].X)
	mc := jwt.MapClaims{}
	_, err = jwt.NewParser(jwt.WithValidMethods([]string{Algorithm}), jwt.WithIssuer(Issuer), jwt.WithAudience(Audience),
		jwt.WithTimeFunc(func() time.Time { return time.Unix(g.Now, 0) })).ParseWithClaims(g.Token, mc, func(*jwt.Token) (any, error) { return ed25519.PublicKey(pub), nil })
	if err != nil {
		t.Fatalf("the golden identity no longer verifies (regenerate with -update-golden if the format changed on purpose): %v", err)
	}
	if mc["tenant"] != "ecorp" || mc["sub"] != "user-1" {
		t.Fatalf("claims = %v", mc)
	}
}

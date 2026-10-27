package identity

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/golang-jwt/jwt/v5"
)

// The fixed parts of an internal identity (contracts/authlimit.md section B). They differ on purpose from everything about an external
// access token (issuer, audience, algorithm, `typ`, key set), so neither can ever be accepted as the other (threat T3).
const (
	Issuer    = "urn:agent-core:authlimit"
	Audience  = "agent-core-api"
	TokenType = "authlimit-identity+jwt"
	Algorithm = "EdDSA"

	// MaxIDBytes bounds the tenant and principal. They are copied into a request header and a Redis key; the external verifier refuses
	// longer ones (reason bad_claims) so this limit is a backstop, not the first line.
	MaxIDBytes = 256
)

// Minter turns a verified caller into an internal identity.
type Minter struct {
	keys *Keyset
	ttl  time.Duration
	now  func() time.Time
}

// NewMinter builds a minter. ttl is AUTHLIMIT_IDENTITY_TTL_SECONDS (1 to 120 s, enforced by config); `now` may be nil.
func NewMinter(keys *Keyset, ttl time.Duration, now func() time.Time) *Minter {
	if now == nil {
		now = time.Now
	}
	return &Minter{keys: keys, ttl: ttl, now: now}
}

// Mint signs an identity for a tenant and person the caller has ALREADY verified. It refuses an empty or oversized id, and a text with a
// control character, rather than sign something the API would have to guess about.
func (m *Minter) Mint(tenant, principal string, scopes []string) (string, error) {
	if err := checkID(tenant); err != nil {
		return "", errors.New("identity: bad tenant")
	}
	if err := checkID(principal); err != nil {
		return "", errors.New("identity: bad principal")
	}
	kid, key, err := m.keys.Current()
	if err != nil {
		return "", err
	}
	var jti [16]byte
	if _, err := rand.Read(jti[:]); err != nil {
		return "", err
	}
	now := m.now()
	claims := jwt.MapClaims{
		"iss":    Issuer,
		"aud":    Audience,
		"sub":    principal,
		"tenant": tenant,
		"scope":  strings.Join(scopes, " "),
		"jti":    base64.RawURLEncoding.EncodeToString(jti[:]),
		"iat":    now.Unix(),
		"nbf":    now.Unix(),
		"exp":    now.Add(m.ttl).Unix(),
	}
	tok := jwt.NewWithClaims(jwt.SigningMethodEdDSA, claims)
	tok.Header["kid"] = kid
	tok.Header["typ"] = TokenType
	return tok.SignedString(key)
}

func checkID(s string) error {
	if s == "" || len(s) > MaxIDBytes || !utf8.ValidString(s) {
		return errors.New("bad id")
	}
	for _, r := range s {
		if r < 0x20 || r == 0x7f {
			return errors.New("control character")
		}
	}
	return nil
}

// PublicJWKS is the document served at /internal/jwks.json: the Ed25519 public keys consumers accept now, as an OKP JWK Set (RFC 8037).
func (k *Keyset) PublicJWKS() ([]byte, error) {
	type jwk struct {
		Kty string `json:"kty"`
		Crv string `json:"crv"`
		X   string `json:"x"`
		Kid string `json:"kid"`
		Use string `json:"use"`
		Alg string `json:"alg"`
	}
	keys := []jwk{}
	for _, p := range k.Published() {
		keys = append(keys, jwk{
			Kty: "OKP", Crv: "Ed25519", X: base64.RawURLEncoding.EncodeToString(p.Key), Kid: p.KID, Use: "sig", Alg: Algorithm,
		})
	}
	return json.Marshal(map[string]any{"keys": keys})
}

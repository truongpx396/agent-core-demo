"""Generate the shared token vectors in testdata/auth-vectors/ (spec 018, FR-034, tasks T011).

Two verifiers in two languages must agree on what a valid token is: the Go service (`services/authlimit`) and the MCP gateway's Python
verifier (`app/mcp/gateway_auth.py`). Agreement that is checked by hand drifts, so both run ONE file of tokens with the outcome each must
reach. This script makes that file.

How the tokens are made, and why no secret is committed:

  * a throwaway RSA key is generated in memory, every token is signed once, and the key is DISCARDED. Only the public half is kept
    (`jwks.json`), plus the finished tokens. Nothing can mint a new token from this directory, and there is no private key for a secret
    scanner to find.
  * the clock is part of the file (`now`): a token's `exp` is relative to it, so no vector ever expires. A verifier under test is given
    that instant as its clock.
  * re-running this script makes a NEW set of tokens (new key); commit the result only when a vector is added or changed.

Three profiles, because the verifiers do different amounts of checking (see `README.md`):

  chat      the service on the chat API: signature, issuer, the API's audience, expiry, an issue time, a lifetime cap, a scope.
  identify  the service in limit-only mode on the MCP routes: signature, issuer and expiry only, to learn who is calling.
  mcp       the MCP gateway's own verifier: signature, issuer, ITS audience, expiry, a subject.

The rule that the `identify` expectations must obey, asserted at the bottom of this script: the limit-only profile never refuses a token
the MCP gateway accepts. If it did, that token would pass straight through to the gateway un-metered, escaping the limiter.
"""
import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

OUT = Path(__file__).resolve().parents[1] / "testdata" / "auth-vectors"

NOW = 1_790_000_000  # the injected clock: 2026-09-21T12:53:20Z
ISSUER = "https://auth.localhost:8443"
API_AUDIENCE = "https://api.localhost:8443"
MCP_AUDIENCE = "https://mcp.localhost:8443/ecorp/mcp"
SCOPE = "api:chat"
LEEWAY = 10
CAP = 168 * 3600  # 604800 s, the default AUTHLIMIT_MAX_TOKEN_LIFETIME
KID = "k1"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def public_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict:
    nums = key.public_key().public_numbers()
    size = (nums.n.bit_length() + 7) // 8
    return {
        "kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
        "n": b64(nums.n.to_bytes(size, "big")), "e": b64(nums.e.to_bytes((nums.e.bit_length() + 7) // 8, "big")),
    }


def claims(**over) -> dict:
    """A realistic Casdoor `JWT-Standard` payload for the chat API; `None` removes a claim."""
    base = {
        "iss": ISSUER, "aud": [API_AUDIENCE], "sub": "user-1", "owner": "ecorp", "name": "alice",
        "scope": f"{SCOPE} openid", "iat": NOW - 60, "nbf": NOW - 60, "exp": NOW - 60 + 3600,
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def main() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ed = ed25519.Ed25519PrivateKey.generate()
    public_pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    def sign(payload: dict, *, signer=None, kid: str | None = KID, alg: str = "RS256") -> str:
        headers = {"kid": kid} if kid else {}
        return jwt.encode(payload, signer or key, algorithm=alg, headers=headers)

    def valid(tenant: str = "ecorp", principal: str = "user-1") -> dict:
        return {"result": "valid", "tenant": tenant, "principal": principal}

    def refused(reason: str) -> dict:
        return {"result": "refused", "reason": reason}

    vectors: list[dict] = []

    def vec(name: str, token: str, chat: dict, identify: dict, mcp: dict, note: str) -> None:
        vectors.append({"name": name, "note": note, "token": token, "expect": {"chat": chat, "identify": identify, "mcp": mcp}})

    bad_aud = refused("bad_audience")

    # --- valid -------------------------------------------------------------------------------------------------------------
    vec("valid_chat", sign(claims()), valid(), valid(), bad_aud, "the ordinary case: a token for the chat API")
    vec("valid_audience_list", sign(claims(aud=[API_AUDIENCE, "https://other.example"])), valid(), valid(), bad_aud,
        "the audience is a list that contains the API's")
    vec("valid_at_the_lifetime_cap", sign(claims(exp=NOW - 60 + CAP)), valid(), valid(), bad_aud, "exp - iat is exactly the cap: allowed")
    vec("valid_expired_within_leeway", sign(claims(exp=NOW - 5)), valid(), valid(), bad_aud, "expired 5 s ago, inside the 10 s leeway")
    vec("valid_mcp", sign(claims(aud=[MCP_AUDIENCE], scope="ecorp:read")), refused("bad_audience"), valid(), valid(),
        "a token for an MCP endpoint: the chat API refuses its audience, limit-only mode and the MCP gateway accept it")
    vec("valid_mcp_without_issue_time", sign(claims(aud=[MCP_AUDIENCE], scope="ecorp:read", iat=None)), refused("bad_audience"), valid(), valid(),
        "no iat: the MCP gateway accepts it, so limit-only mode must too (it treats the issue time as 0: any revocation of the person voids it)")
    vec("valid_mcp_over_the_lifetime_cap", sign(claims(aud=[MCP_AUDIENCE], scope="ecorp:read", exp=NOW - 60 + CAP + 1)), refused("bad_audience"),
        valid(), valid(), "a lifetime over the cap: only the chat API enforces the cap, and limit-only mode must not refuse what the gateway accepts")

    # --- time --------------------------------------------------------------------------------------------------------------
    expired = refused("expired")
    vec("expired", sign(claims(exp=NOW - 11)), expired, expired, expired, "expired 11 s ago, outside the 10 s leeway")
    vec("not_yet_valid_nbf", sign(claims(nbf=NOW + 600)), refused("not_yet_valid"), refused("not_yet_valid"), refused("not_yet_valid"),
        "nbf is ten minutes ahead")
    vec("issued_in_the_future", sign(claims(iat=NOW + 600)), refused("not_yet_valid"), refused("not_yet_valid"), refused("not_yet_valid"),
        "iat is ten minutes ahead")

    # --- claims ------------------------------------------------------------------------------------------------------------
    vec("missing_exp", sign(claims(exp=None)), refused("bad_claims"), refused("bad_claims"), refused("bad_claims"), "a token that never expires")
    vec("missing_sub", sign(claims(sub=None)), refused("bad_claims"), refused("bad_claims"), refused("bad_claims"), "no subject")
    vec("missing_iat", sign(claims(iat=None)), refused("no_issue_time"), valid(), bad_aud,
        "no issue time: the chat API refuses it (the lifetime cap and revocation both need it); limit-only mode treats it as 0")
    vec("lifetime_over_the_cap", sign(claims(exp=NOW - 60 + CAP + 1)), refused("lifetime_too_long"), valid(), bad_aud,
        "exp - iat is one second over the cap")
    vec("tenant_not_text", sign(claims(owner=123)), refused("bad_claims"), refused("bad_claims"), bad_aud, "the tenant claim is a number")
    vec("tenant_empty", sign(claims(owner="")), refused("bad_claims"), refused("bad_claims"), bad_aud, "the tenant claim is empty")
    vec("tenant_missing", sign(claims(owner=None)), refused("bad_claims"), refused("bad_claims"), bad_aud, "no tenant claim")
    vec("principal_not_text", sign(claims(sub=123)), refused("bad_claims"), refused("bad_claims"), refused("bad_claims"),
        "the subject is a number (PyJWT itself refuses it)")

    # --- issuer, audience, scope -------------------------------------------------------------------------------------------
    vec("wrong_issuer", sign(claims(iss="https://evil.example")), refused("bad_issuer"), refused("bad_issuer"), refused("bad_issuer"),
        "a token from some other issuer, signed with our key")
    vec("wrong_audience", sign(claims(aud=["https://other.example"])), bad_aud, valid(), bad_aud, "a token for another resource")
    vec("client_id_as_audience", sign(claims(aud=["client-abc123"])), bad_aud, valid(), bad_aud,
        "what the authorization server mints when no `resource` was sent: the audience is the client id (spec 017)")
    vec("missing_scope", sign(claims(scope="openid")), refused("bad_scope"), valid(), bad_aud, "the audience is right, the scope is not")
    vec("scope_not_text", sign(claims(scope=5)), refused("bad_scope"), valid(), bad_aud, "the scope claim is a number")
    vec("scope_as_a_list", sign(claims(scope=[SCOPE])), refused("bad_scope"), valid(), bad_aud, "the scope claim is a list, not the space-separated string")
    vec("scope_is_a_prefix_only", sign(claims(scope="api:chatter")), refused("bad_scope"), valid(), bad_aud, "api:chatter must not satisfy api:chat")

    # --- signatures and algorithms -----------------------------------------------------------------------------------------
    body = claims()
    unsigned = f"{b64(json.dumps({'alg': 'none', 'typ': 'JWT', 'kid': KID}).encode())}.{b64(json.dumps(body).encode())}."
    hs_head = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    hs_body = b64(json.dumps(body).encode())
    hs_sig = b64(hmac.new(public_pem, f"{hs_head}.{hs_body}".encode(), hashlib.sha256).digest())  # the classic confusion: the PUBLIC key as the secret
    alg_refused = refused("bad_algorithm")
    vec("alg_none", unsigned, alg_refused, alg_refused, alg_refused, "alg none, no signature")
    vec("hs256_signed_with_the_public_key", f"{hs_head}.{hs_body}.{hs_sig}", alg_refused, alg_refused, alg_refused,
        "algorithm confusion: HMAC with the public key as the secret")
    vec("rs512", sign(claims(), alg="RS512"), alg_refused, alg_refused, alg_refused, "a valid RSA signature with an algorithm outside the allow-list")
    vec("internal_identity_as_an_external_token",
        jwt.encode({"iss": "urn:agent-core:authlimit", "aud": "agent-core-api", "sub": "user-1", "tenant": "ecorp", "iat": NOW - 5, "exp": NOW + 25,
                    "nbf": NOW - 5, "jti": "x"}, ed, algorithm="EdDSA", headers={"kid": "i1", "typ": "authlimit-identity+jwt"}),
        alg_refused, alg_refused, alg_refused, "the service's own identity format presented as an access token")
    bad_sig = refused("bad_signature")
    vec("unknown_kid", sign(claims(), kid="not-a-key"), bad_sig, bad_sig, bad_sig, "a kid that is not in the key set")
    vec("no_kid", sign(claims(), kid=None), bad_sig, bad_sig, bad_sig, "no kid in the header")
    vec("signed_by_a_foreign_key", sign(claims(), signer=foreign), bad_sig, bad_sig, bad_sig, "a valid-looking token signed by a key we do not trust, claiming our kid")
    good = sign(claims())
    head, _payload, signature = good.split(".")
    forged = b64(json.dumps(claims(owner="other-co")).encode())
    vec("tampered_payload", f"{head}.{forged}.{signature}", bad_sig, bad_sig, bad_sig, "the payload changed to another tenant, the original signature kept")

    # --- not a token -------------------------------------------------------------------------------------------------------
    malformed = refused("malformed")
    vec("not_a_jwt", "this-is-not-a-token", malformed, malformed, malformed, "no dots")
    vec("two_segments", "aaaa.bbbb", malformed, malformed, malformed, "two segments")
    vec("garbage_segments", "!!!.@@@.###", malformed, malformed, malformed, "three segments of invalid base64")
    vec("header_not_json", f"{b64(b'not json')}.{b64(b'{}')}.{b64(b'sig')}", malformed, malformed, malformed, "the header is not JSON")

    # --- the rule that keeps the limiter from being bypassed ---------------------------------------------------------------
    for v in vectors:
        if v["expect"]["mcp"]["result"] == "valid" and v["expect"]["identify"]["result"] != "valid":
            sys.exit(f"vector {v['name']}: the MCP gateway accepts it but limit-only mode refuses it, which would let it escape the limiter")
    names = [v["name"] for v in vectors]
    if len(names) != len(set(names)):
        sys.exit("duplicate vector names")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "jwks.json").write_text(json.dumps({"keys": [public_jwk(key, KID)]}, indent=2) + "\n")
    document = {
        "description": "Shared token vectors for the Go service and the MCP gateway verifier (spec 018). Generated by scripts/gen_auth_vectors.py; see README.md.",
        "generated_at": time.strftime("%Y-%m-%d", time.gmtime()),
        "now": NOW,
        "config": {
            "issuer": ISSUER, "audience": API_AUDIENCE, "mcp_audience": MCP_AUDIENCE, "required_scope": SCOPE,
            "tenant_claim": "owner", "principal_claim": "sub", "leeway_seconds": LEEWAY, "max_token_lifetime_seconds": CAP,
        },
        "vectors": vectors,
    }
    (OUT / "vectors.json").write_text(json.dumps(document, indent=2) + "\n")
    print(f"wrote {len(vectors)} vectors to {OUT} (the private key was never written)")


if __name__ == "__main__":
    main()

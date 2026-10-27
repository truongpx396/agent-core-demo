"""Deliberate defects in the authlimit service, one at a time: each must make a test fail (specs/018, SC-010). `make authlimit-mutants`.

Spec 017 ran this kind of list by hand ("every deliberate bug in the checks is caught") and wrote the result down. This keeps the list
runnable, because a claim that tests catch a defect is only as good as the next person's ability to check it. It edits the REAL source
files in place, runs the package's tests with the defect, and puts every file back (also on Ctrl-C), so run it on a clean tree.

A mutant that SURVIVES is a missing test: the tests pass with the defect present. One whose target text is not found exactly once means
the code moved since the list was written: update the list, do not skip it. A manual tier, like `make authlimit-smoke`: not part of CI
(it runs the Go tests ~30 times). Needs Go.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "services" / "authlimit"

# (name, file, old, new, package to test)
M = [
    ("no audience check", "internal/extjwt/verify.go", 'if p == Chat && !hasAudience(mc["aud"], v.s.Audience) {', 'if false && !hasAudience(mc["aud"], v.s.Audience) {', "./internal/extjwt"),
    ("no issuer check", "internal/extjwt/verify.go", 'if iss, _ := mc["iss"].(string); iss != v.s.Issuer {', 'if iss, _ := mc["iss"].(string); false && iss != v.s.Issuer {', "./internal/extjwt"),
    ("algorithm not pinned before the key is used", "internal/extjwt/verify.go", 'if alg, _ := h.Alg.(string); alg != allowedAlg {', 'if alg, _ := h.Alg.(string); false && alg != allowedAlg {', "./internal/extjwt"),
    ("no lifetime cap", "internal/extjwt/verify.go", 'if time.Duration(exp-iat)*time.Second > v.s.MaxLifetime {', 'if false && time.Duration(exp-iat)*time.Second > v.s.MaxLifetime {', "./internal/extjwt"),
    ("a missing issue time accepted on the chat API", "internal/extjwt/verify.go", 'if _, present := mc["iat"]; !present {\n\t\treturn Claims{}, reason.NoIssueTime', 'if _, present := mc["iat"]; !present && false {\n\t\treturn Claims{}, reason.NoIssueTime', "./internal/extjwt"),
    ("no scope check", "internal/extjwt/verify.go", 'if !contains(scopes, v.s.RequiredScope) {', 'if false && !contains(scopes, v.s.RequiredScope) {', "./internal/extjwt"),
    ("a huge clock leeway allowed", "internal/config/config.go", 'l.duration("AUTHLIMIT_CLOCK_LEEWAY", 10*time.Second, 0, 2*time.Minute)', 'l.duration("AUTHLIMIT_CLOCK_LEEWAY", 10*time.Second, 0, 1000*time.Hour)', "./internal/config"),
    ("an internal identity that may live a day", "internal/config/config.go", 'l.seconds("AUTHLIMIT_IDENTITY_TTL_SECONDS", 30, 1, 120)', 'l.seconds("AUTHLIMIT_IDENTITY_TTL_SECONDS", 30, 1, 86400)', "./internal/config"),
    ("limit-only mode refuses a token the MCP gateway accepts", "internal/extjwt/verify.go", 'if p == Identify {\n\t\treturn out, reason.None\n\t}', 'if p == Identify {\n\t\tif _, present := mc["iat"]; !present {\n\t\t\treturn Claims{}, reason.NoIssueTime\n\t\t}\n\t\treturn out, reason.None\n\t}', "./internal/extjwt"),
    ("an identity minted for 24 hours", "internal/identity/mint.go", '"exp":    now.Add(m.ttl).Unix(),', '"exp":    now.Add(24 * time.Hour).Unix(),', "./internal/identity"),
    ("a private key readable by others accepted", "internal/identity/keyset.go", 'if !k.loosePerms && info.Mode().Perm()&0o077 != 0 {', 'if false && !k.loosePerms && info.Mode().Perm()&0o077 != 0 {', "./internal/identity"),
    ("keys never go stale", "internal/extjwt/jwks.go", 'case age <= c.ttl+c.grace:', 'case true:', "./internal/extjwt"),
    ("key refresh not rate-limited", "internal/extjwt/jwks.go", 'if !last.IsZero() && c.now().Sub(last) < c.minRefresh {\n\t\treturn\n\t}', 'if false && !last.IsZero() && c.now().Sub(last) < c.minRefresh {\n\t\treturn\n\t}', "./internal/extjwt"),
    ("a refused person still charges the tenant's bucket", "internal/limiter/bucket.lua", '  -- Nothing is consumed, but the refilled amounts are stored below so the elapsed time is not lost.', '  if ttk >= cost then ttk = ttk - cost end', "./internal/limiter"),
    ("a dot segment is not refused (a path trick can reach public)", "internal/limiter/routes.go", 'if s == "" || s == "." || s == ".." {', 'if s == "" {', "./internal/limiter"),
    ("an encoded slash is not refused", "internal/limiter/routes.go", 'strings.Contains(lower, "%2f") || strings.Contains(lower, "%5c") ||', 'strings.Contains(lower, "%5c") ||', "./internal/limiter"),
    ("a stale revocation copy is trusted for ever", "internal/revoke/revoke.go", 'if !l.Fresh() && l.policy == Closed {', 'if false {', "./internal/revoke"),
    ("a token issued in the revocation second works", "internal/revoke/revoke.go", 'if ok && iat <= at {', 'if ok && iat < at {', "./internal/revoke"),
    ("error details not allow-listed", "internal/errenv/errenv.go", 'if allowedDetails[k] {', 'if true {', "./internal/errenv"),
    ("revocation checked AFTER the limiter", "internal/server/server.go",
     'if v := s.Revoked.Check(claims.Tenant, claims.Principal, claims.IssuedAt); v != revoke.OK {\n\t\ts.refuse(w, class, revocationReason(v), start) // BEFORE the limiter: a revoked person consumes nothing\n\t\treturn\n\t}\n\tif blocked := s.limit(ctx, w, class, claims, start); blocked {\n\t\treturn\n\t}',
     'if blocked := s.limit(ctx, w, class, claims, start); blocked {\n\t\treturn\n\t}\n\tif v := s.Revoked.Check(claims.Tenant, claims.Principal, claims.IssuedAt); v != revoke.OK {\n\t\ts.refuse(w, class, revocationReason(v), start)\n\t\treturn\n\t}', "./internal/server"),
    ("the limiter keyed on an unverified header", "internal/server/server.go", 'if blocked := s.limit(ctx, w, class, claims, start); blocked {\n\t\treturn\n\t}\n\tid, err', 'if blocked := s.limit(ctx, w, class, extjwt.Claims{Tenant: r.Header.Get("X-Tenant-Id"), Principal: claims.Principal}, start); blocked {\n\t\treturn\n\t}\n\tid, err', "./internal/server"),
    ("the limiter fails closed", "internal/server/server.go", 's.Log.Warn("limiter_store_failed", "route_class", string(class)) // fail open: serve; the counter and the alert are the signal\n\t\treturn false', 's.Log.Warn("limiter_store_failed", "route_class", string(class))\n\t\treturn true', "./internal/server"),
    ("read and cancel are limited too", "internal/server/server.go", 'if !limited {\n\t\treturn false // read, cancel: verified and never limited\n\t}', 'if !limited && false {\n\t\treturn false\n\t}', "./internal/server"),
    ("a duplicated X-Forwarded-Uri is trusted", "internal/server/server.go", 'len(m) == 1 && len(u) == 1 {', 'len(m) >= 1 && len(u) >= 1 {', "./internal/server"),
    ("MCP limit-only mode refuses an invalid token itself", "internal/server/server.go", 'claims, why := s.Verifier.Verify(ctx, token, extjwt.Identify)\n\tif why != reason.None {\n\t\ts.done(w, class, reason.Passthrough, why, start)', 'claims, why := s.Verifier.Verify(ctx, token, extjwt.Identify)\n\tif why != reason.None {\n\t\ts.refuse(w, class, why, start)', "./internal/server"),
    ("an unusable key set answers 401 about the token, not 503", "internal/extjwt/verify.go", 'case err != nil:\n\t\treturn Claims{}, reason.KeysUnavailable', 'case err != nil:\n\t\treturn Claims{}, reason.BadSignature', "./internal/server"),
]


def run() -> int:
    survivors: list[tuple[str, str]] = []
    for name, rel, old, new, pkg in M:
        path = ROOT / rel
        original = path.read_text()
        if original.count(old) != 1:
            print(f"STALE     {name}: the target text appears {original.count(old)} times in {rel}")
            survivors.append((name, "target text not found exactly once"))
            continue
        try:
            path.write_text(original.replace(old, new, 1))
            built = subprocess.run(["go", "build", "./..."], cwd=ROOT, capture_output=True, text=True, check=False)
            if built.returncode != 0:
                print(f"BROKEN    {name}: the mutant does not compile: {built.stderr.strip()[:120]}")
                survivors.append((name, "does not compile"))
                continue
            result = subprocess.run(["go", "test", "-count=1", pkg], cwd=ROOT, capture_output=True, text=True, timeout=300, check=False)
            if result.returncode == 0:
                print(f"SURVIVED  {name}   <-- a missing test")
                survivors.append((name, "SURVIVED"))
            else:
                failed = sorted({line.split()[2] for line in result.stdout.splitlines() if line.startswith("--- FAIL")})[:3]
                print(f"caught    {name}   [{', '.join(failed) or 'package failed'}]")
        finally:
            path.write_text(original)  # always: a half-mutated tree is the one failure this script must not cause

    print(f"\n{len(M) - len(survivors)} of {len(M)} mutants caught")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(run())

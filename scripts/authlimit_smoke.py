"""Does the front door work, end to end, the way a client sees it? `make authlimit-smoke` (specs/018).

It acts like a client: an OAuth authorization-code + PKCE sign-in as a PUBLIC client with the RFC 8707 `resource` parameter, through Caddy over
TLS signed by Caddy's local CA; then requests to the chat API's address and to the MCP routes. Behind the chat API's address is the STUB
upstream (`authlimit-echo`), which answers with every header it received, so each check can say what a real API would have been given.

Stages (default: the ones that change nothing; `--all` adds the ones that stop things):

  identity    a valid token yields an identity for that tenant and person, signed by the key in the key directory, and the external token
              does not reach the upstream (Q1)
  refusals    no token, a tampered token, a token for another audience or none, garbage: refused at the door, the upstream untouched (Q2)
  spoofing    forged identity and tenant headers, duplicated headers, forged X-Forwarded-*, and path tricks never change who the caller is
              or make a route public (Q3)
  limits      the person bucket, the tenant bucket, a colleague, another tenant, and cancel/read never limited (Q4)
  mcp         limit-only mode: no or bad tokens reach the MCP gateway's own 401; a valid token is metered (Q10)
  revoke      a revoked person is refused on every replica within seconds, on a chat route and an MCP route, and can sign in again (Q9)
  logs        no token and no key in any service's log (Q11)
  -- need --compose, and stop or restart things (the Makefile passes it; `--all` runs them):
  observe     observation mode serves and counts instead of refusing (Q5)
  replica     killing one authlimit replica under steady traffic fails (almost) nothing (Q6)
  rotate      rotating the internal signing key under traffic fails nothing (Q8)
  outage      Casdoor down: existing tokens work inside the grace, are refused beyond it, and service resumes (Q7)

It needs the stack up and bootstrapped (`make authlimit-up`, `make mcp-gateway-bootstrap`, `make mcp-gateway-ca`). It creates throwaway users and
deletes them however the run ends. A manual tier, like `make mcp-gateway-smoke`: it needs Docker services and is not part of CI. Exit status 0
only if every check passed.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import ssl
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization

CALLBACK_API = "http://localhost:33419/callback"  # registered for api-<tenant> by scripts/mcp_gateway_bootstrap.py
CALLBACK_MCP = "http://localhost:33418/callback"  # registered for mcp-<tenant>
IDENTITY_ISSUER = "urn:agent-core:authlimit"
IDENTITY_AUDIENCE = "agent-core-api"
SAFE = ["identity", "refusals", "spoofing", "limits", "mcp", "revoke", "logs"]
DESTRUCTIVE = ["observe", "replica", "rotate", "outage"]
NEEDS_COMPOSE = {"revoke", "logs", "observe", "replica", "outage"}  # rotate runs the image directly
FAILED: list[str] = []
SECRETS_SEEN: list[str] = []  # every token and refresh token this run obtained: the log scan looks for them


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(label)
    return ok


def info(text: str) -> None:
    print(f"      {text}")


def jwt_part(token: str, index: int) -> dict:
    part = token.split(".")[index]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


class Door:
    """Everything the checks need: the three addresses, an admin session for throwaway users, the key directory and the compose command."""

    def __init__(self, args: argparse.Namespace, tls: ssl.SSLContext, admin_password: str) -> None:
        self.args, self.tls, self.admin_password = args, tls, admin_password
        self.api, self.mcp, self.auth = args.api.rstrip("/"), args.mcp.rstrip("/"), args.auth.rstrip("/")
        self.http = httpx.AsyncClient(verify=tls, timeout=20.0)
        self.admin = httpx.AsyncClient(base_url=self.auth, verify=tls, timeout=20.0)
        self.users: list[tuple[str, str]] = []
        self.passwords: dict[tuple[str, str], str] = {}
        self.apps: dict[str, dict] = {}

    # -- Casdoor ---------------------------------------------------------------------------------------------------------------------

    async def start(self) -> None:
        reply = (
            await self.admin.post(
                "/api/login",
                json={"application": "app-built-in", "organization": "built-in", "username": "admin", "password": self.admin_password, "type": "login", "autoSignin": True},
            )
        ).json()
        if reply.get("status") != "ok":
            sys.exit(f"admin login failed: {reply.get('msg')} (stopping rather than retrying: Casdoor locks an account after five failures)")

    async def cleanup(self) -> None:
        for organization, name in self.users:
            try:
                await self.admin.post("/api/delete-user", json={"owner": organization, "name": name})
            except httpx.HTTPError as exc:
                print(f"      could not delete {organization}/{name}: {exc.__class__.__name__}")
        await self.http.aclose()
        await self.admin.aclose()

    async def user(self, organization: str) -> tuple[str, str]:
        name, password = f"smoke-{secrets.token_hex(3)}", secrets.token_urlsafe(16)
        reply = (
            await self.admin.post(
                "/api/add-user",
                json={"owner": organization, "name": name, "displayName": name, "password": password, "email": f"{name}@{organization}.localhost", "type": "normal-user"},
            )
        ).json()
        if reply.get("status") != "ok":
            sys.exit(f"could not create a user in {organization}: {reply.get('msg')}")
        self.users.append((organization, name))
        self.passwords[(organization, name)] = password
        return organization, name

    async def application(self, name: str) -> dict:
        if name not in self.apps:
            data = (await self.admin.get("/api/get-application", params={"id": f"admin/{name}"})).json().get("data")
            if not data:
                sys.exit(f"no Casdoor application {name!r}: run make mcp-gateway-bootstrap first")
            self.apps[name] = data
        return self.apps[name]

    async def token(self, user: tuple[str, str], *, kind: str = "api", resource: str | None = "default", scope: str | None = None) -> str | None:
        """Sign in as a public client and exchange the code. kind is api or mcp; resource "default" means the kind's own address, None sends none."""
        organization, name = user
        application = f"{kind}-{organization}"
        app = await self.application(application)
        callback = CALLBACK_API if kind == "api" else CALLBACK_MCP
        if resource == "default":
            resource = self.api if kind == "api" else f"{self.mcp}/ecorp/mcp"
        scope = scope or ("api:chat" if kind == "api" else "ecorp:read")
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        query = {"clientId": app["clientId"], "responseType": "code", "redirectUri": callback, "scope": scope, "state": "s", "nonce": "",
                 "code_challenge_method": "S256", "code_challenge": challenge}
        if resource:
            query["resource"] = resource
        try:
            async with httpx.AsyncClient(base_url=self.auth, verify=self.tls, timeout=20.0) as client:
                reply = (await client.post("/api/login?" + urllib.parse.urlencode(query),
                                           json={"application": application, "organization": organization, "username": name,
                                                 "password": self.passwords[user], "type": "code", "autoSignin": True})).json()
                if reply.get("status") != "ok":
                    return None
                form = {"grant_type": "authorization_code", "client_id": app["clientId"], "code": reply["data"], "redirect_uri": callback, "code_verifier": verifier}
                if resource:
                    form["resource"] = resource
                reply = (await client.post("/api/login/oauth/access_token", data=form)).json()
        except (httpx.HTTPError, ValueError):
            return None  # the authorization server (or Caddy in front of it) is down or answered something that is not JSON: no token
        for key in ("access_token", "refresh_token"):
            if reply.get(key):
                SECRETS_SEEN.append(reply[key])
        return reply.get("access_token")

    # -- requests --------------------------------------------------------------------------------------------------------------------

    async def call(self, method: str, path: str, token: str | None, *, base: str | None = None, extra: list[tuple[str, str]] | None = None,
                   content: bytes | None = None) -> httpx.Response:
        headers: list[tuple[str, str]] = [("Accept", "application/json, text/event-stream")]
        if token:
            headers.append(("Authorization", f"Bearer {token}"))
        headers += extra or []
        return await self.http.request(method, (base or self.api) + path, headers=headers, content=content)

    async def raw(self, target: str, headers: list[tuple[str, str]] | None = None, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
        """One HTTP/1.1 request with the request target sent EXACTLY as given. A client library normalises `..` and `//` before sending, which
        would hide the thing under test."""
        host = urllib.parse.urlsplit(self.api)
        reader, writer = await asyncio.open_connection(host.hostname, host.port or 443, ssl=self.tls, server_hostname=host.hostname)
        lines = [f"{method} {target} HTTP/1.1", f"Host: {host.netloc}", "Connection: close"] + [f"{k}: {v}" for k, v in (headers or [])]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        data = await reader.read()
        writer.close()
        head, _, body = data.partition(b"\r\n\r\n")
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        parsed = {k.lower(): v for k, _, v in (h.partition(": ") for h in header_lines)}
        return int(status_line.split()[1]), parsed, body

    # -- the compose command (destructive stages) -----------------------------------------------------------------------------------

    def compose(self, tail: str, env: dict[str, str] | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
        """Run the stack's compose command with `tail` appended. No shell: --compose is split into an argument list, and the variables a
        stage wants to change for ONE restart go in `env` (the Makefile sets the standing ones, AUTHLIMIT_UID and the key directory, on this
        process, which its children inherit)."""
        if not self.args.compose:
            sys.exit("this stage needs --compose (the Makefile passes it)")
        argv = shlex.split(self.args.compose) + shlex.split(tail)
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, env={**os.environ, **(env or {})})  # noqa: S603 - the developer's own compose command, passed by the Makefile, never a shell string

    async def wait_ready(self, seconds: int = 90) -> bool:
        """Until a valid request through the door works again (both replicas back and Caddy has them)."""
        user = await self.user("ecorp")
        deadline = time.time() + seconds
        while time.time() < deadline:
            token = await self.token(user)
            if token:
                try:
                    if (await self.call("GET", "/chat/sessions", token)).status_code == 200:
                        return True
                except httpx.HTTPError:
                    pass
            await asyncio.sleep(2)
        return False


# ---------------------------------------------------------------------------------------------------------------------------------
# helpers on what the stub upstream reports


def echoed(response: httpx.Response) -> dict | None:
    """The stub's JSON (it lists the headers it received), or None when the answer is not from the stub (a refusal, an envelope)."""
    if response.headers.get("content-type", "").startswith("application/json"):
        body = response.json()
        if isinstance(body, dict) and "headers" in body:
            return body
    return None


def header_of(echo: dict, name: str) -> list[str]:
    return [v for k, vs in echo["headers"].items() if k.lower() == name.lower() for v in vs]


def public_keys(keys_dir: Path) -> dict[str, Any]:
    """kid -> public key, from the private keys in the key directory (the smoke runs as the user who owns them)."""
    out = {}
    for pem in keys_dir.glob("*.pem"):
        out[pem.stem] = serialization.load_pem_private_key(pem.read_bytes(), None).public_key()
    return out


def verify_identity(identity: str, keys: dict[str, Any]) -> dict | None:
    try:
        kid = jwt.get_unverified_header(identity).get("kid")
        if not isinstance(kid, str):
            return None
        return jwt.decode(identity, keys[kid], algorithms=["EdDSA"], audience=IDENTITY_AUDIENCE, issuer=IDENTITY_ISSUER)
    except (jwt.PyJWTError, KeyError):
        return None


# ---------------------------------------------------------------------------------------------------------------------------------
# the stages


async def stage_identity(d: Door, keys: dict[str, Any]) -> None:
    print("\n== identity: a valid token gets an identity, and the external token stays at the door")
    user = await d.user("ecorp")
    token = await d.token(user)
    check("sign-in as a public client with the API's resource and scope", token is not None)
    if token is None:
        return
    claims = jwt_part(token, 1)
    response = await d.call("GET", "/chat/sessions", token)
    echo = echoed(response)
    check("a valid token reaches the upstream", response.status_code == 200 and echo is not None, f"status {response.status_code}: {response.text[:200]}")
    if echo is None:
        return
    ids = header_of(echo, "X-Internal-Identity")
    check("the upstream received exactly one X-Internal-Identity", len(ids) == 1, str(ids))
    identity = verify_identity(ids[0], keys) if ids else None
    check("the identity verifies against the key in the key directory (EdDSA, our issuer and audience)", identity is not None)
    if identity:
        check("it names the token's tenant and person", identity.get("tenant") == claims["owner"] and identity.get("sub") == claims["sub"],
              f"{identity.get('tenant')}/{identity.get('sub')} vs {claims['owner']}/{claims['sub']}")
        check("its scope is the token's", "api:chat" in identity.get("scope", ""))
        check("it lives 30 seconds", identity["exp"] - identity["iat"] == 30, str(identity["exp"] - identity["iat"]))
        head = jwt.get_unverified_header(ids[0])
        check("its header names the key and the type", head.get("typ") == "authlimit-identity+jwt" and head.get("kid") in keys, str(head))
    check("the caller's Authorization header was removed before the upstream", header_of(echo, "Authorization") == [], str(header_of(echo, "Authorization")))
    info(f"token: {claims['exp'] - claims['iat']} s lifetime, aud {claims['aud']}, scope {claims['scope']!r}, jti {'present' if 'jti' in claims else 'absent'}")
    for method, path in [("POST", "/chat/cancel"), ("GET", "/usage"), ("GET", "/chat/sessions/t1/messages")]:
        r = await d.call(method, path, token)
        check(f"{method} {path} is served (verified, not limited)", r.status_code == 200 and echoed(r) is not None, str(r.status_code))
    for path in ["/", "/health", "/health/ready", "/static/x.js", "/billing/webhooks/polar"]:
        r = await d.call("GET", path, None)
        e = echoed(r)
        check(f"public: GET {path} needs no token", r.status_code == 200 and e is not None and header_of(e, "X-Internal-Identity") == [], str(r.status_code))
    r = await d.call("OPTIONS", "/chat/stream/queued", None)
    check("a CORS preflight needs no token", r.status_code == 200, str(r.status_code))


async def stage_refusals(d: Door) -> None:
    print("\n== refusals: nothing invalid reaches the upstream")
    user = await d.user("ecorp")
    good = await d.token(user)
    no_resource = await d.token(user, resource=None)
    wrong_resource = await d.token(user, resource="https://elsewhere.example")
    mcp = await d.token(user, kind="mcp")
    head, payload, sig = (good or "a.b.c").split(".")
    flipped = sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB")
    cases = {
        "no token": (None, 401, "unauthenticated"),
        "garbage": ("not-a-token", 401, "unauthenticated"),
        "a tampered signature": (f"{head}.{payload}.{flipped}", 401, "unauthenticated"),
        "a token with no `resource` (audience is the client id)": (no_resource, 401, "unauthenticated"),
        "a token for another resource": (wrong_resource, 401, "unauthenticated"),
        "a token for an MCP endpoint": (mcp, 401, "unauthenticated"),
    }
    for label, (token, status, code) in cases.items():
        r = await d.call("POST", "/chat/stream/queued", token)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        ok = r.status_code == status and body.get("code") == code and "headers" not in body and "WWW-Authenticate" in r.headers
        check(f"{label}: {status} {code}, the upstream untouched", ok, f"{r.status_code} {r.text[:160]}")
    r = await d.call("POST", "/chat/stream/queued", None, extra=[("Authorization", "Basic dXNlcjpwYXNz")])
    check("another authorization scheme: 401", r.status_code == 401, str(r.status_code))
    if good:
        r = await d.call("POST", "/chat/stream/queued", None, extra=[("Authorization", f"Bearer {good}"), ("Authorization", f"Bearer {good}")])
        check("two Authorization headers: refused, not guessed at", r.status_code == 401, str(r.status_code))
    check("401 carries a WWW-Authenticate challenge naming the realm", "agent-core-api" in (await d.call("GET", "/usage", None)).headers.get("www-authenticate", ""))


async def stage_spoofing(d: Door, keys: dict[str, Any]) -> None:
    print("\n== spoofing: nothing the client sends changes who it is, or makes a route public")
    user = await d.user("ecorp")
    token = await d.token(user)
    if token is None:
        check("sign-in", False)
        return
    claims = jwt_part(token, 1)
    forged_headers = [
        ("X-Tenant-Id", "other-co"), ("X-Principal-Id", "mallory"), ("X-Internal-Identity", "forged-1"), ("X-Internal-Identity", "forged-2"),
        ("x-internal-identity", "forged-3"), ("X-Forwarded-Uri", "/health"), ("X-Forwarded-Method", "OPTIONS"), ("X-Forwarded-For", "10.0.0.1"),
    ]
    r = await d.call("GET", "/chat/sessions", token, extra=forged_headers)
    echo = echoed(r)
    check("a valid token with eight forged headers is still served", r.status_code == 200 and echo is not None, f"{r.status_code} {r.text[:160]}")
    if echo is not None:
        ids = header_of(echo, "X-Internal-Identity")
        identity = verify_identity(ids[0], keys) if len(ids) == 1 else None
        check("exactly one identity arrives and it is the real one", identity is not None and identity["tenant"] == claims["owner"] and identity["sub"] == claims["sub"], str(ids))
        check("the door removed the client's own X-Tenant-Id and X-Principal-Id (so an API flipped to header mode behind it would not trust them)",
              header_of(echo, "X-Tenant-Id") == [] and header_of(echo, "X-Principal-Id") == [], f"{header_of(echo, 'X-Tenant-Id')} {header_of(echo, 'X-Principal-Id')}")
    # The forged X-Forwarded-* must not decide the class: Caddy replaces them in the check it sends.
    for label, extra in {
        "a forged X-Forwarded-Uri: /health": [("X-Forwarded-Uri", "/health")],
        "a forged X-Forwarded-Method: OPTIONS": [("X-Forwarded-Method", "OPTIONS")],
        "a forged X-Internal-Identity": [("X-Internal-Identity", "forged")],
        "forged tenant and principal headers": [("X-Tenant-Id", "ecorp"), ("X-Principal-Id", "admin")],
    }.items():
        r = await d.call("POST", "/chat/stream/queued", None, extra=extra)
        check(f"without a token, {label}: still 401", r.status_code == 401 and echoed(r) is None, f"{r.status_code} {r.text[:120]}")
    # Path tricks: sent byte for byte. A route must never become public because a prefix looks public.
    tricks = ["/static/../chat/stream/queued", "/static/%2e%2e/chat/stream/queued", "/static/..%2fchat/stream/queued", "/health/../chat/resume",
              "//chat/stream/queued", "/chat/./stream/queued", "/static/a%5c..%5cchat/resume", "/billing/webhooks/../../chat/resume"]
    for target in tricks:
        status, headers, body = await d.raw(target, method="POST")
        reached = b'"headers"' in body and status == 200
        check(f"path trick {target!r} does not reach the upstream unauthenticated", not reached, f"{status} {body[:100]!r}")


async def stage_limits(d: Door) -> None:
    print("\n== limits: the person bucket, the tenant bucket, and who is never limited")
    # The tenant bucket belongs to the TENANT, not to this run: a previous run (or the observe stage) may have emptied it, and it refills at
    # 0.5 token a second, so 10 tokens take 20 s. Wait it out, or the exact counts below depend on how recently the smoke last ran.
    info("waiting 22 s for the tenant buckets to refill after any earlier run")
    await asyncio.sleep(22)
    a1, a2, a3 = await d.user("ecorp"), await d.user("ecorp"), await d.user("ecorp")
    b = await d.user("other-co")
    t1, t2, t3, tb = await d.token(a1), await d.token(a2), await d.token(a3), await d.token(b)
    if t1 is None or t2 is None or t3 is None or tb is None:
        check("sign-in of four users", False)
        return

    async def turn(token: str) -> httpx.Response:
        return await d.call("POST", "/chat/stream/queued", token)

    first = [await turn(t1) for _ in range(6)]  # the person bucket holds 5
    codes = [r.status_code for r in first]
    check("a person's first 5 turn requests are served and the 6th is limited", codes == [200] * 5 + [429], str(codes))
    limited = first[-1]
    body = limited.json() if limited.status_code == 429 else {}
    check("the 429 is the envelope with a Retry-After and no upstream data",
          body.get("code") == "rate_limited" and int(limited.headers.get("retry-after", "0")) >= 1 and "headers" not in body, f"{limited.headers.get('retry-after')} {limited.text[:120]}")
    check("a limited person can still cancel a run and read (never limited)",
          (await d.call("POST", "/chat/cancel", t1)).status_code == 200 and (await d.call("GET", "/chat/sessions", t1)).status_code == 200)
    second = [(await turn(t2)).status_code for _ in range(5)]
    check("a colleague has their own bucket (the refused 6th request did not charge the tenant)", second == [200] * 5, str(second))
    third = await turn(t3)
    check("the tenant bucket (capacity 10) is now empty: a third person is limited though their own bucket is full", third.status_code == 429 and third.json().get("code") == "rate_limited",
          f"{third.status_code} {third.text[:120]}")
    check("another tenant is unaffected", (await turn(tb)).status_code == 200)
    wait = int(limited.headers.get("retry-after", "2"))
    await asyncio.sleep(wait + 1)
    check(f"after the advertised Retry-After ({wait} s) the person is served again", (await turn(t1)).status_code == 200)


async def stage_mcp(d: Door) -> None:
    print("\n== mcp: limit-only mode; the MCP gateway keeps answering its own 401")
    user = await d.user("ecorp")
    token = await d.token(user, kind="mcp")
    if not token:
        check("sign-in for the MCP application", False)
        return
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}}}).encode()
    extra = [("Content-Type", "application/json")]
    r = await d.call("POST", "/ecorp/mcp", None, base=d.mcp, extra=extra, content=body)
    challenge = r.headers.get("www-authenticate", "")
    check("no token: the MCP gateway's own 401 with its discovery metadata (not authlimit's envelope)",
          r.status_code == 401 and "resource_metadata" in challenge and "agent-core-api" not in challenge, f"{r.status_code} {challenge[:160]}")
    r = await d.call("POST", "/ecorp/mcp", "not-a-token", base=d.mcp, extra=extra, content=body)
    check("a bad token: also the gateway's 401", r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", ""), str(r.status_code))
    r = await d.call("POST", "/ecorp/mcp", token, base=d.mcp, extra=extra, content=body)
    check("a valid token passes through to the MCP gateway", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    meta = await d.call("GET", "/.well-known/oauth-protected-resource/ecorp/mcp", None, base=d.mcp)
    check("the discovery document needs no token", meta.status_code == 200, str(meta.status_code))
    responses = []
    for _ in range(4):  # in bursts, to stay inside one refill window
        responses += await asyncio.gather(*[d.call("POST", "/ecorp/mcp", token, base=d.mcp, extra=extra, content=body) for _ in range(20)])
    codes = [x.status_code for x in responses]
    limited = [x for x in responses if x.status_code == 429]
    check("a burst beyond the person's MCP bucket (30) is limited", len(limited) > 0 and 200 in codes, f"{codes.count(200)} ok, {len(limited)} limited")
    if limited:
        envelope = limited[0].json()
        check("a limited MCP request is authlimit's envelope and names the wait",
              envelope.get("code") == "rate_limited" and int(limited[0].headers.get("retry-after", "0")) >= 1, f"{limited[0].text[:120]}")
    info(f"{len(codes)} requests: {codes.count(200)} served, {len(limited)} limited")


async def stage_revoke(d: Door) -> None:
    print("\n== revoke: a revoked person is refused everywhere within seconds, and can sign in again")
    user = await d.user("ecorp")
    api_token, mcp_token = await d.token(user), await d.token(user, kind="mcp")
    if not (api_token and mcp_token):
        check("sign-in", False)
        return
    sub = jwt_part(api_token, 1)["sub"]
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    extra = [("Content-Type", "application/json")]
    check("before revocation the API accepts the token", (await d.call("GET", "/chat/sessions", api_token)).status_code == 200)
    revoked_at = time.time()
    result = d.compose(f"exec -T --index 1 authlimit /authlimit revoke --tenant ecorp --sub {shlex.quote(sub)}")
    if not check("`authlimit revoke` ran", result.returncode == 0, (result.stdout + result.stderr)[-300:]):
        return
    first_refusal = everywhere = None
    streak = 0
    while time.time() - revoked_at < 12:
        r = await d.call("GET", "/chat/sessions", api_token)
        now = time.time() - revoked_at
        if r.status_code == 401:
            first_refusal = first_refusal or now
            streak += 1
            if streak >= 20 and everywhere is None:  # 20 in a row: both replicas have been asked, with overwhelming probability
                everywhere = now
                break
        else:
            streak = 0
        await asyncio.sleep(0.1)
    check("refused on every replica within 5 seconds", everywhere is not None and everywhere <= 5.0, f"first refusal at {first_refusal}, everywhere at {everywhere}")
    info(f"first refusal after {first_refusal and round(first_refusal, 2)} s, on every replica after {everywhere and round(everywhere, 2)} s")
    r = await d.call("POST", "/ecorp/mcp", mcp_token, base=d.mcp, extra=extra, content=body)
    challenge = r.headers.get("www-authenticate", "")
    check("the same person is refused on an MCP route, with the discovery URL",
          r.status_code == 401 and 'error="invalid_token"' in challenge and "/.well-known/oauth-protected-resource/ecorp/mcp" in challenge, f"{r.status_code} {challenge[:200]}")
    await asyncio.sleep(max(0.0, 1.2 - (time.time() - revoked_at)))
    fresh = await d.token(user)
    check("a new sign-in after the revocation works", bool(fresh) and (await d.call("GET", "/chat/sessions", fresh)).status_code == 200)
    check("while the old token stays refused", (await d.call("GET", "/chat/sessions", api_token)).status_code == 401)


async def stage_logs(d: Door, keys_dir: Path) -> None:
    print("\n== logs: no token and no key in any service's log")
    result = d.compose("logs --no-color authlimit authlimit-echo caddy-gateway casdoor mcp-gateway")
    text = result.stdout + result.stderr
    check("the logs were collected", len(text) > 200, f"{len(text)} bytes")
    leaked = [t[:12] + "…" for t in set(SECRETS_SEEN) if t and t in text]
    check(f"none of the {len(set(SECRETS_SEEN))} tokens this run obtained appears in any log", not leaked, str(leaked))
    pem_bodies = []
    for pem in keys_dir.glob("*.pem"):
        pem_bodies += [line for line in pem.read_text().splitlines() if len(line) > 20 and not line.startswith("-----")]
    check("no private key material appears in any log", not any(b in text for b in pem_bodies) and "BEGIN PRIVATE KEY" not in text)
    check("no JWT-shaped string is logged by authlimit", not re.search(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.", "\n".join(l for l in text.splitlines() if "authlimit-" in l and "casdoor" not in l)))
    errors = [l for l in text.splitlines() if "authlimit-" in l and '"level":"ERROR"' in l]
    check("authlimit logged no errors", not errors, "\n".join(errors[:3]))


async def stage_observe(d: Door) -> None:
    print("\n== observe: the limiter counts what it would have refused, and serves")
    result = d.compose("up -d --no-deps authlimit", env={"AUTHLIMIT_LIMIT_MODE": "observe"})
    check("authlimit restarted in observe mode", result.returncode == 0, result.stderr[-300:])
    if not await d.wait_ready():
        check("the door came back", False)
        return
    user = await d.user("ecorp")
    token = await d.token(user)
    codes = [(await d.call("POST", "/chat/stream/queued", token)).status_code for _ in range(10)]
    check("10 turns from one person, past the bucket of 5: all served", codes == [200] * 10, str(codes))
    logs = d.compose("logs --no-color authlimit").stdout
    check("and the would-be refusals were logged", logs.count('"msg":"would_limit"') >= 4, f"{logs.count(chr(34) + 'would_limit' + chr(34))} lines")
    result = d.compose("up -d --no-deps authlimit", env={"AUTHLIMIT_LIMIT_MODE": "enforce"})
    check("restored to enforce mode", result.returncode == 0 and await d.wait_ready(), result.stderr[-300:])


async def traffic(d: Door, token: str, seconds: float, per_second: float, started: list[float]) -> list[tuple[float, int]]:
    """Steady GETs on a never-limited route; returns (seconds since start, status) for each."""
    out: list[tuple[float, int]] = []
    t0 = time.time()
    started.append(t0)
    while time.time() - t0 < seconds:
        try:
            status = (await d.call("GET", "/chat/sessions", token)).status_code
        except httpx.HTTPError:
            status = 0
        out.append((time.time() - t0, status))
        await asyncio.sleep(1 / per_second)
    return out


async def stage_replica(d: Door) -> None:
    print("\n== replica: killing one authlimit replica under steady traffic")
    user = await d.user("ecorp")
    token = await d.token(user)
    if token is None:
        check("sign-in", False)
        return
    ids = d.compose("ps -q authlimit").stdout.split()
    if not check("two replicas are running", len(ids) == 2, str(ids)):
        return
    started: list[float] = []
    task = asyncio.create_task(traffic(d, token, 14, 20, started))
    await asyncio.sleep(4)
    killed = subprocess.run(["docker", "kill", ids[0]], capture_output=True, text=True, check=False)  # noqa: S603,S607 - a container id from our own compose project
    check("one replica was killed", killed.returncode == 0, killed.stderr[-200:])
    results = await task
    bad = [(round(t, 1), s) for t, s in results if s != 200]
    check(f"traffic through the kill: {len(results)} requests, {len(bad)} failed", len(bad) <= 2, f"{bad[:8]}")
    info(f"failures (seconds since start, status): {bad}")
    slow = max((b - a for (a, _), (b, _) in zip(results, results[1:], strict=False)), default=0.0)
    info(f"the longest gap between two answers was {round(slow, 2)} s (a request that picked the dead replica waits for a dial timeout)")
    check("no request waited longer than 2 seconds on the dead replica", slow <= 2.5, f"{round(slow, 2)} s")
    d.compose("up -d --no-deps authlimit")
    check("the replica was brought back", await d.wait_ready() and len(d.compose("ps -q authlimit").stdout.split()) == 2)


async def stage_rotate(d: Door, keys: dict[str, Any], keys_dir: Path) -> None:
    print("\n== rotate: a new internal signing key under traffic")
    image = d.args.image
    user = await d.user("ecorp")
    token = await d.token(user)
    kids: list[str] = []
    failures: list[str] = []
    started = time.time()
    rotated = False
    while time.time() - started < 50:
        if not rotated and time.time() - started > 3:
            rotated = True
            run = subprocess.run(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{keys_dir.parent}:/work", image, "keys", "rotate", "--dir", f"/work/{keys_dir.name}", "--overlap", "20s"],  # noqa: S603,S607 - our own image and key directory
                                 capture_output=True, text=True, check=False)
            if not check("`authlimit keys rotate --overlap 20s` ran", run.returncode == 0, run.stderr[-300:]):
                return
        r = await d.call("GET", "/chat/sessions", token)
        echo = echoed(r)
        ids = header_of(echo, "X-Internal-Identity") if echo else []
        if r.status_code != 200 or len(ids) != 1:
            failures.append(f"{round(time.time() - started, 1)}s: {r.status_code}")
        else:
            current = {p.stem: serialization.load_pem_private_key(p.read_bytes(), None).public_key() for p in keys_dir.glob("*.pem")}
            if verify_identity(ids[0], current) is None:
                failures.append(f"{round(time.time() - started, 1)}s: an identity did not verify against the published keys")
            kids.append(jwt.get_unverified_header(ids[0])["kid"])
        await asyncio.sleep(0.1)
    check(f"no request failed through the rotation ({len(kids)} requests)", not failures, str(failures[:5]))
    distinct = list(dict.fromkeys(kids))
    check("identities were signed by the old key, then by the new one", len(distinct) == 2 and kids[0] == distinct[0] and kids[-1] == distinct[1], str(distinct))
    if len(distinct) == 2:
        switch = kids.index(distinct[1])
        info(f"the new key signed from request {switch} of {len(kids)} (about {round(switch / max(1, len(kids)) * 50 - 3, 1)} s after the rotation was scheduled)")


async def stage_outage(d: Door) -> None:
    print("\n== outage: Casdoor down, inside and beyond the grace")
    env = {"AUTHLIMIT_JWKS_CACHE_SECONDS": "5", "AUTHLIMIT_JWKS_MIN_REFRESH_SECONDS": "1", "AUTHLIMIT_JWKS_STALE_GRACE_SECONDS": "15"}
    result = d.compose("up -d --no-deps authlimit", env=env)
    if not check("authlimit restarted with a 5 s key cache and a 15 s grace", result.returncode == 0 and await d.wait_ready(), result.stderr[-300:]):
        return
    user = await d.user("ecorp")
    token = await d.token(user)
    check("a valid request is served", (await d.call("GET", "/chat/sessions", token)).status_code == 200)
    stopped = time.time()
    d.compose("stop casdoor")
    try:
        await asyncio.sleep(8)  # past the 5 s TTL, inside the 15 s grace
        r = await d.call("GET", "/chat/sessions", token)
        check("Casdoor down, inside the grace: the existing token is still served from the last keys", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        sign_in = await d.token(user)
        check("but a NEW sign-in fails (the authorization server is down)", sign_in is None)
        await asyncio.sleep(max(0.0, 26 - (time.time() - stopped)))  # beyond TTL + grace = 20 s
        r = await d.call("GET", "/chat/sessions", token)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        check("beyond the grace: refused, never served", r.status_code in (502, 503) and "headers" not in body, f"{r.status_code} {r.text[:160]}")
        if r.status_code == 503 and body.get("code"):
            check("with the auth_unavailable envelope and a Retry-After", body.get("code") == "auth_unavailable" and "retry-after" in r.headers)
        started = time.time()
        d.compose("start casdoor")
        recovered = None
        while time.time() - started < 90:
            fresh = await d.token(user)
            if fresh:
                r = await d.call("GET", "/chat/sessions", fresh)
                if r.status_code == 200:
                    recovered = time.time() - started
                    break
            await asyncio.sleep(2)
        check("service resumed after Casdoor returned", recovered is not None, "never recovered within 90 s")
        if recovered:
            info(f"resumed {round(recovered, 1)} s after Casdoor was started")
    finally:
        d.compose("start casdoor")  # however the checks went, leave the stack as it was found
    d.compose("up -d --no-deps authlimit", env={"AUTHLIMIT_JWKS_CACHE_SECONDS": "300", "AUTHLIMIT_JWKS_MIN_REFRESH_SECONDS": "30", "AUTHLIMIT_JWKS_STALE_GRACE_SECONDS": "21600"})
    check("defaults restored", await d.wait_ready())


# ---------------------------------------------------------------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> None:
    password = os.environ.get("CASDOOR_ADMIN_PASSWORD", "")
    if not password:
        sys.exit("CASDOOR_ADMIN_PASSWORD is not set (make authlimit-smoke passes it)")
    stages = SAFE + (DESTRUCTIVE if args.all else []) if not args.stages else args.stages.split(",")
    unknown = [s for s in stages if s not in SAFE + DESTRUCTIVE]
    if unknown:
        sys.exit(f"unknown stage(s) {unknown}; choose from {SAFE + DESTRUCTIVE}")
    keys_dir = Path(args.keys_dir).expanduser()
    if not any(keys_dir.glob("*.pem")):
        sys.exit(f"no signing keys in {keys_dir} (make authlimit-keys); the smoke verifies identities against them")
    keys = public_keys(keys_dir)
    tls = ssl.create_default_context(cafile=args.ca)
    door = Door(args, tls, password)
    try:
        await door.start()
        runners = {
            "identity": lambda: stage_identity(door, keys),
            "refusals": lambda: stage_refusals(door),
            "spoofing": lambda: stage_spoofing(door, keys),
            "limits": lambda: stage_limits(door),
            "mcp": lambda: stage_mcp(door),
            "revoke": lambda: stage_revoke(door),
            "logs": lambda: stage_logs(door, keys_dir),
            "observe": lambda: stage_observe(door),
            "replica": lambda: stage_replica(door),
            "rotate": lambda: stage_rotate(door, keys, keys_dir),
            "outage": lambda: stage_outage(door),
        }
        for name in stages:
            if name in NEEDS_COMPOSE and not args.compose:
                print(f"\n== {name}: SKIPPED (needs --compose, the running stack's compose command line; make authlimit-smoke passes it)")
                continue
            await runners[name]()
    finally:
        await door.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="End-to-end check of the front door (specs/018).")
    parser.add_argument("--ca", required=True, help="Caddy's local root certificate (make mcp-gateway-ca)")
    parser.add_argument("--api", default="https://api.localhost:8443")
    parser.add_argument("--mcp", default="https://mcp.localhost:8443")
    parser.add_argument("--auth", default="https://auth.localhost:8443")
    parser.add_argument("--keys-dir", default=os.environ.get("AUTHLIMIT_KEYS_DIR") or "~/.cache/agent-core-demo-authlimit/keys")
    parser.add_argument("--compose", default="", help="the compose command of the running stack, as one string split like a shell would (make authlimit-smoke passes it); needed by revoke, logs and the destructive stages")
    parser.add_argument("--image", default="authlimit:dev")
    parser.add_argument("--all", action="store_true", help="also run the stages that stop or restart things")
    parser.add_argument("--stages", default="", help="a comma-separated list of stages instead of the default")
    args = parser.parse_args()
    asyncio.run(run(args))
    print()
    if FAILED:
        print(f"{len(FAILED)} check(s) FAILED:")
        for label in FAILED:
            print(f"  - {label}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()

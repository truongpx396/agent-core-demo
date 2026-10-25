"""Does the MCP gateway stack work, end to end, the way a client uses it? `make mcp-gateway-smoke` (specs/017).

It acts like Claude Code does: an OAuth authorization-code + PKCE login as a PUBLIC client, with the RFC 8707 `resource` parameter, through
Caddy over TLS signed by Caddy's local CA; then the real MCP client with the token it got. It is the check the unit tests cannot be: they
replace the authorization server, and what this repo depends on from Casdoor (the audience, the claim set, public clients, the issuer) is
measured behaviour of one version, so it is also what to run after changing the Casdoor digest in docker-compose.yml.

It needs the stack up and bootstrapped (`make mcp-gateway-up`, `make mcp-gateway-bootstrap`, `make mcp-gateway-ca`). It creates a throwaway
user in each organization (a demo user's password is never touched) and deletes them, however the run ends. A manual tier, like
`make test-sandbox`: it needs Docker services and is not part of CI. Exit status 0 only if every check passed.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import ssl
import sys
import urllib.parse

import httpx
import httpx2  # the SDK's own HTTP client library (mcp 2.x depends on it): the client handed to it must be one of these
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

CALLBACK = "http://localhost:33418/callback"  # what scripts/mcp_gateway_bootstrap.py registers
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(label)


def _json_part(token: str, index: int) -> dict:
    part = token.split(".")[index]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


class Stack:
    def __init__(self, auth: str, mcp: str, tls: ssl.SSLContext, admin_password: str) -> None:
        self.auth, self.mcp, self.tls, self.admin_password = auth, mcp, tls, admin_password
        self.admin = httpx.AsyncClient(base_url=auth, verify=tls, timeout=20.0)
        self.users: list[tuple[str, str]] = []  # (organization, name) created by this run, deleted at the end
        self.passwords: dict[tuple[str, str], str] = {}
        self.applications: dict[str, dict] = {}

    async def start(self) -> None:
        reply = (
            await self.admin.post(
                "/api/login",
                json={"application": "app-built-in", "organization": "built-in", "username": "admin", "password": self.admin_password, "type": "login", "autoSignin": True},
            )
        ).json()
        if reply.get("status") != "ok":
            sys.exit(f"admin login failed: {reply.get('msg')} (stopping rather than retrying: Casdoor locks an account after five failures)")

    async def application(self, name: str) -> dict | None:
        if name not in self.applications:
            reply = (await self.admin.get("/api/get-application", params={"id": f"admin/{name}"})).json()
            self.applications[name] = reply.get("data")
        return self.applications[name]

    async def throwaway_user(self, organization: str) -> tuple[str, str]:
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

    async def cleanup(self) -> None:
        for organization, name in self.users:
            await self.admin.post("/api/delete-user", json={"owner": organization, "name": name})

    async def token(self, application: str, user: tuple[str, str], resource: str | None, scope: str) -> tuple[str | None, dict]:
        """Log in and exchange the code, as a public client (no secret). Returns (access token or None, the last reply)."""
        app = await self.application(application)
        if app is None:
            sys.exit(f"no Casdoor application {application!r}: run make mcp-gateway-bootstrap first")
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        query = {"clientId": app["clientId"], "responseType": "code", "redirectUri": CALLBACK, "scope": scope, "state": "s", "nonce": "",
                 "code_challenge_method": "S256", "code_challenge": challenge}
        if resource:
            query["resource"] = resource
        organization, name = user
        async with httpx.AsyncClient(base_url=self.auth, verify=self.tls, timeout=20.0) as client:
            reply = (await client.post("/api/login?" + urllib.parse.urlencode(query),
                                       json={"application": application, "organization": organization, "username": name,
                                             "password": self.passwords[user], "type": "code", "autoSignin": True})).json()
            if reply.get("status") != "ok":
                return None, reply
            form = {"grant_type": "authorization_code", "client_id": app["clientId"], "code": reply["data"], "redirect_uri": CALLBACK, "code_verifier": verifier}
            if resource:
                form["resource"] = resource
            reply = (await client.post("/api/login/oauth/access_token", data=form)).json()
        return reply.get("access_token"), reply

    async def post(self, token: str | None, path: str) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(verify=self.tls, headers=headers, timeout=30.0) as http:
            return await http.post(self.mcp + path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                   headers={"Accept": "application/json, text/event-stream"})

    async def tools(self, token: str, path: str) -> list[str]:
        async with httpx2.AsyncClient(verify=self.tls, headers={"Authorization": f"Bearer {token}"}, timeout=30.0) as http:
            async with Client(streamable_http_client(self.mcp + path, http_client=http)) as client:
                return sorted(t.name for t in (await client.list_tools()).tools)

    async def call(self, token: str, path: str, tool: str, arguments: dict | None = None) -> str:
        async with httpx2.AsyncClient(verify=self.tls, headers={"Authorization": f"Bearer {token}"}, timeout=30.0) as http:
            async with Client(streamable_http_client(self.mcp + path, http_client=http)) as client:
                result = await client.call_tool(tool, arguments or {})
                return "".join(block.text for block in result.content if isinstance(block, TextContent))


async def run(stack: Stack) -> None:
    ecorp_url, ops_url = f"{stack.mcp}/ecorp/mcp", f"{stack.mcp}/ops/mcp"
    alice = await stack.throwaway_user("ecorp")
    operator = await stack.throwaway_user("ops-team")
    other = await stack.throwaway_user("other-co") if await stack.application("mcp-other-co") else None

    token, reply = await stack.token("mcp-ecorp", alice, ecorp_url, "ecorp:read")
    check("a public client (no secret) completes code + PKCE through the proxy", token is not None, str(reply)[:200])
    if token is None:
        return
    claims = _json_part(token, 1)
    check("the token is RS256 and its issuer is the public issuer", _json_part(token, 0)["alg"] == "RS256" and claims["iss"] == stack.auth, f"iss={claims.get('iss')}")
    check("its audience is exactly the ecorp resource", claims["aud"] == [ecorp_url], f"aud={claims.get('aud')}")
    check("its owner is the organization (the tenant) and its scope is ecorp:read", claims["owner"] == "ecorp" and claims["scope"] == "ecorp:read")

    response = await stack.post(None, "/ecorp/mcp")
    check("no token: 401 naming where to log in", response.status_code == 401 and "resource_metadata=" in response.headers.get("www-authenticate", ""))
    check("a tampered token: 401", (await stack.post(token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB"), "/ecorp/mcp")).status_code == 401)
    no_resource, _ = await stack.token("mcp-ecorp", alice, None, "ecorp:read")
    check("a token minted without the resource parameter (audience = client id): 401",
          no_resource is not None and (await stack.post(no_resource, "/ecorp/mcp")).status_code == 401)

    check("the ecorp server offers its one tool", await stack.tools(token, "/ecorp/mcp") == ["query_employees"])
    rows = await stack.call(token, "/ecorp/mcp", "query_employees")
    check("a user of ecorp reads ecorp's rows", "Priya Nair" in rows and "Jordan Lee" not in rows, rows[:120])
    forced = await stack.call(token, "/ecorp/mcp", "query_employees", {"tenant": "other-co", "principal": "x", "name_contains": "other-co"})
    check("naming another tenant in the arguments changes nothing", "Jordan Lee" not in forced, forced[:120])
    if other:
        other_token, _ = await stack.token("mcp-other-co", other, ecorp_url, "ecorp:read")
        if other_token is None:
            sys.exit("could not log in as the other-co user")
        a, b = await asyncio.gather(stack.call(token, "/ecorp/mcp", "query_employees"), stack.call(other_token, "/ecorp/mcp", "query_employees"))
        check("two tenants at the same time each read only their own rows", "Priya Nair" in a and "Jordan Lee" not in a and "Jordan Lee" in b and "Priya Nair" not in b)
    else:
        print("SKIP  the second-tenant check (no mcp-other-co application: make mcp-gateway-bootstrap ARGS=\"--tenant ecorp --tenant other-co\")")

    ops_token, reply = await stack.token("mcp-ops", operator, ops_url, "ops:read")
    check("an operator logs in to the ops application", ops_token is not None, str(reply)[:200])
    if ops_token:
        check("... and reads the ops server", await stack.tools(ops_token, "/ops/mcp") == ["fetch_metrics_summary", "list_recent_incidents"])
        incidents = await stack.call(ops_token, "/ops/mcp", "list_recent_incidents")
        check("... and its incident log", "Refused" not in incidents, incidents[:120])
        check("the operator's ops token does not open the ecorp server", (await stack.post(ops_token, "/ecorp/mcp")).status_code == 401)
    check("a tenant's token for the ecorp server does not open the ops server", (await stack.post(token, "/ops/mcp")).status_code == 401)
    cross, _ = await stack.token("mcp-ecorp", alice, ops_url, "ecorp:read")  # the authorization server mints this: it checks neither
    check("... and even one whose audience says ops is refused: not an operator tenant (403)", cross is not None and (await stack.post(cross, "/ops/mcp")).status_code == 403)


async def main() -> None:
    parser = argparse.ArgumentParser(description="End-to-end check of the MCP gateway stack (OAuth login + MCP calls).")
    parser.add_argument("--ca", required=True, help="Caddy's root certificate (make mcp-gateway-ca)")
    parser.add_argument("--auth", default="https://auth.localhost:8443")
    parser.add_argument("--mcp", default="https://mcp.localhost:8443")
    args = parser.parse_args()
    password = os.environ.get("CASDOOR_ADMIN_PASSWORD", "")
    if not password:
        sys.exit("CASDOOR_ADMIN_PASSWORD is not set (make mcp-gateway-smoke passes it)")
    stack = Stack(args.auth, args.mcp, ssl.create_default_context(cafile=args.ca), password)
    try:
        await stack.start()
        await run(stack)
    finally:
        await stack.cleanup()
        await stack.admin.aclose()
    print(f"\n{len(FAILED)} failed" if FAILED else "\nall checks passed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())

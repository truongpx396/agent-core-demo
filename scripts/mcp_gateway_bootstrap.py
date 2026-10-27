"""Create, idempotently, what the MCP gateway expects to find in Casdoor (specs/017): `make mcp-gateway-bootstrap`.

The gateway is a resource server; it trusts whatever the authorization server signed for it. For that to be useful the authorization
server needs, for each published server, an APPLICATION whose tokens carry the right claims, and a user to log in as. This makes them, so
the stack works after one command and a second run changes nothing it should not:

  * one ORGANIZATION per tenant (default `ecorp`, which owns the seeded Ecorp rows) and one for the operators (`ops-team`). The token's
    `owner` claim is the organization, and the gateway reads the tenant from it (`MCP_GATEWAY_TENANT_CLAIM`).
  * one APPLICATION per organization and server: `mcp-<tenant>` for the Ecorp directory (scope `ecorp:read`) and `mcp-ops` for the
    operations server (scope `ops:read`). Category `Agent` / type `MCP` is what turns custom scopes on. Tokens are `JWT-Standard`
    (17 claims, none of the user's profile; the default format carries ~85 including `lastSigninIp`).
  * (specs/018) one APPLICATION per tenant for the chat API: `api-<tenant>` (scope `api:chat`), a public client whose redirect URIs are the
    web page's root and the smoke check's callback. Its tokens are what the authlimit service verifies at the front door.
  * every application's access token lasts SEVEN DAYS by default (`--token-hours` or the TOKEN_HOURS variable change it; spec 018 FR-022).
    Verification is offline, so a token cannot be recalled by the authorization server: `make authlimit-revoke` is how an operator bounds
    that, and a short access token with a refresh token is the production recommendation. (Spec 017 used one hour for the MCP applications;
    revocation now covers the MCP routes too, so they follow the same setting.)
  * a demo USER in each organization. A password is printed ONCE, when the user is created; an existing user is left alone.

It prints the `claude mcp add` command for each server: a PRE-REGISTERED client with a fixed callback port, because this Casdoor has
dynamic client registration off by default (anyone who can reach it could register a client) and no Client ID Metadata Document support
(both measured, specs/017 research.md), and Casdoor compares redirect URIs exactly.

Runs INSIDE the gateway container (`docker compose exec`), where Casdoor is `http://casdoor:8000`: no TLS to trust, and the admin API is not
published. Needs CASDOOR_ADMIN_PASSWORD in the environment. Standard library plus httpx, nothing from `app`.
"""
import argparse
import os
import secrets
import sys

import httpx

ADMIN_ORG = "built-in"
ADMIN_USER = "admin"
DEFAULT_TOKEN_HOURS = 168  # seven days (spec 018, FR-022)
REFRESH_HOURS = 168
# The smoke check (scripts/authlimit_smoke.py) signs in as a public client with this callback; the web page signs in with its own root URL.
API_SMOKE_CALLBACK = "http://localhost:33419/callback"


class CasdoorError(Exception):
    pass


class Casdoor:
    """The few admin endpoints this needs. Every call checks Casdoor's own `status` field: it answers HTTP 200 for failures."""

    def __init__(self, base_url: str, admin_password: str) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=20.0)
        reply = self.http.post(
            "/api/login",
            json={
                "application": "app-built-in",
                "organization": ADMIN_ORG,
                "username": ADMIN_USER,
                "password": admin_password,
                "type": "login",
                "autoSignin": True,
            },
        ).json()
        if reply.get("status") != "ok":
            raise CasdoorError(f"admin login failed ({reply.get('msg') or reply.get('code')}); stopping rather than retrying (Casdoor locks after 5)")

    def read(self, path: str, **params: str):
        reply = self.http.get(path, params=params).json()
        if reply.get("status") == "error":
            raise CasdoorError(f"GET {path}: {reply.get('msg')}")
        return reply.get("data")

    def write(self, path: str, body: dict, **params: str) -> None:
        reply = self.http.post(path, json=body, params=params).json()
        if reply.get("status") != "ok":
            raise CasdoorError(f"POST {path}: {reply.get('msg')}")

    # -- what to ensure --------------------------------------------------------------------------------------------------------

    def ensure_organization(self, name: str) -> None:
        if self.read("/api/get-organization", id=f"admin/{name}"):
            return
        self.write(
            "/api/add-organization",
            {"owner": "admin", "name": name, "displayName": name, "passwordType": "bcrypt", "defaultApplication": "", "enableSoftDeletion": False},
        )
        print(f"created organization {name}")

    def ensure_user(self, organization: str, name: str) -> str | None:
        """The generated password if the user was created now, else None."""
        if self.read("/api/get-user", id=f"{organization}/{name}"):
            return None
        password = secrets.token_urlsafe(18)
        self.write(
            "/api/add-user",
            {
                "owner": organization,
                "name": name,
                "displayName": name,
                "password": password,
                "email": f"{name}@{organization}.localhost",
                "type": "normal-user",
                "isAdmin": False,
                "isForbidden": False,
            },
        )
        return password

    def ensure_application(
        self, name: str, organization: str, scope: str, description: str, redirect_uris: list[str], token_hours: int, kind: str = "MCP"
    ) -> str:
        """Create or update the application; returns its client id (kept on a re-run, so a client already registered with it still works)."""
        existing = self.read("/api/get-application", id=f"admin/{name}")
        record = {
            "owner": "admin",
            "name": name,
            "displayName": f"{kind}: {description}",
            "organization": organization,
            "category": "Agent",
            "type": "MCP",
            "cert": "cert-built-in",
            "enablePassword": True,
            "enableSignUp": False,
            "grantTypes": ["authorization_code", "refresh_token"],
            "redirectUris": redirect_uris,
            "tokenFormat": "JWT-Standard",
            "expireInHours": token_hours,
            "refreshExpireInHours": REFRESH_HOURS,
            "scopes": [{"name": scope, "displayName": scope, "description": description}],
        }
        if existing:
            record = {**existing, **record, "clientId": existing["clientId"], "clientSecret": existing["clientSecret"]}
            self.write("/api/update-application", record, id=f"admin/{name}")
            return existing["clientId"]
        client_id = secrets.token_hex(10)
        self.write("/api/add-application", {**record, "clientId": client_id, "clientSecret": secrets.token_hex(20)})
        print(f"created application {name}")
        return client_id


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Create the Casdoor organizations, applications and demo users the MCP gateway expects.")
    parser.add_argument("--tenant", action="append", default=[], help="a tenant (Casdoor organization) to create an Ecorp-directory application for; repeatable (default: ecorp)")
    parser.add_argument("--ops-org", default=os.environ.get("MCP_GATEWAY_OPS_TENANTS", "ops-team").split(",")[0].strip() or "ops-team")
    parser.add_argument("--callback-port", type=int, default=33418, help="the fixed port of the client's OAuth callback (claude mcp add --callback-port)")
    parser.add_argument(
        "--token-hours",
        type=int,
        default=int(os.environ.get("TOKEN_HOURS") or DEFAULT_TOKEN_HOURS),
        help="how long an access token lasts, for every application this creates (default 168 = seven days; TOKEN_HOURS sets the default). "
        "A shorter lifetime with a refresh token is the production recommendation",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.token_hours <= 8760:
        sys.exit("mcp-gateway-bootstrap: --token-hours must be between 1 and 8760")
    tenants = args.tenant or ["ecorp"]

    password = os.environ.get("CASDOOR_ADMIN_PASSWORD", "")
    if not password:
        sys.exit("CASDOOR_ADMIN_PASSWORD is not set (make mcp-gateway-bootstrap passes it)")
    public = os.environ.get("MCP_GATEWAY_PUBLIC_URL", "https://mcp.localhost:8443").rstrip("/")
    callback = f"http://localhost:{args.callback_port}/callback"
    api_public = os.environ.get("AUTHLIMIT_AUDIENCE", "https://api.localhost:8443").rstrip("/")  # the chat API's resource identifier (the token's audience)

    try:
        casdoor = Casdoor(os.environ.get("CASDOOR_URL", "http://casdoor:8000"), password)
        commands = []
        api_clients = []
        for tenant in tenants:
            casdoor.ensure_organization(tenant)
            client_id = casdoor.ensure_application(f"mcp-{tenant}", tenant, "ecorp:read", f"Ecorp directory for {tenant}", [callback], args.token_hours)
            user_password = casdoor.ensure_user(tenant, "alice")
            commands.append(("ecorp" if tenant == "ecorp" else f"ecorp-{tenant}", tenant, "/ecorp/mcp", client_id, "alice", user_password))
            # The chat API (specs/018): the page signs in at its own root URL; the smoke check signs in at its own callback.
            api_client_id = casdoor.ensure_application(
                f"api-{tenant}", tenant, "api:chat", f"chat API for {tenant}", [f"{api_public}/", API_SMOKE_CALLBACK], args.token_hours, kind="API"
            )
            api_clients.append((tenant, api_client_id))
        casdoor.ensure_organization(args.ops_org)
        client_id = casdoor.ensure_application("mcp-ops", args.ops_org, "ops:read", "operations metrics and incidents", [callback], args.token_hours)
        commands.append(("ops", args.ops_org, "/ops/mcp", client_id, "oncall", casdoor.ensure_user(args.ops_org, "oncall")))
    except (CasdoorError, httpx.HTTPError) as exc:
        sys.exit(f"mcp-gateway-bootstrap: {exc}")

    print("\nRegister each server in Claude Code (a pre-registered client; trust the local CA first: make mcp-gateway-ca):\n")
    for label, organization, path, client_id, user, user_password in commands:
        print(f"  claude mcp add --transport http --callback-port {args.callback_port} --client-id {client_id} {label} {public}{path}")
        shown = user_password if user_password else "(already existed: password unchanged)"
        print(f"    log in as {organization}/{user}  password: {shown}\n")
    print(f"The chat API (resource {api_public}, scope api:chat), one public client per tenant; sign in as <tenant>/alice:\n")
    for tenant, api_client_id in api_clients:
        print(f"  {tenant}: client id {api_client_id}")
    print(f"\nAccess tokens last {args.token_hours} hours. `make authlimit-revoke TENANT=<tenant> SUB=<subject>` voids a person's tokens at once.")
    print("The browser login is https://auth.localhost:8443 (Casdoor, signed by Caddy's local CA).")


if __name__ == "__main__":
    main()

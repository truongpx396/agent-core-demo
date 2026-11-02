"""Add, disable or re-enable a Telegram person at the authorization server (specs/020; `make telegram-person`).

A Telegram user is a person `telegram-<id>` of one tenant, and the bot signs in as them with a password it DERIVES from TELEGRAM_PERSON_SECRET
(app/channels/telegram_people.py). This tool is how an operator creates that person without the bot ever holding a credential that can: it logs in
to Casdoor as the administrator (the generated password in the secrets file `make mcp-gateway-secrets` keeps outside the repo) and writes the user
with the same derived password. It never prints the password.

  make telegram-person ACTION=add TELEGRAM_ID=123456789
  make telegram-person ACTION=disable TELEGRAM_ID=123456789   # sign-in refused from now; a disabled person's refresh tokens stop working at once

Disabling does not void an access token already issued (seven days by default): the command prints the `make authlimit-revoke` line that does.
If the door uses Caddy's local CA, set SSL_CERT_FILE to the root `make mcp-gateway-ca` exports.
"""
import argparse
import os
import sys

import httpx

from app.channels.telegram_people import MIN_SECRET_CHARS, person_name, person_password
from app.core.config import settings
from scripts.mcp_gateway_bootstrap import Casdoor, CasdoorError


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Add, disable or re-enable a Telegram person at the authorization server.")
    parser.add_argument("action", choices=("add", "disable", "enable"))
    parser.add_argument("--telegram-id", type=int, required=True)
    args = parser.parse_args(argv)

    secret = settings.telegram_person_secret
    if len(secret) < MIN_SECRET_CHARS:
        sys.exit(f"telegram-person: TELEGRAM_PERSON_SECRET must be set to at least {MIN_SECRET_CHARS} characters (in .env)")
    admin_password = os.environ.get("CASDOOR_ADMIN_PASSWORD", "")
    if not admin_password:
        sys.exit("telegram-person: CASDOOR_ADMIN_PASSWORD is not set (make telegram-person passes it)")
    tenant = settings.telegram_tenant.strip() or settings.default_tenant
    name = person_name(args.telegram_id)
    try:
        casdoor = Casdoor(settings.web_auth_issuer.strip().rstrip("/"), admin_password)
        existing = casdoor.read("/api/get-user", id=f"{tenant}/{name}")
        if args.action == "add":
            if existing:
                print(f"{tenant}/{name} already exists (nothing changed)")
                return
            casdoor.ensure_organization(tenant)
            casdoor.write(
                "/api/add-user",
                {
                    "owner": tenant,
                    "name": name,
                    "displayName": name,
                    "password": person_password(secret, tenant, args.telegram_id),
                    "email": f"{name}@{tenant}.invalid",
                    "type": "normal-user",
                    "isAdmin": False,
                    "isForbidden": False,
                },
            )
            print(f"added {tenant}/{name}: Telegram user {args.telegram_id} can now use the bot")
            return
        if not existing:
            sys.exit(f"telegram-person: {tenant}/{name} does not exist")
        casdoor.write("/api/update-user", {**existing, "isForbidden": args.action == "disable"}, id=f"{tenant}/{name}")
        print(f"{args.action}d {tenant}/{name}")
        if args.action == "disable":
            print(f"tokens already issued stay valid until they expire; void them now with:\n  make authlimit-revoke TENANT={tenant} SUB={existing['id']}")
    except (CasdoorError, httpx.HTTPError) as exc:
        sys.exit(f"telegram-person: {exc}")


if __name__ == "__main__":
    main()

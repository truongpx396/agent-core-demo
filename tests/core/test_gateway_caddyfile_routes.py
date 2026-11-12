"""What the front door's Caddy routing names agrees with the code it routes to (pattern 98).

`deploy/caddy/gateway.caddy` is shared by the development and production Caddyfiles. It names two things that live in code, and a drift in either
only shows when a request arrives: a Telegram path the bot no longer serves is a silent 404 behind a webhook Telegram keeps retrying, and an address
limit authlimit does not define is a 500 on every sign-in.
"""
import json
import re
from pathlib import Path

from app.adapters.inbound.channels.telegram.link import CALLBACK_PATH, CONFIRM_PATH
from app.adapters.inbound.channels.telegram.server import WEBHOOK_PATH

REPO = Path(__file__).resolve().parents[2]
GATEWAY_CADDY = (REPO / "deploy" / "caddy" / "gateway.caddy").read_text(encoding="utf-8")


def test_the_telegram_paths_caddy_routes_are_the_ones_the_bot_serves():
    for path in (WEBHOOK_PATH, CALLBACK_PATH, CONFIRM_PATH):
        assert path in GATEWAY_CADDY, f"gateway.caddy does not route {path}, which the Telegram bot serves"


def test_every_address_limit_caddy_asks_authlimit_for_is_one_its_route_table_defines():
    asked = set(re.findall(r"^\s*import address_limit (\w+)\s*$", GATEWAY_CADDY, re.MULTILINE))
    defined = set(json.loads((REPO / "services" / "authlimit" / "routes.json").read_text(encoding="utf-8"))["address_limits"])

    assert asked, "gateway.caddy asks for no address limit: the sign-in and the webhooks are unmetered"
    assert asked <= defined, f"gateway.caddy asks for {sorted(asked - defined)}, which routes.json does not define"

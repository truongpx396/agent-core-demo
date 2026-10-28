"""The built-in web page's sign-in, server side (specs/018): what the page is told about the mode it runs in, and the script that signs in.

The page (`app/api/static/index.html`) is served as one file; its sign-in is `/static/auth.js`. The script needs to know two things the page cannot
work out for itself: whether this API is in `identity` mode (a token is needed, the Tenant and Principal pickers are meaningless) or `headers` mode
(today's behaviour, untouched), and, in identity mode, where the authorization server is. The smallest way to tell it is a <meta> tag the server
writes into the page it already renders: no second request the page would have to wait for before its first one, no route to classify, no cache.

The tag carries no secret: the authorization server's address, the audience the token must be for, the scope, and the prefix of a tenant's client
id. The value is HTML-escaped, so a setting with a quote in it cannot break out of the attribute.
"""
import html
import json
from pathlib import Path

from app.core.config import settings

# A tenant's chat-API application at the authorization server has the client id `api-<tenant>` (scripts/mcp_gateway_bootstrap.py creates it
# so), which is how the page finds the client of the organization a person types without being told every tenant's id in advance.
CLIENT_ID_PREFIX = "api-"
PLACEHOLDER = "__AGENT_AUTH__"
SCRIPT_PATH = Path(__file__).parent / "static" / "auth.js"


def page_config() -> dict:
    """What the page is told. In header mode only the mode: nothing about an authorization server that is not in use."""
    if settings.auth_mode != "identity":
        return {"mode": "headers"}
    return {
        "mode": "identity",
        "issuer": settings.web_auth_issuer.strip().rstrip("/"),
        "audience": settings.web_auth_audience.strip().rstrip("/"),
        "scope": settings.web_auth_scope.strip(),
        "client_id_prefix": CLIENT_ID_PREFIX,
    }


def render(page: str) -> str:
    """The page with its placeholder replaced by the (escaped) configuration."""
    return page.replace(PLACEHOLDER, html.escape(json.dumps(page_config(), separators=(",", ":")), quote=True))


def script() -> str:
    return SCRIPT_PATH.read_text()

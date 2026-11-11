"""Write, or check, the committed OpenAPI documents of the two Python services.

    python -m scripts.tools.export_openapi            # rewrite openapi.json and services/ml-service/openapi.json from the code
    python -m scripts.tools.export_openapi --check    # exit 1, naming each stale file, when one differs from what the code serves

`make openapi` is the first form. The third service, authlimit, is Go and its `services/authlimit/openapi.json` is written by hand; its
own tests hold it to the handlers (services/authlimit/internal/server/openapi_test.go).

A document sits beside the Dockerfile of the process it describes. It is the FastAPI app's own `openapi()`, so it cannot disagree with
the routes, plus a `servers` list added HERE and not on the apps: Swagger UI calls the first server a document names, so setting it on
the app would send "Try it out" from a page served behind the front door to another origin. Served live, a document names no server.
tests/scripts/test_openapi_documents_are_current.py runs this check in the default suite, and CI's `openapi` job compares each document
against main for a breaking change (`make openapi-breaking`).
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]

CHAT_API = REPO / "openapi.json"
ML_SERVICE = REPO / "services" / "ml-service" / "openapi.json"

# Where a client can reach each service in this repository's own stacks. No production host is listed: a deployment's address is its own
# (deploy/README.md), and a document that named one would be a guess that outlives the guesser.
SERVERS: dict[Path, list[dict[str, str]]] = {
    CHAT_API: [
        {"url": "http://localhost:8000", "description": "This API alone (`make serve`). Runs with AUTH_MODE=headers: name the caller with X-Tenant-Id and X-Principal-Id."},
        {"url": "https://api.localhost:8443", "description": "Behind the front door (`make authlimit-up-app`). Send a Casdoor access token as a Bearer token."},
    ],
    ML_SERVICE: [
        {"url": "http://localhost:8083", "description": "The container's port 80, published by `make up`."},
        {"url": "http://ml-service", "description": "Inside the compose network, where the app reaches it (ML_SERVICE_URL)."},
    ],
}


def _with_servers(spec: dict[str, Any], servers: list[dict[str, str]]) -> dict[str, Any]:
    """A new document with `servers` right after `info`, where a reader expects it. `app.openapi()` caches its dict: never edit that."""
    out: dict[str, Any] = {}
    for key, value in spec.items():
        out[key] = value
        if key == "info":
            out["servers"] = servers
    return out


def _chat_api() -> dict[str, Any]:
    from app.adapters.inbound.api import main as api

    return api.app.openapi()


def _ml_service() -> dict[str, Any]:
    from ml_service.app import create_app
    from ml_service.settings import Settings

    # `specs=[]`: the document does not depend on which models are enabled, so it must not need models.toml's files or a download.
    return create_app(Settings.from_env(), specs=[]).openapi()


def render() -> dict[Path, str]:
    """Each document's file text, exactly as it is committed."""
    built = {CHAT_API: _chat_api(), ML_SERVICE: _ml_service()}
    return {path: json.dumps(_with_servers(spec, SERVERS[path]), indent=2, ensure_ascii=False) + "\n" for path, spec in built.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 if a committed document is stale")
    args = parser.parse_args(argv)

    stale = []
    for path, text in render().items():
        if path.exists() and path.read_text(encoding="utf-8") == text:
            continue
        stale.append(path)
        if not args.check:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(REPO)}")
    if args.check and stale:
        names = ", ".join(str(p.relative_to(REPO)) for p in stale)
        print(f"stale: {names}. Run `make openapi` and commit the result.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

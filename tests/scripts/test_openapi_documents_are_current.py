"""The committed OpenAPI documents are what the two Python services serve (`make openapi` writes them).

Nothing else reads these files at build time, so without this a route added or changed in code would leave the file a client or the CI
breaking-change check reads describing yesterday's API, green. It is a check of an artifact against CODE, the kind the testing rules
keep. The authlimit document is hand-written and held to its handlers by a Go test (services/authlimit/internal/server/openapi_test.go).
"""
import json
from pathlib import Path

import pytest

# The ML service's metrics need prometheus-client, which only the `test` job (the gate) installs: every other tier collects this directory
# too, and an import error there is a failed job, not a skip (the same line tests/services/ml_service/conftest.py has).
pytest.importorskip("prometheus_client", reason="ml_service.metrics needs prometheus-client (requirements-dev.txt / the CI test job)")

from app.adapters.inbound.api import main as api
from app.adapters.inbound.api.openapi import TAGS
from ml_service.app import API_TAGS, create_app
from ml_service.settings import Settings
from scripts.tools import export_openapi

HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}


@pytest.fixture(scope="module")
def rendered() -> dict[Path, str]:
    return export_openapi.render()


@pytest.mark.parametrize("path", [export_openapi.CHAT_API, export_openapi.ML_SERVICE], ids=lambda p: p.parent.name or "app")
def test_the_committed_document_is_what_the_code_serves(path: Path, rendered: dict[Path, str]):
    committed = path.read_text(encoding="utf-8") if path.exists() else None
    assert committed == rendered[path], f"{path.relative_to(export_openapi.REPO)} is stale: run `make openapi` and commit the result"


def test_a_committed_document_names_where_a_client_can_reach_the_service(rendered: dict[Path, str]):
    for path, text in rendered.items():
        servers = json.loads(text)["servers"]
        assert servers and all(s["url"] and s["description"] for s in servers), path


def test_the_live_document_names_no_server_so_swagger_ui_calls_the_origin_it_was_served_from():
    """A `servers` list on the app is what Swagger UI's "Try it out" calls: behind the front door that would be another origin."""
    assert "servers" not in api.app.openapi()
    assert "servers" not in create_app(Settings.from_env(), specs=[]).openapi()


@pytest.mark.parametrize(
    ("spec", "declared"),
    [(api.app.openapi, TAGS), (create_app(Settings.from_env(), specs=[]).openapi, API_TAGS)],
    ids=["app", "ml-service"],
)
def test_every_operation_is_under_a_tag_the_document_describes(spec, declared):
    described = {tag["name"] for tag in declared}
    for route, item in spec()["paths"].items():
        for method, operation in item.items():
            if method in HTTP_METHODS:
                tags = set(operation.get("tags", []))
                assert tags and tags <= described, f"{method.upper()} {route} is under {sorted(tags) or 'no tag'}; give it one of {sorted(described)}"

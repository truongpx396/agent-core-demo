"""The Bruno collection (`bruno/`) calls what the API serves, the way the API reads it.

Nothing runs the collection in CI (it needs the stack), so a renamed route, a misspelled body field or a variable no environment
defines would stay green until someone clicked Send. The worst cases fail quietly: pydantic ignores an unknown body field, and Bruno
sends an undefined `{{variable}}` as literal text. This checks the files against the real app's OpenAPI, as
test_authlimit_routes_cover_api.py does for the front door's route table.
"""
import json
import re
import textwrap
from pathlib import Path

from app.adapters.inbound.api import main as api

COLLECTION = Path(__file__).resolve().parents[4] / "bruno"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
# Documented, and deliberately not a request: the web page is HTML, not an API call.
NOT_IN_COLLECTION = {("GET", "/")}

BLOCK = re.compile(r"^([\w:-]+) \{\n(.*?)^\}$", re.MULTILINE | re.DOTALL)  # a top-level block: its closing brace is at column 0
VARIABLE = re.compile(r"\{\{(\w+)\}\}")
PATH_PARAMETER = re.compile(r"\{\w+\}")


def _blocks(path: Path) -> dict[str, str]:
    return dict(BLOCK.findall(path.read_text(encoding="utf-8")))


def _requests() -> list[tuple[Path, str, str, dict[str, str]]]:
    """`(file, METHOD, route, blocks)` per request file; `route` has its `{{baseUrl}}` cut and each variable segment as `{}`."""
    found = []
    for path in sorted(COLLECTION.rglob("*.bru")):
        if path.name in {"collection.bru", "folder.bru"} or "environments" in path.parts:
            continue
        blocks = _blocks(path)
        method = next(name for name in blocks if name in HTTP_METHODS)
        url = re.search(r"^  url: (.+)$", blocks[method], re.MULTILINE)
        assert url, f"{path.name}: no url"
        found.append((path, method.upper(), VARIABLE.sub("{}", url.group(1).removeprefix("{{baseUrl}}")), blocks))
    return found


def _operations() -> dict[tuple[str, str], dict]:
    return {
        (method.upper(), PATH_PARAMETER.sub("{}", path)): operation
        for path, methods in api.app.openapi()["paths"].items()
        for method, operation in methods.items()
        if method in HTTP_METHODS
    }


def _request_schema(operation: dict, media_type: str) -> dict:
    schema = operation["requestBody"]["content"][media_type]["schema"]
    if "$ref" in schema:
        schema = api.app.openapi()["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    return schema


def _environment_variables(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    declared = _blocks(path).get("vars", "")
    secret = re.search(r"^vars:secret \[\n(.*?)^\]$", text, re.MULTILINE | re.DOTALL)
    return set(re.findall(r"^  (\w+):", declared, re.MULTILINE)) | set(re.findall(r"^  (\w+)$", secret.group(1) if secret else "", re.MULTILINE))


def test_the_collection_calls_every_documented_route_and_no_other():
    called = {(method, route) for _, method, route, _ in _requests()}
    documented = set(_operations()) - NOT_IN_COLLECTION

    assert called == documented, f"no request for: {sorted(documented - called)}; request for a route the API lacks: {sorted(called - documented)}"


def test_a_json_body_sends_only_fields_the_endpoint_has_and_every_one_it_requires():
    operations = _operations()
    checked = 0
    for path, method, route, blocks in _requests():
        if "body:json" not in blocks:
            continue
        schema = _request_schema(operations[(method, route)], "application/json")
        sent = set(json.loads(textwrap.dedent(blocks["body:json"])))
        assert sent <= set(schema["properties"]), f"{path.name} sends a field the API ignores: {sorted(sent - set(schema['properties']))}"
        assert set(schema.get("required", [])) <= sent, f"{path.name} leaves out a required field: {sorted(set(schema['required']) - sent)}"
        checked += 1

    assert checked >= 4, "premise: the collection has JSON requests to check"


def test_the_upload_sends_the_form_fields_the_endpoint_reads_and_a_file_that_exists():
    operations = _operations()
    for path, method, route, blocks in _requests():
        if "body:multipart-form" not in blocks:
            continue
        schema = _request_schema(operations[(method, route)], "multipart/form-data")
        fields = dict(re.findall(r"^  (\w+): (.+)$", blocks["body:multipart-form"], re.MULTILINE))
        assert set(fields) <= set(schema["properties"]), f"{path.name} sends a field the API ignores: {sorted(set(fields) - set(schema['properties']))}"
        for value in fields.values():
            if value.startswith("@file("):
                assert (COLLECTION / value[len("@file("):-1]).is_file(), f"{path.name}: {value} is not a file in the collection"
        return
    raise AssertionError("premise: the collection has an upload request")


def test_the_collection_sends_the_headers_the_api_reads():
    sent = {name.lower() for name in re.findall(r"^  ([\w-]+): ", _blocks(COLLECTION / "collection.bru")["headers"], re.MULTILINE)}
    operation = _operations()[("POST", "/chat/stream/queued")]
    read = {parameter["name"].lower() for parameter in operation["parameters"] if parameter["in"] == "header"}

    assert sent <= read, f"headers the API never reads: {sorted(sent - read)}"
    assert {"x-tenant-id", "x-principal-id"} <= sent, "header mode answers 422 without both"


def test_every_variable_a_request_uses_is_defined_by_each_environment_or_set_by_a_script():
    environments = {path.stem: _environment_variables(path) for path in (COLLECTION / "environments").glob("*.bru")}
    sources = [path for path, *_ in _requests()] + [COLLECTION / "collection.bru"]
    used, set_by_scripts = set(), set()
    for path in sources:
        blocks = _blocks(path)
        used |= {name for block, body in blocks.items() if block != "docs" for name in VARIABLE.findall(body)}
        set_by_scripts |= {name for body in blocks.values() for name in re.findall(r'bru\.setVar\("(\w+)"', body)}

    assert len(environments) >= 2, "premise: there is an environment per way of reaching the API"
    assert len({frozenset(names) for names in environments.values()}) == 1, f"the environments define different variables: {environments}"
    for name, defined in environments.items():
        assert used <= defined | set_by_scripts, f"{name}: Bruno would send the literal text for {sorted(used - defined - set_by_scripts)}"

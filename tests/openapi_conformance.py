"""What a service really sends, held to its committed OpenAPI document (the fixtures that use this are in the conftest files beside the tests).

A document that nothing checks against behaviour drifts into fiction without a test failing, and a client reads it as fact. These
helpers are the shared part: read the document the way a client does (from the file), close its objects so a field the code sends and the
document lacks fails, and build a validator for one schema.
"""
import functools
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parents[1]


@functools.cache
def load(relative: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((REPO / relative).read_text(encoding="utf-8"))
    return result


def closed(node: Any) -> Any:
    """Objects that list `properties` and say nothing of `additionalProperties` are closed, so an undocumented field fails."""
    if isinstance(node, dict):
        out = {key: closed(value) for key, value in node.items()}
        if out.get("type") == "object" and "properties" in out and "additionalProperties" not in out:
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [closed(item) for item in node]
    return node


def validator(document: dict[str, Any], schema: dict[str, Any]) -> Draft202012Validator:
    """A validator for `schema`, whose `$ref`s into `#/components` resolve against the document's own components."""
    return Draft202012Validator({"components": closed(document["components"]), **closed(schema)})


def event_schema(document: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    """The schema of ONE event a streaming route sends: the `contentSchema` of a frame's `data` under `itemSchema`."""
    media = document["paths"][path][method]["responses"]["200"]["content"]["text/event-stream"]
    schema: dict[str, Any] = media["itemSchema"]["properties"]["data"]["contentSchema"]
    return schema

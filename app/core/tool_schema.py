"""The strict-mode form of a tool's schema: what `make_llm` sends when `TOOL_CALLS_STRICT` is on.

OpenAI's strict function calling (`"strict": true`) makes a provider that supports it decode the arguments against the schema,
so a call can only name an enum value that exists and can only carry the fields that were declared. It wants a narrower
dialect of JSON Schema than Pydantic writes, and LangChain's `convert_to_openai_tool(tool, strict=True)` only closes the
objects (`additionalProperties: false`). It does not make the dialect fit, and for a tool whose arguments are ALL optional it
does not even close the object (its helper keys on `required`, which Pydantic omits when nothing is required), so the
provider would reject the tool. Found by converting every tool of every domain; `tests/core/test_tool_schema.py` does it
again on every run. The rewrite, and how well each part is known:

- Every property is listed in `required`, and every object is closed. An optional argument stays optional by being nullable,
  which the schemas already say (`Topic | None` is `anyOf: [enum, null]`); it is just no longer allowed to be absent. A
  property whose only claim to optionality was a `None` default is made nullable. (Checked against the installed `openai`
  SDK's own converter, `_ensure_strict_json_schema`, which does the same three things and finds nothing left to change.)
- A `default` is removed, and one that is not null is told to the model in the property's description, because a field it
  cannot omit has to say what "no preference" is. `minLength`/`maxLength` are removed and told the same way ("At most 200
  characters."). These two are the cautious reading of OpenAI's list of supported keywords, which as I remember it has no
  length bounds for strings. They have NOT been run against a live strict provider (none is configured here), and the SDK's
  converter does not touch them. If a provider accepts them, this loses the model a hint, not correctness.

Only the copy sent to the model is rewritten. A tool's `args_schema` is untouched, so a call from a model (or a provider) that
does not honour `strict` is validated exactly as before, an omitted optional argument and a real length bound included. That
is why this sits beside the tools and not in them: the schema a provider sees and the schema the app enforces are two
documents, and only the first has a dialect to fit.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

# (JSON Schema keyword, sentence appended to the property's description). The keyword is removed from the schema.
_BOUNDS_AS_TEXT = (("minLength", "At least {} characters."), ("maxLength", "At most {} characters."))


def strict_tool_schema(tool: BaseTool) -> dict[str, Any]:
    """`tool` as an OpenAI `{"type": "function", "function": {..., "strict": True}}` dict that fits strict mode."""
    spec = convert_to_openai_tool(tool, strict=True)
    spec["function"]["parameters"] = _strictify(deepcopy(spec["function"]["parameters"]), [])
    return spec


def _strictify(schema: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    """Rewrite `schema` in place for strict mode. A bound or default that has to become prose is added to `notes`, which
    the caller (the property that owns this schema) folds into its description."""
    for keyword, sentence in _BOUNDS_AS_TEXT:
        if keyword in schema:
            notes.append(sentence.format(schema.pop(keyword)))
    if schema.get("default") is not None:
        notes.append(f"Default: {schema['default']}.")
    schema.pop("default", None)

    for branch in schema.get("anyOf", []):
        _strictify(branch, notes)
    if isinstance(schema.get("items"), dict):
        item_notes: list[str] = []
        _strictify(schema["items"], item_notes)
        notes.extend(f"Each item: {note[0].lower()}{note[1:]}" for note in item_notes)

    properties = schema.get("properties")
    if properties is not None:
        originally_required = set(schema.get("required", []))
        for name, prop in properties.items():
            if "default" in prop and prop["default"] is None and not _nullable(prop):
                _make_nullable(prop)
            prop_notes: list[str] = []
            _strictify(prop, prop_notes)
            if name not in originally_required and _nullable(prop):
                # Its own description may say "omit it"; a required field cannot be omitted, only null.
                prop_notes.append("Pass null to leave it out.")
            if prop_notes:
                prop["description"] = " ".join(filter(None, [prop.get("description", "").strip(), *prop_notes]))
        # Required means "must be present", not "must be non-null": an optional argument is a nullable one.
        schema["required"] = list(properties)
        schema["additionalProperties"] = False
    return schema


def _nullable(prop: dict[str, Any]) -> bool:
    return prop.get("type") == "null" or any(branch.get("type") == "null" for branch in prop.get("anyOf", []))


def _make_nullable(prop: dict[str, Any]) -> None:
    """`prop` becomes `anyOf: [<what it was>, null]`, keeping its description (and nothing else) at the top."""
    inner = {key: value for key, value in prop.items() if key not in ("description", "default")}
    description = prop.get("description")
    prop.clear()
    prop["anyOf"] = [inner, {"type": "null"}]
    if description:
        prop["description"] = description

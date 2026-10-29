"""The strict-mode form of a tool's schema: what `make_llm` sends when `TOOL_CALLS_STRICT` is on.

OpenAI's strict function calling (`"strict": true`) makes a supporting provider decode arguments against the
schema, so a call can only name an existing enum value and declared fields. It wants a narrower JSON Schema
dialect than Pydantic writes, and LangChain's `convert_to_openai_tool(tool, strict=True)` only closes the
objects. It doesn't make the dialect fit, and for a tool whose arguments are ALL optional it doesn't even
close the object (it keys on `required`, which Pydantic omits then), so the provider would reject the tool.
Found by converting every tool of every domain; `tests/core/test_tool_schema.py` repeats that on every run.
The rewrite:

- Every property is listed in `required` and every object is closed. An optional argument stays optional by
  being nullable (`Topic | None` is `anyOf: [enum, null]`), just no longer absent-able. A property whose only
  optionality was a `None` default becomes nullable. (The installed `openai` SDK's
  `_ensure_strict_json_schema` does the same three things and finds nothing left to change.)
- A `default` is removed, and a non-null one is told to the model in the description, because a field it can't
  omit has to say what "no preference" is. `minLength`/`maxLength` are removed and described the same way ("At
  most 200 characters."). That is the cautious reading of OpenAI's supported keywords, NOT run against a live
  strict provider (none is configured). If a provider accepts them, this costs a hint, not correctness.

Only the copy sent to the model is rewritten. A tool's `args_schema` is untouched, so a length bound is still
enforced when a call arrives and a call from a model that ignores `strict` is validated as before (`ToolArgs`
below accepts a nullable argument left out). The schema a provider sees and the one the app enforces are two
documents; only the first has a dialect to fit.
"""
from __future__ import annotations

from copy import deepcopy
from types import NoneType
from typing import Any, ClassVar, get_args

from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, model_validator

# (JSON Schema keyword, sentence appended to the property's description). The keyword is removed from the schema.
_BOUNDS_AS_TEXT = (("minLength", "At least {} characters."), ("maxLength", "At most {} characters."))


class ToolArgs(BaseModel):
    """Base of a tool's `args_schema`: every argument is REQUIRED in the schema, and a call may still leave out the ones that
    have a natural "nothing" (pattern 74).

    A strict provider needs every property listed in `required`, and `null` is the unmistakable way to say "no filter". So a
    filter is declared `T | None = Field(..., description="... Pass null for ...")`: required, nullable, no default, and that
    is what every model sees, strict provider or not. But the local models do not follow "required": `qwen2.5:7b` sends only the
    arguments it needs (`{'query': 'support hours', 'topic': 'company'}`) and, with the tools declared strictly, 7 of the probe's 19
    tool prompts failed validation, a retry round trip each. Nothing in a prompt or a description fixed that reliably
    (best case 18 of 19, and only with all of them stacked; the smaller models got worse). So the model's omission is accepted
    here: a missing nullable argument is filled with `None`, and `omitted_defaults` names the value for one that is not nullable
    (a closed choice like a priority has no null).

    Validation passing is not enough: LangChain hands the tool function only the keys the call CONTAINED
    (`BaseTool._parse_input` keeps `k in tool_input`), so the function's own signature still needs `= None` (or the default
    here). That default and this class say the same thing in two places, and `tests/core/test_tool_schema.py` calls every such tool
    with the arguments left out.
    """

    omitted_defaults: ClassVar[dict[str, Any]] = {}

    @model_validator(mode="before")
    @classmethod
    def _fill_omitted_arguments(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        filled = dict(data)
        for name, field in cls.model_fields.items():
            if name in filled or not field.is_required():
                continue
            if name in cls.omitted_defaults:
                filled[name] = cls.omitted_defaults[name]
            elif NoneType in get_args(field.annotation):
                filled[name] = None
        return filled


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

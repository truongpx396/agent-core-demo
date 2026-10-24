"""The four sandbox tools' free-form arguments are length-bounded in every domain (specs/009 A3, FR-011).

`command`, `script`, `path` and file `content` used to carry only a non-blank check, so one runaway generation became one runaway
call (and, for `content`, one runaway write). All three domains build the same four argument models and now share ONE limit,
`sandbox_session.SANDBOX_ARG_LIMIT` (SANDBOX_ARG_MAX_CHARS); a validation error names the limit, so the model can shorten and retry.
"""
import importlib

import pytest
from pydantic import ValidationError

from app.domains import sandbox_session

_DOMAINS = ["ops", "support", "sales"]
_FIELDS = [
    ("RunCommandInSandboxArgs", "command", {}),
    ("RunPythonInSandboxArgs", "script", {}),
    ("ReadSandboxFileArgs", "path", {}),
    ("WriteSandboxFileArgs", "path", {"content": "ok"}),
    ("WriteSandboxFileArgs", "content", {"path": "/tmp/f"}),
]


@pytest.mark.parametrize("domain", _DOMAINS)
@pytest.mark.parametrize("model_name, field, others", _FIELDS)
def test_a_value_over_the_limit_is_rejected_naming_the_limit(domain, model_name, field, others):
    model = getattr(importlib.import_module(f"app.domains.{domain}.tools"), model_name)
    too_long = "x" * (sandbox_session.SANDBOX_ARG_LIMIT + 1)

    with pytest.raises(ValidationError) as exc:
        model(**{field: too_long, **others}, tool_call_id="call_1")

    assert str(sandbox_session.SANDBOX_ARG_LIMIT) in str(exc.value)


@pytest.mark.parametrize("domain", _DOMAINS)
@pytest.mark.parametrize("model_name, field, others", _FIELDS)
def test_a_value_exactly_at_the_limit_is_accepted(domain, model_name, field, others):
    model = getattr(importlib.import_module(f"app.domains.{domain}.tools"), model_name)

    model(**{field: "x" * sandbox_session.SANDBOX_ARG_LIMIT, **others}, tool_call_id="call_1")


@pytest.mark.parametrize("domain", _DOMAINS)
def test_the_limit_is_in_the_schema_the_model_is_shown(domain):
    tools = importlib.import_module(f"app.domains.{domain}.tools")

    for model_name, field, _ in _FIELDS:
        schema = getattr(tools, model_name).model_json_schema()
        assert schema["properties"][field]["maxLength"] == sandbox_session.SANDBOX_ARG_LIMIT, (model_name, field)

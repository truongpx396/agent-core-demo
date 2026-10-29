"""`app/core/tool_schema.py`: the strict-mode form of every tool the chat model is offered, and `make_llm`'s switch for it.

What a strict provider needs is checked on the REAL tools of every domain, not on examples, because the failure was found
that way: LangChain's `strict=True` left six tools in a form the provider would refuse (an optional argument not in
`required`; for a tool whose arguments are all optional, the object not even closed). Those six now declare the argument
as required and nullable themselves (`test_no_tool_has_an_optional_argument...`); the conversion still has to move defaults and
length bounds out of what the provider sees. A tool added tomorrow with either shape fails here, not at a provider's 400.
What a provider then DOES with the schema needs a live strict provider and is not claimed here.
"""
import inspect
from types import NoneType
from typing import Annotated, get_args

import pytest
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field, ValidationError

from app.agent.graph.utils import make_llm
from app.agent.tools.builtin import SearchDocsArgs
from app.core.config import Settings, settings
from app.core.tool_schema import ToolArgs, strict_tool_schema
from app.domains.ecorp.tools import QueryEmployeesArgs
from app.domains.ops.tools import ListRecentIncidentsArgs, LogIncidentArgs
from app.domains.registry import DOMAINS
from app.domains.sales.tools import ListPendingFollowupsArgs
from app.domains.support.tools import CreateTicketArgs, TicketPriority


def _all_tools() -> dict:
    tools: dict = {}
    for _manifest, plugin in DOMAINS.values():
        for t in plugin.tools():
            tools.setdefault(t.name, t)
    return tools


ALL_TOOLS = _all_tools()
UNSUPPORTED = ("default", "minLength", "maxLength", "$ref", "$defs")


def _walk(schema, path="$"):
    """Every (path, node) in a JSON-Schema document."""
    if isinstance(schema, dict):
        yield path, schema
        for key, value in schema.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for i, value in enumerate(schema):
            yield from _walk(value, f"{path}[{i}]")


@pytest.mark.parametrize("name", sorted(ALL_TOOLS))
def test_every_tool_of_every_domain_fits_strict_mode(name):
    spec = strict_tool_schema(ALL_TOOLS[name])
    assert spec["function"]["strict"] is True
    problems = []
    for path, node in _walk(spec["function"]["parameters"]):
        if "properties" in node:
            if node.get("additionalProperties") is not False:
                problems.append(f"{path}: object is not closed")
            if node.get("required") != list(node["properties"]):
                problems.append(f"{path}: required {node.get('required')} != properties {list(node['properties'])}")
        problems += [f"{path}: has {keyword}" for keyword in UNSUPPORTED if keyword in node]
    assert not problems, f"{name}: {problems}"


@pytest.mark.parametrize("name", sorted(ALL_TOOLS))
def test_no_tool_has_an_optional_argument_only_required_nullable_ones(name):
    """The rule the strict form rests on: "no filter" is `null`, never an absent argument (pattern 74). A new tool with
    `Field(default=None)` fails here and not at a strict provider; write `Field(..., ...)` on a `T | None` instead."""
    schema = convert_to_openai_tool(ALL_TOOLS[name])["function"]["parameters"]
    optional = sorted(set(schema.get("properties", {})) - set(schema.get("required", [])))
    assert not optional, f"{name}: optional argument(s) {optional}; make them required and nullable"


NULLABLE_ARGS = [
    (SearchDocsArgs, {"query": "hours"}, ("topic", "doc_ids")),
    (QueryEmployeesArgs, {}, ("department", "name_contains")),
    (ListRecentIncidentsArgs, {}, ("status",)),
    (ListPendingFollowupsArgs, {}, ("contact",)),
    (LogIncidentArgs, {"summary": "s", "tool_call_id": "t"}, ("detail",)),
]


@pytest.mark.parametrize(("args_schema", "kwargs", "nullable"), NULLABLE_ARGS)
def test_a_call_that_leaves_a_nullable_argument_out_is_accepted_as_null_though_the_schema_requires_it(args_schema, kwargs, nullable):
    """What qwen2.5:7b does (it sends only the arguments it needs, and 7 of 19 probe prompts failed when that was refused)."""
    parsed = args_schema(**kwargs)
    assert all(getattr(parsed, name) is None for name in nullable)
    assert set(nullable) <= set(args_schema.model_json_schema()["required"])


def test_a_ticket_left_without_a_priority_is_normal_and_the_schema_still_requires_one():
    assert CreateTicketArgs(subject="s", description="d", tool_call_id="t").priority is TicketPriority.normal
    assert "priority" in CreateTicketArgs.model_json_schema()["required"]


def test_what_has_no_null_and_no_stated_default_is_still_refused():
    with pytest.raises(ValidationError, match="query"):
        SearchDocsArgs()  # type: ignore[call-arg]  # the point: `query` is missing


@pytest.mark.parametrize("name", sorted(ALL_TOOLS))
def test_the_function_defaults_whatever_a_call_may_leave_out(name):
    """LangChain passes the function only the keys the call contained, so validation filling `None` is not enough: without
    `= None` in the signature the tool dies with a TypeError on the very call the schema just accepted."""
    t = ALL_TOOLS[name]
    schema = t.args_schema
    if not (isinstance(schema, type) and issubclass(schema, ToolArgs)):
        return
    params = inspect.signature(t.coroutine or t.func).parameters
    tolerated = [n for n, f in schema.model_fields.items() if n in schema.omitted_defaults or (f.is_required() and NoneType in get_args(f.annotation))]
    assert tolerated, f"{name} is a ToolArgs that tolerates nothing: use BaseModel"
    assert all(params[n].default is not inspect.Parameter.empty for n in tolerated), f"{name}: {tolerated} need a default in the signature"


async def test_a_tool_whose_function_has_no_default_fails_on_the_call_validation_accepted():
    class Filter(ToolArgs):
        f: str | None = Field(..., description="Pass null for none.")

    @tool(args_schema=Filter)
    def with_default(f: str | None = None) -> str:
        """Ok."""
        return repr(f)

    @tool(args_schema=Filter)
    def without_default(f: str | None) -> str:
        """Not ok."""
        return repr(f)

    assert await with_default.ainvoke({}) == "None"
    with pytest.raises(TypeError):
        await without_default.ainvoke({})


def test_langchains_own_strict_conversion_is_not_enough_which_is_why_this_module_exists():
    """It sets the flag and closes objects, nothing else: a non-null default and a length bound survive it. If LangChain
    ever rewrites those, this module can be deleted: this test then fails, and says so."""
    raw = convert_to_openai_tool(_sample, strict=True)["function"]["parameters"]
    assert raw["properties"]["name"]["maxLength"] == 20 and raw["properties"]["priority"]["default"] == "normal"
    assert "nick" not in raw["required"]
    fixed = strict_tool_schema(_sample)["function"]["parameters"]
    assert "maxLength" not in fixed["properties"]["name"] and fixed["required"] == list(fixed["properties"])


def test_converting_a_tool_does_not_change_the_schema_the_app_enforces():
    """The strict copy is a copy: the tool's own `args_schema` (bounds included) is what still validates a call."""
    before = ALL_TOOLS["add_note"].args_schema.model_json_schema()
    strict_tool_schema(ALL_TOOLS["add_note"])
    assert ALL_TOOLS["add_note"].args_schema.model_json_schema() == before
    assert before["properties"]["title"]["maxLength"] == 200


class _Args(BaseModel):
    name: str = Field(..., min_length=1, max_length=20, description="Who.")
    nick: str | None = Field(None, max_length=5, description="Optional nickname, omit it if unknown.")
    tags: list[Annotated[str, Field(max_length=8)]] = Field(default_factory=list, description="Labels.")
    priority: str = Field("normal", description="How urgent.")
    unset: str = Field(None)  # type: ignore[assignment]  # a None default on a non-nullable type, as old code writes it


@tool(args_schema=_Args)
def _sample(name: str, nick: str | None = None, tags: list[str] | None = None, priority: str = "normal", unset: str = "") -> str:
    """A tool for the tests."""
    return name


class TestStrictify:
    @pytest.fixture
    def params(self):
        return strict_tool_schema(_sample)["function"]["parameters"]

    def test_every_property_is_required_and_the_object_is_closed(self, params):
        assert params["required"] == ["name", "nick", "tags", "priority", "unset"]
        assert params["additionalProperties"] is False

    def test_a_length_bound_moves_into_the_description_and_out_of_the_schema(self, params):
        assert "maxLength" not in params["properties"]["name"] and "minLength" not in params["properties"]["name"]
        assert params["properties"]["name"]["description"] == "Who. At least 1 characters. At most 20 characters."

    def test_an_optional_property_is_told_to_pass_null(self, params):
        nick = params["properties"]["nick"]
        assert {"type": "null"} in nick["anyOf"]
        assert "At most 5 characters." in nick["description"] and nick["description"].endswith("Pass null to leave it out.")

    def test_a_non_null_default_becomes_a_sentence_for_the_model(self, params):
        assert params["properties"]["priority"]["description"] == "How urgent. Default: normal."
        assert "default" not in params["properties"]["priority"]

    def test_a_none_default_on_a_non_nullable_type_becomes_nullable(self, params):
        assert params["properties"]["unset"]["anyOf"][-1] == {"type": "null"}

    def test_an_array_items_bound_is_attributed_to_each_item(self, params):
        assert "Each item: at most 8 characters." in params["properties"]["tags"]["description"]

    def test_a_tool_with_no_arguments_still_gets_a_closed_object(self):
        @tool
        def ping() -> str:
            """No arguments."""
            return "pong"

        params = strict_tool_schema(ping)["function"]["parameters"]
        assert params.get("required", []) == [] and params.get("additionalProperties") is False


class TestMakeLlmSwitch:
    TOOLS = [ALL_TOOLS["search_docs"], ALL_TOOLS["calculator"]]

    @staticmethod
    def _sent(bound) -> list[dict]:
        return bound.kwargs["tools"]  # type: ignore[attr-defined]  # RunnableBinding.kwargs

    def test_strict_binds_the_strict_form_in_the_given_order(self):
        sent = self._sent(make_llm(self.TOOLS, strict=True))
        assert [t["function"]["name"] for t in sent] == ["search_docs", "calculator"]
        assert all(t["function"]["strict"] is True for t in sent)
        assert sent[0]["function"]["parameters"]["required"] == ["query", "topic", "doc_ids"]

    def test_not_strict_binds_the_tools_as_before(self):
        sent = self._sent(make_llm([ALL_TOOLS["add_note"]], strict=False))
        assert "strict" not in sent[0]["function"]
        assert sent[0]["function"]["parameters"]["properties"]["title"]["maxLength"] == 200  # the bound is still in the schema

    def test_the_default_follows_the_setting(self, monkeypatch):
        monkeypatch.setattr(settings, "tool_calls_strict", True)
        assert self._sent(make_llm(self.TOOLS))[0]["function"]["strict"] is True
        monkeypatch.setattr(settings, "tool_calls_strict", False)
        assert "strict" not in self._sent(make_llm(self.TOOLS))[0]["function"]

    def test_it_is_off_unless_asked_for(self):
        """Measured, not assumed (pattern 74): on the local Ollama path it buys nothing and the smallest CI model
        lost 3 of 19 probe prompts.
        """
        assert Settings.model_fields["tool_calls_strict"].default is False

"""`make_llm` (app/agent/graph/utils.py): the application decides WHAT to build (the configured alias, endpoint, key and the tool shape), the
`ChatModels` port builds it. Against a recording fake here; what the real adapter builds is tests/adapters/litellm/test_chat_model.py."""
from langchain_core.tools import tool

from app.agent.graph import utils
from app.core.config import CHAT_MODEL, OPENAI_API_BASE, OPENAI_API_KEY
from app.core.tool_schema import strict_tool_schema
from app.ports.llm import chat_models


class _Recorder:
    def __init__(self):
        self.calls: list[tuple[list, dict]] = []

    def build(self, tools, *, model, base_url, api_key):
        self.calls.append((tools, {"model": model, "base_url": base_url, "api_key": api_key}))
        return "a chat model"


@tool
def lookup(query: str) -> str:
    """Looks something up."""
    return query


def test_it_builds_the_configured_alias_at_the_configured_endpoint_through_the_port():
    recorder = _Recorder()
    with chat_models.overriding(recorder):
        built = utils.make_llm([lookup], strict=False)

    assert built == "a chat model"
    (tools, kwargs), = recorder.calls
    assert tools == [lookup]
    assert kwargs == {"model": CHAT_MODEL, "base_url": OPENAI_API_BASE, "api_key": OPENAI_API_KEY}


def test_a_caller_can_point_it_at_another_endpoint():
    recorder = _Recorder()
    with chat_models.overriding(recorder):
        utils.make_llm([], model="m", base_url="http://other/v1", api_key="k", strict=False)

    assert recorder.calls[0][1] == {"model": "m", "base_url": "http://other/v1", "api_key": "k"}


def test_strict_tool_schemas_are_the_applications_decision_not_the_adapters():
    recorder = _Recorder()
    with chat_models.overriding(recorder):
        utils.make_llm([lookup], strict=True)

    (tools, _), = recorder.calls
    assert tools == [strict_tool_schema(lookup)]

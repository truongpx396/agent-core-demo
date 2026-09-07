"""A real crawl4ai round trip THROUGH the graph, not just through
app/ingestion/web_crawler.py directly (see test_web_crawler_live.py for
that narrower check) — proves the actual wiring in
app/domains/ops/tools.py::check_vendor_status_page and
app/domains/support/tools.py::fetch_external_reference (GRAPH_PATTERNS.md
pattern 50) end to end: a scripted tool call pauses at human_approval
(mandatory, "outward"), and once approved, ACTUALLY renders a real page and
returns its content as a ToolMessage — not a mocked
`render_url_to_markdown` the way tests/domains/ops/test_domain.py and
tests/domains/support/test_domain.py's own approval-gate tests use.

No LLM needed — `GenericFakeChatModel` scripts the tool call, same
technique every tests/domains/*/test_domain.py file already uses — only
the CRAWL leg is real, hence `@pytest.mark.crawl` (not `llm`) and no
`ollama_endpoint` fixture. Neither domain's tool here touches Postgres
either (`check_vendor_status_page`/`fetch_external_reference` are pure
crawl-and-return, unlike e.g. sales's `enrich_lead_from_website`, which
also needs a real lead row — left to a hermetic, mocked-store test instead
since covering that combination live would need a real Postgres too, for
comparatively little extra proof over what this file already establishes).
"""
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from app.agent.graph import GraphDeps, build_graph
from app.domains.ops.domain import OPS_DOMAIN_PLUGIN, OPS_MANIFEST
from app.domains.support.domain import SUPPORT_DOMAIN_PLUGIN, SUPPORT_MANIFEST
from tests.conftest import TEST_CTX

pytestmark = pytest.mark.crawl


def _tool_call(name, args, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _config(thread_id):
    return {"configurable": {"thread_id": thread_id, "ctx": TEST_CTX}}


def _fake_llm_returning(*responses):
    return GenericFakeChatModel(messages=iter(responses))


def test_check_vendor_status_page_actually_crawls_once_approved():
    llm = _fake_llm_returning(
        _tool_call("check_vendor_status_page", {"url": "https://example.com"}),
        AIMessage(content="Their status page shows no ongoing incident."),
    )
    g = build_graph(GraphDeps(llm=llm), manifest=OPS_MANIFEST, domain=OPS_DOMAIN_PLUGIN)
    config = _config("live-crawl-ops-thread")

    g.invoke(
        {"messages": [HumanMessage(content="is our vendor having an outage?")]}, config=config
    )
    assert g.get_state(config).next  # paused for approval, not finished

    result = g.invoke(Command(resume=True), config=config)
    assert not g.get_state(config).next  # finished, not paused

    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert any("Example Domain" in m.content for m in tool_messages)


def test_fetch_external_reference_actually_crawls_once_approved():
    llm = _fake_llm_returning(
        _tool_call("fetch_external_reference", {"url": "https://example.com"}),
        AIMessage(content="Here's what that page says."),
    )
    g = build_graph(GraphDeps(llm=llm), manifest=SUPPORT_MANIFEST, domain=SUPPORT_DOMAIN_PLUGIN)
    config = _config("live-crawl-support-thread")

    g.invoke(
        {"messages": [HumanMessage(content="can you check this page the customer linked?")]},
        config=config,
    )
    assert g.get_state(config).next  # paused for approval, not finished

    result = g.invoke(Command(resume=True), config=config)
    assert not g.get_state(config).next  # finished, not paused

    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert any("Example Domain" in m.content for m in tool_messages)

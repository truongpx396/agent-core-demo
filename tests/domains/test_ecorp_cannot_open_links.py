"""The Ecorp domain has no tool that opens a page, and its prompt now says so (GRAPH_PATTERNS.md pattern 79).

Langfuse trace `889b5405`: "run a subAgent to check what this is https://jsonviewer.stack.hu/" was
answered with a confident description of the page (syntax highlighting, "keys in blue, values in
green") that nothing had fetched. The domain's tools are a knowledge-base search, a calculator, a staff
directory, notes and memory; its subagent looks up Ecorp facts. The honest answer was "I can't open
links", and the prompt never said it.

The support, ops and sales domains DO have a tool that opens an address, so the sentence belongs to the
domain that cannot, and these tests keep it true in both directions.
"""
from app.agent.graph.links import LINK_TOOL_NAMES as _TOOLS_THAT_REACH_AN_ADDRESS
from app.agent.graph.links import cannot_open_links
from app.domains.ecorp.domain import ECORP_DOMAIN_PLUGIN, ECORP_SYSTEM_PROMPT
from app.domains.registry import resolve_domain

_STATEMENT = "You cannot open web pages or links"


def test_the_ecorp_prompt_says_it_cannot_open_links():
    assert _STATEMENT in ECORP_SYSTEM_PROMPT
    assert "instead of describing what is on the page" in ECORP_SYSTEM_PROMPT


def test_the_statement_is_true_ecorp_has_no_tool_that_reaches_an_address():
    names = {t.name for t in ECORP_DOMAIN_PLUGIN.tools()}
    assert not names & _TOOLS_THAT_REACH_AN_ADDRESS, (
        "Ecorp gained a tool that reaches an address: drop 'You cannot open web pages or links' from its prompt"
    )


def test_the_domains_that_can_reach_an_address_do_not_say_they_cannot():
    for name in ("support", "ops", "sales"):
        manifest, domain = resolve_domain(name)
        assert {t.name for t in domain.tools()} & _TOOLS_THAT_REACH_AN_ADDRESS, f"{name} lost its address-reaching tool"
        assert _STATEMENT not in manifest.system_prompt


def test_only_ecorp_is_told_to_remind_the_model_when_a_message_has_a_link():
    """What `build_graph` hands the agent node: the reminder is for the domain with no such tool."""
    wants_it = {}
    for name in ("ecorp", "support", "ops", "sales"):
        _manifest, domain = resolve_domain(name)
        wants_it[name] = cannot_open_links({t.name for t in domain.tools()})
    assert wants_it == {"ecorp": True, "support": False, "ops": False, "sales": False}

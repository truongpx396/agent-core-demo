"""A skill tells the agent which tools to call, so every domain that can see a skill must have the tools it names.

A skill in `shared/` is seen by every domain; one in a domain's folder only by that domain (`app/agent/tools/skill_catalog.py`). A skill that names a tool
its reader lacks is advertised by `skill_search`, loaded by `use_skill`, and then cannot be followed: `onboarding-brief` calls `query_employees` and `expense-summary` calls
`calculator`, which only Ecorp has, while both were visible to all four domains. That is why neither is in `shared/`.
"""
import re

from app.agent.tools.skill_catalog import load_skills
from app.domains.registry import DOMAINS

# (skill, domain, tool): a skill that NAMES a tool its domain lacks, on purpose, to say it is not enough. `deal-economics` (sales) explains that `calculator` cannot hold a
# loop and sends the agent to the sandbox. Not a gap: delete the entry if the skill stops saying it.
MENTIONED_NOT_CALLED = frozenset({("deal-economics", "sales", "calculator")})


def tools_of(domain: str) -> set[str]:
    manifest, plugin = DOMAINS[domain]
    return set(manifest.allowed_tools) or {tool.name for tool in plugin.tools()}


def unreachable(skills: dict, tools_by_domain: dict[str, set[str]]) -> set[tuple[str, str, str]]:
    """`(skill, domain, tool)` for each tool a skill's body names (in backticks) that a domain which can see the skill does not have."""
    every_tool = set().union(*tools_by_domain.values())
    found = set()
    for name, record in skills.items():
        named = set(re.findall(r"`([a-z_]+)`", record.body)) & every_tool
        for domain, tools in tools_by_domain.items():
            if record.domains is None or domain in record.domains:
                found |= {(name, domain, tool) for tool in named - tools}
    return found


def test_a_domain_that_can_see_a_skill_has_every_tool_the_skill_names():
    skills = load_skills()
    assert skills, "no shipped skills found: the layout moved"

    found = unreachable(skills, {domain: tools_of(domain) for domain in DOMAINS})

    assert sorted(found - MENTIONED_NOT_CALLED) == [], "a skill is visible to a domain that lacks a tool it tells the agent to call: move the skill into the domain that has it"
    assert sorted(MENTIONED_NOT_CALLED - found) == [], "this skill no longer names the tool: delete the entry from MENTIONED_NOT_CALLED"


def test_the_check_can_fail():
    class Skill:
        def __init__(self, body, domains):
            self.body, self.domains = body, domains

    tools = {"ecorp": {"query_employees", "search_docs"}, "sales": {"search_docs"}}
    skills = {
        "everyone": Skill("Call `query_employees`, then `search_docs`. Not a tool: `topic`.", None),
        "ecorp-only": Skill("Call `query_employees`.", ("ecorp",)),
        "sales-only": Skill("Call `search_docs`.", ("sales",)),
    }

    assert unreachable(skills, tools) == {("everyone", "sales", "query_employees")}

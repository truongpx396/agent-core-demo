"""The Ecorp domain as the default fixture for graph tests.

The graph is domain-agnostic: `build_graph` takes the manifest and the domain plugin, and the
generic guards (`should_continue`, `check_output`, `invalid_tool_call`, ...) take the calling
domain's tool capabilities, tool names and system prompt as arguments, with no default. Most graph
tests only need *a* domain, and Ecorp's is the richest one (retrieval, a calculator, a mutating
tool, a read-only directory), so these wrappers bind the generic functions to it.

A test that is about a different domain passes its own manifest/domain/arguments explicitly, which
always wins over the default here.
"""
from app.agent.graph import build as _build
from app.agent.graph import build_subagent as _build_subagent
from app.agent.graph import cache as _cache
from app.agent.graph import loop_guards as _loop_guards
from app.agent.graph import retry as _retry
from app.agent.graph import routing as _routing
from app.agent.graph import tool_guards as _tool_guards
from app.agent.tools import run_subagent as _run_subagent
from app.domains.ecorp.domain import (
    ECORP_DOMAIN_PLUGIN,
    ECORP_MANIFEST,
    ECORP_SYSTEM_PROMPT,
)

ECORP_TOOL_CAPABILITIES = ECORP_DOMAIN_PLUGIN.tool_capabilities()
ECORP_TOOL_NAMES = frozenset(t.name for t in ECORP_DOMAIN_PLUGIN.tools())

__all__ = [
    "ECORP_DOMAIN_PLUGIN",
    "ECORP_MANIFEST",
    "ECORP_SYSTEM_PROMPT",
    "ECORP_TOOL_CAPABILITIES",
    "ECORP_TOOL_NAMES",
    "build_graph",
    "build_subagent_graph",
    "check_output",
    "invalid_tool_call",
    "invalid_tool_call_names",
    "make_no_answer_fallback_node",
    "make_write_semantic_cache_node",
    "mandatory_gate_reason",
    "run_subagent_impl",
    "should_continue",
    "tool_capability",
]


def build_graph(deps=None, checkpointer=None, manifest=None, domain=None, **kwargs):
    return _build.build_graph(
        manifest or ECORP_MANIFEST, domain or ECORP_DOMAIN_PLUGIN, deps, checkpointer, **kwargs
    )


def build_subagent_graph(deps=None, checkpointer=None, manifest=None, domain=None, **kwargs):
    return _build_subagent.build_subagent_graph(
        manifest or ECORP_MANIFEST, domain or ECORP_DOMAIN_PLUGIN, deps, checkpointer, **kwargs
    )


def should_continue(
    state, tool_capabilities=ECORP_TOOL_CAPABILITIES, valid_tool_names=ECORP_TOOL_NAMES, *args, **kwargs
):
    return _routing.should_continue(state, tool_capabilities, valid_tool_names, *args, **kwargs)


def check_output(state, system_prompt=ECORP_SYSTEM_PROMPT, enforce_citations=True):
    return _routing.check_output(state, system_prompt, enforce_citations)


def invalid_tool_call(state, valid_tool_names=ECORP_TOOL_NAMES):
    return _tool_guards.invalid_tool_call(state, valid_tool_names)


def invalid_tool_call_names(tool_calls, valid_tool_names=ECORP_TOOL_NAMES):
    return _tool_guards.invalid_tool_call_names(tool_calls, valid_tool_names)


def tool_capability(name, tool_capabilities=ECORP_TOOL_CAPABILITIES):
    return _loop_guards.tool_capability(name, tool_capabilities)


def mandatory_gate_reason(tool_calls, tool_capabilities=ECORP_TOOL_CAPABILITIES):
    return _loop_guards.mandatory_gate_reason(tool_calls, tool_capabilities)


def make_write_semantic_cache_node(cache_set, tool_capabilities=ECORP_TOOL_CAPABILITIES):
    return _cache.make_write_semantic_cache_node(cache_set, tool_capabilities)


def make_no_answer_fallback_node(emit_message=True, *, system_prompt=ECORP_SYSTEM_PROMPT, enforce_citations=True):
    return _retry.make_no_answer_fallback_node(
        emit_message, system_prompt=system_prompt, enforce_citations=enforce_citations
    )


async def run_subagent_impl(subagent_name, task, config, *, domain="ecorp", registry, tools_by_name=None, **kwargs):
    """`run_subagent_impl` for the Ecorp domain: its tool universe is the default `tools_by_name`."""
    if tools_by_name is None:
        tools_by_name = {t.name: t for t in ECORP_DOMAIN_PLUGIN.tools()}
    return await _run_subagent.run_subagent_impl(
        subagent_name, task, config, domain=domain, registry=registry, tools_by_name=tools_by_name, **kwargs
    )

"""The sampling options and the model behind the dev gateway's tool-calling chat aliases (GRAPH_PATTERNS.md pattern 73).

Langfuse trace `1e46b2c8` (2026-10-21): `remember` never ran. `qwen2.5:3b` at `repeat_penalty: 1.3` wrote malformed tool-call JSON
(`{"content: "..."}`, a missing quote), Ollama's parser silently discards a call it cannot read, and the graph saw an empty reply.
`scripts/toolcall_probe.py` measured it on the app's real tools: the 3b answered 8/19 tool-needing prompts at 1.3, 13/19 at 1.1 and
15/19 even at 1.0; the 7b answered 11/19 at 1.3 (fragments such as `brtc {"name": "calculator"...`) and 19/19 at 1.1 and at 1.0. The penalty
falls on tokens already in the context, and the quote and colon tokens of a tool call are in it dozens of times.

A TEXT test of the config, like `test_gateway_aliases_config.py`: no Ollama needed. It cannot say a model is good (`make probe-tools`
does, against a live Ollama); it stops the two known ways back to the bug: raising the penalty, and putting a model measured to miss
tool calls behind the chat alias.
"""
from pathlib import Path

import pytest
import yaml

DEV = Path(__file__).resolve().parents[2] / "deploy/litellm/litellm-config.yaml"
# The aliases the agent binds tools to. `vision` is left out on purpose: no local vision model supports tools (see its entry).
TOOL_ALIASES = ("chat", "chat-backup", "chat-strong")
# Ollama's own default is 1.1. The probe's 7b results: 19/19 at 1.1, 19/19 at 1.0, 11/19 at 1.3.
MAX_REPEAT_PENALTY = 1.1
# Measured with scripts/toolcall_probe.py on this app's tools (pattern 73), tool-needing prompts answered correctly out of 19, at
# repeat_penalty 1.3 / 1.1 / 1.0: qwen2.5:3b 8 / 13 / 15 (and at 1.1 it also loops on open-ended prompts, so no setting saves it);
# qwen2.5:1.5b 5 / 12 / 12. Both are under the probe's own bar (90%) at every penalty. Remove a name only after `make probe-tools`
# has passed for it with the penalty the config sets. (Models not named here were not measured, which is not the same as passing.)
MEASURED_TO_MISS_TOOL_CALLS = {"qwen2.5:3b", "qwen2.5:1.5b"}


def _params(alias: str) -> dict:
    (entry,) = [m for m in yaml.safe_load(DEV.read_text())["model_list"] if m["model_name"] == alias]
    return entry["litellm_params"]


def _tag(alias: str) -> str:
    return _params(alias)["model"].partition("/")[2]


@pytest.mark.parametrize("alias", TOOL_ALIASES)
def test_the_repeat_penalty_stays_where_tool_call_json_survives(alias):
    """Above 1.1 even the 7b turns tool calls into text fragments. The old value was 1.3."""
    assert _params(alias)["repeat_penalty"] <= MAX_REPEAT_PENALTY, (
        f"{alias}: repeat_penalty above {MAX_REPEAT_PENALTY} corrupts tool-call JSON; run `make probe-tools` before raising it"
    )


@pytest.mark.parametrize("alias", TOOL_ALIASES)
def test_no_tool_alias_serves_a_model_measured_to_miss_tool_calls(alias):
    assert _tag(alias) not in MEASURED_TO_MISS_TOOL_CALLS, (
        f"{alias} serves {_tag(alias)}, which missed tool calls on the app's own tools; see pattern 73 and `make probe-tools`"
    )


def test_the_fallback_target_is_the_same_model_so_a_retry_behaves_like_the_first_try():
    assert _params("chat-backup")["model"] == _params("chat")["model"]


def test_every_tool_alias_still_asks_for_the_context_and_output_bounds_the_graph_is_sized_against():
    for alias in TOOL_ALIASES:
        assert _params(alias)["num_ctx"] == 32000 and _params(alias)["num_predict"] == 2048, alias

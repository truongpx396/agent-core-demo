"""Does the configured chat model emit a tool call the app can use? A quick check, to run before a model or sampling change ships.

The model behind the gateway's `chat` alias is chosen by what it can do with THIS app's tools, and the setting that
decides it is easy to get wrong without noticing. Langfuse trace `1e46b2c8` (2026-10-21): with `qwen2.5:3b` at
`repeat_penalty: 1.3`, 11 of the 22 prompts it then held came back from Ollama as a reply with output tokens, no text and no tool
call (the model wrote `{"name": "remember", "arguments": {"content: "..."}}`, a missing quote, and Ollama's parser
discards a call it cannot read without a word). Nothing in the graph can tell that from an empty reply. This script can.

It sends one labelled prompt at a time to Ollama's native chat endpoint, exactly as the gateway's `ollama_chat`
provider does: the app's real system prompt and tool schemas (taken from the Ecorp domain, not copied), temperature 0, and
the options the chosen alias sets in `deploy/litellm/litellm-config.yaml` (`num_ctx`, `num_predict`, `repeat_penalty`).
Temperature 0 makes one run per prompt the whole distribution. Each reply is put in one class:

    ok                     the right tool with arguments that satisfy its schema (or, for a no-tool prompt, plain text)
                           (and, where a prompt names them, the arguments the QUESTION asks for: a call can satisfy the schema and still
                           be the wrong one, e.g. a job title typed into a name filter)
    DROPPED                output tokens but no text and no tool call: the case above
    prose_instead_of_call  text where a call was needed (the garbled-JSON-as-text form included)
    wrong_tool / bad_args (a schema violation, or the wrong values for the question) / unneeded_call / empty

    python -m scripts.toolcall_probe                          # the `chat` alias exactly as configured
    python -m scripts.toolcall_probe --alias chat-strong
    python -m scripts.toolcall_probe --model qwen2.5:7b --repeat-penalty 1.3 1.1 1.0   # what a setting does

It also asks four open-ended prompts with no tools, because the other half of the trade-off is a repetition loop: a high
`repeat_penalty` was added (1.3) to stop `qwen2.5:3b` repeating itself until `num_predict` cut it off, and lowering it on a small
model brings that back (at 1.1 the 3b ran to the 2048-token cap with 44% of its 6-word runs repeated on "list every kind of database
index"). A reply that hits the cap with at least a quarter of its 6-word runs repeated is a LOOP. `--no-loops` skips this half,
which is most of the run time (about two minutes per penalty on an M4 Pro with the 7b; the tool half takes seconds).

Needs a native Ollama with the model pulled. It exits 1 when any reply was DROPPED, any open-ended reply LOOPED, or fewer than
`--min-ok` of the tool-needing prompts were OK. Twenty-two prompts separate a broken setup from a working one; they cannot rank two good
models, and a pass says nothing about answer quality (`make eval`, `make promptfoo` and the deepeval tier do).
"""
import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.agent.runtime.core import DEFAULT_DOMAIN
from app.bootstrap import wire
from app.core.config import settings as app_settings
from app.core.tool_schema import strict_tool_schema
from app.domains.registry import resolve_domain

CONFIG = Path(__file__).resolve().parents[1] / "deploy" / "litellm" / "litellm-config.yaml"
# The options the gateway forwards to Ollama; anything else in `litellm_params` is routing, not sampling.
FORWARDED = ("num_ctx", "num_predict", "repeat_penalty")

OK = "ok"
DROPPED = "DROPPED"
PROSE = "prose_instead_of_call"
WRONG_TOOL = "wrong_tool"
BAD_ARGS = "bad_args"
UNNEEDED = "unneeded_call"
EMPTY = "empty"


# Open-ended on purpose: these invite a long list, the shape a small model repeats itself in.
OPEN_ENDED: tuple[str, ...] = (
    "Showcase many examples of how to use Python list comprehensions.",
    "Give me 40 different greetings in as many languages as you can.",
    "List every kind of database index you know, with an example of each.",
    "Write a long story about a robot learning to paint, with many scenes.",
)
LOOP_SHARE = 0.25  # of 6-word runs that repeat an earlier one, at or above which a capped reply is a loop (observed: 0.44 looping, at most 0.12 not)


def repeated_share(text: str, n: int = 6) -> float:
    """The share of the text's n-word runs that repeat an earlier run: 0.0 for text that never says the same thing twice."""
    words = text.split()
    runs = [tuple(words[i : i + n]) for i in range(max(0, len(words) - n + 1))]
    return 0.0 if not runs else 1 - len(set(runs)) / len(runs)


def looped(done_reason: str | None, text: str) -> bool:
    """Ran to the output cap (`done_reason == "length"`) while repeating itself. A long answer that ends by itself is not a loop."""
    return done_reason == "length" and repeated_share(text) >= LOOP_SHARE


@dataclass(frozen=True)
class Probe:
    text: str
    expected: str | None  # the tool a good reply calls, or None when no tool is needed
    # The values a good call carries, for the keys that matter (others are not compared): a string case-insensitively, None for "not
    # narrowed" (null or left out). A call can satisfy the schema and still answer a different question: CI's qwen2.5:3b typed the job title
    # "Support Lead" into `name_contains`, a valid call that matched nobody, and the aggregate count of OK replies never showed it.
    args: dict | None = None


PROBES: tuple[Probe, ...] = (
    Probe("remember that my favourite colour is blue", "remember"),
    Probe('remember "abc"', "remember"),
    Probe("remember that I prefer short answers", "remember"),
    Probe("please remember my manager is Dana", "remember"),
    Probe("save this for later: the deploy window is Friday 3pm", "remember"),
    Probe("remember I work in the Sales department", "remember"),
    Probe("add a note titled Holiday hours under company: support is closed on Christmas", "add_note"),
    Probe("save a note in the qdrant topic: payload filters are fast", "add_note"),
    Probe("create a note about langgraph checkpointers saying they persist state", "add_note"),
    Probe("what are the support hours?", "search_docs"),
    Probe("how does Qdrant payload filtering work?", "search_docs"),
    Probe("what is a LangGraph checkpointer?", "search_docs"),
    Probe("what is Ecorp's refund policy?", "search_docs"),
    Probe("what is 17 * 23?", "calculator"),
    Probe("calculate 1234 + 5678", "calculator"),
    Probe("what's 15% of 240?", "calculator"),
    Probe("who works in engineering?", "query_employees"),
    Probe("list the people in the Sales department", "query_employees"),
    Probe("is there anyone named Maria?", "query_employees"),
    # A ROLE is not a name: the right call narrows by department and leaves the name alone (the live gate went red on this, 2026-10-24).
    Probe("who is the Support Lead?", "query_employees", {"department": "Support", "name_contains": None}),
    Probe("who is the Engineering Manager?", "query_employees", {"department": "Engineering", "name_contains": None}),
    Probe("is there anyone named Dana at Ecorp?", "query_employees", {"name_contains": "Dana"}),
    Probe("hi", None),
    Probe("thanks!", None),
    Probe("what is the capital of France?", None),
)


def alias_settings(alias: str, config: Path = CONFIG) -> tuple[str, str, dict[str, float]]:
    """`(ollama model tag, base url, forwarded options)` for a gateway alias in the dev config."""
    entries = [m for m in yaml.safe_load(config.read_text())["model_list"] if m["model_name"] == alias]
    if len(entries) != 1:
        raise SystemExit(f"{config.name} defines {len(entries)} entries named {alias!r}, expected exactly one")
    params = entries[0]["litellm_params"]
    provider, _, model = params["model"].partition("/")
    if provider != "ollama_chat":
        raise SystemExit(f"{alias!r} is served by {provider!r}, not Ollama: nothing to probe here")
    # The config is written for the gateway inside Docker; this script runs on the host.
    base = params.get("api_base", "http://localhost:11434").replace("host.docker.internal", "localhost")
    return model, base, {k: params[k] for k in FORWARDED if k in params}


def _nullable(spec: dict) -> bool:
    return any(branch.get("type") == "null" for branch in spec.get("anyOf", []))


def valid_args(schema: dict, args: object) -> bool:
    """Does `args` satisfy the tool's parameter schema as far as the app depends on it: an object, every required key present
    (a nullable one may be left out: `ToolArgs` fills it with None, so the call runs), no key the schema does not declare, and
    any enum respected."""
    if not isinstance(args, dict):
        return False
    props = schema.get("properties", {})
    missing = [key for key in schema.get("required", []) if key not in args and not _nullable(props.get(key, {}))]
    if missing or any(key not in props for key in args):
        return False
    for key, value in args.items():
        spec = props[key]
        enum = spec.get("enum") or next((a["enum"] for a in spec.get("anyOf", []) if a.get("enum")), None)
        if enum and value is not None and value not in enum:
            return False
    return True


def args_match(expected: dict, args: object) -> bool:
    """Are these the arguments the question asks for? Only the keys the probe names are compared: a string case-insensitively, None
    meaning "not narrowed" (null, an empty string, or left out)."""
    if not isinstance(args, dict):
        return False
    for key, want in expected.items():
        got = args.get(key)
        if want is None:
            if got not in (None, ""):
                return False
        elif str(got).strip().lower() != str(want).lower():
            return False
    return True


def classify(probe: Probe, message: dict, eval_count: int, schemas: dict[str, dict]) -> str:
    """The class of one reply, from Ollama's `message` and `eval_count` (output tokens generated)."""
    calls = message.get("tool_calls") or []
    text = (message.get("content") or "").strip()
    if calls:
        name, args = calls[0]["function"]["name"], calls[0]["function"]["arguments"]
        if probe.expected is None:
            return UNNEEDED
        if name != probe.expected:
            return WRONG_TOOL
        if not valid_args(schemas.get(name, {}), args):
            return BAD_ARGS
        return OK if probe.args is None or args_match(probe.args, args) else BAD_ARGS
    if not text:
        return DROPPED if eval_count > 0 else EMPTY
    return OK if probe.expected is None else PROSE


def app_tools_and_prompt(strict: bool | None = None) -> tuple[list[dict], str]:
    """The tool schemas and system prompt the Ecorp graph sends, built the way `build_graph` builds them. `strict=None` follows
    TOOL_CALLS_STRICT, so by default the probe sends the form the app sends (pattern 74)."""
    manifest, domain = resolve_domain(DEFAULT_DOMAIN)
    tools = domain.tools()
    if manifest.allowed_tools:
        tools = [t for t in tools if t.name in set(manifest.allowed_tools)]
    use_strict = app_settings.tool_calls_strict if strict is None else strict
    return [strict_tool_schema(t) if use_strict else convert_to_openai_tool(t) for t in tools], manifest.system_prompt


def chat(client: httpx.Client, base: str, model: str, options: dict, messages: list[dict], tools: list[dict] | None = None) -> dict:
    payload: dict = {"model": model, "stream": False, "messages": messages, "options": {**options, "temperature": 0}}
    if tools:
        payload["tools"] = tools
    reply = client.post(f"{base}/api/chat", json=payload, timeout=600)
    reply.raise_for_status()
    return reply.json()


def ask(client: httpx.Client, base: str, model: str, options: dict, system: str, tools: list[dict], text: str) -> tuple[dict, int]:
    body = chat(client, base, model, options, [{"role": "system", "content": system}, {"role": "user", "content": text}], tools)
    return body["message"], body.get("eval_count", 0)


def probe(client: httpx.Client, base: str, model: str, options: dict, strict: bool | None = None) -> list[tuple[Probe, str]]:
    tools, system = app_tools_and_prompt(strict)
    # A reply is judged against the schema the app ENFORCES, not the one it sent: `valid_args` lets a nullable argument be left
    # out, which is what the tools accept (`ToolArgs`) and what a model that ignores "required" does.
    schemas = {t["function"]["name"]: t["function"].get("parameters", {}) for t in app_tools_and_prompt(False)[0]}
    results = []
    for p in PROBES:
        message, eval_count = ask(client, base, model, options, system, tools, p.text)
        results.append((p, classify(p, message, eval_count, schemas)))
    return results


def probe_loops(client: httpx.Client, base: str, model: str, options: dict) -> list[tuple[str, bool, float]]:
    """`(prompt, looped, repeated share)` for each open-ended prompt, asked with no tools and no system prompt."""
    out = []
    for text in OPEN_ENDED:
        body = chat(client, base, model, options, [{"role": "user", "content": text}])
        reply = body["message"].get("content") or ""
        out.append((text, looped(body.get("done_reason"), reply), repeated_share(reply)))
    return out


def summarize(results: list[tuple[Probe, str]]) -> tuple[int, int, Counter]:
    """`(tool-needing prompts answered OK, tool-needing prompts, count of every class)`."""
    needed = [cls for p, cls in results if p.expected]
    return sum(1 for cls in needed if cls == OK), len(needed), Counter(cls for _, cls in results)


def main(argv: list[str] | None = None, client: httpx.Client | None = None) -> int:
    wire()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--alias", default="chat", help="gateway alias to read the model and options from (default: chat)")
    parser.add_argument("--model", help="an Ollama model tag to use instead of the alias's")
    parser.add_argument("--repeat-penalty", type=float, nargs="+", help="override repeat_penalty; several values run one pass each")
    parser.add_argument("--no-loops", action="store_true", help="skip the open-ended repetition check (most of the run time)")
    parser.add_argument("--strict", action=argparse.BooleanOptionalAction, default=None,
                        help="send the tools as strict-mode schemas (default: whatever TOOL_CALLS_STRICT says, i.e. what the app sends)")
    parser.add_argument("--min-ok", type=float, default=0.9, help="fail below this share of tool-needing prompts answered OK (default 0.9)")
    args = parser.parse_args(argv)

    model, base, options = alias_settings(args.alias)
    model = args.model or model
    failed = False
    with client or httpx.Client() as client:
        for penalty in args.repeat_penalty or [options.get("repeat_penalty")]:
            settings = {**options, **({"repeat_penalty": penalty} if penalty is not None else {})}
            results = probe(client, base, model, settings, args.strict)
            ok, needed, counts = summarize(results)
            print(f"{model}  {settings}  strict={app_settings.tool_calls_strict if args.strict is None else args.strict}\n  tool-needing prompts OK: {ok}/{needed}   classes: {dict(sorted(counts.items()))}")
            for p, cls in results:
                if cls != OK:
                    print(f"    {cls:22s} want={p.expected or '(no tool)':16s} {p.text}")
            failed = failed or counts[DROPPED] > 0 or ok < args.min_ok * needed
            if not args.no_loops:
                loops = probe_loops(client, base, model, settings)
                print(f"  open-ended prompts that looped: {sum(1 for _, bad, _ in loops if bad)}/{len(loops)}   repeated 6-word runs: "
                      + ", ".join(f"{share:.0%}" for _, _, share in loops))
                for text, bad, share in loops:
                    if bad:
                        print(f"    LOOP                   ran to the cap repeating itself ({share:.0%}): {text}")
                failed = failed or any(bad for _, bad, _ in loops)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

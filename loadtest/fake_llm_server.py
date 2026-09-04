"""OpenAI-compatible fake LLM server — a load-testing double for whatever
actually serves inference, so `POST /chat/stream/queued`'s concurrency
(app/turns/agent_worker.py's own `_MAX_CONCURRENCY` + pooled checkpointer)
can be measured in isolation. Native Ollama on this project's own dev stack
is NOT concurrent — verified directly by firing concurrent requests at it:
its `llama-server` process runs with `-np 1` (`--parallel 1`), so every
request queues behind whichever one is already running, regardless of how
many concurrent callers there are. Locust load results against real Ollama
therefore measure Ollama's own serialization, not agent_worker.py's —
pointing at this server instead removes that confound.

`langchain_openai.ChatOpenAI` (app/agent/graph.py::_make_llm) needs zero
code changes to talk to this: just point `OPENAI_API_BASE` here instead of
LiteLLM. Implements both streaming and non-streaming `POST
/v1/chat/completions`, since app/agent/graph.py's `_make_llm()` comment
notes `astream_events` (what the queued path always runs through) forces
every LLM call through the streaming HTTP path even when the calling node
code does `.invoke()` — so a load-test run exercising `/chat/stream/queued`
always hits the streaming branch below; the non-streaming branch exists so
this server also works against the plain (non-queued) `POST /chat` path,
which does issue a genuine non-streaming request.

Run: `make fake-llm` (defaults to :9009). Point a load-test run at it
(bypassing LiteLLM/Ollama/Langfuse tracing entirely — this exists to
isolate agent_worker.py's own concurrency, not to exercise the full
production observability path):

    OPENAI_API_BASE=http://localhost:9009/v1 make serve
    OPENAI_API_BASE=http://localhost:9009/v1 make agent-worker
    make loadtest-queued   # loadtest/locustfile_queued.py, against the above

Two knobs simulate different backends, both via env var so no code change
is needed to compare them:
- `FAKE_LLM_LATENCY_SECONDS` / `FAKE_LLM_LATENCY_JITTER_SECONDS` — simulated
  think-time per completion (default 1.5s +/- up to 0.5s).
- `FAKE_LLM_MAX_CONCURRENCY` — 0 (default) means unbounded, i.e. a genuinely
  concurrent backend; set it to 1 to reproduce today's Ollama-shaped
  ceiling as an A/B baseline, or any N to simulate a rate-limited real
  provider.

Also implements `POST /v1/embeddings` — every real turn calls
app/retrieval/embeddings.py::embed_text unconditionally (retrieve_context's
own automatic pre-fetch, plus `remember`/`add_note`'s writes), and without
this route every one of those calls 404s against a server that only ever
implemented chat completions, silently degrading EVERY semantic-cache
lookup and EVERY Qdrant read/write to a permanent miss/no-op — never
exercising the real code on the other side of those calls at all. Same
deterministic, dependency-free technique
tests/agent/test_concurrent_turns.py's own `_fake_embed_text` already uses
in-process (same text -> same vector, needed for a cache/Qdrant "hit" to
work at all) — a real embedding model was never load-bearing for THIS
server's job of exercising concurrency, only a consistent one.
"""
import asyncio
import hashlib
import json
import os
import random
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Fake concurrent LLM (load-test double)")

LATENCY_SECONDS = float(os.environ.get("FAKE_LLM_LATENCY_SECONDS", "1.5"))
LATENCY_JITTER_SECONDS = float(os.environ.get("FAKE_LLM_LATENCY_JITTER_SECONDS", "0.5"))
_MAX_CONCURRENCY = int(os.environ.get("FAKE_LLM_MAX_CONCURRENCY", "0"))  # 0 = unbounded
_semaphore = asyncio.Semaphore(_MAX_CONCURRENCY) if _MAX_CONCURRENCY > 0 else None


# --- Tool-call simulation registry ------------------------------------------
#
# To make a NEW tool call get simulated, add a new `@tool_simulator`-decorated
# function below (anywhere above `# --- OpenAI wire format ---`) — nothing
# else in this file needs to change. Each simulator inspects the turn's last
# USER message text plus the tool names the request actually offered
# (`tools`), and returns the ToolCall to emit instead of a plain-text answer,
# or None to decline (the next registered simulator gets a turn; if none
# claim it, the turn gets a plain-text final answer). A turn that's already
# past its tool call — the follow-up request carrying the tool's real result
# as a `role: "tool"` message — never reaches these; see `_pick_tool_call`.
#
# The tool NAME returned must match a real tool this app's graph can execute
# (app/agent/tools.py) — ToolNode actually runs it for real against the
# arguments given here, exactly as production would; this file only fakes
# the LLM's decision to call it, never the tool execution itself.


@dataclass
class ToolCall:
    name: str
    arguments: dict


ToolSimulator = Callable[[str, set[str]], "ToolCall | None"]
_TOOL_SIMULATORS: list[ToolSimulator] = []


def tool_simulator(fn: ToolSimulator) -> ToolSimulator:
    _TOOL_SIMULATORS.append(fn)
    return fn


@tool_simulator
def _calculator(last_user_text: str, requested_tool_names: set[str]) -> ToolCall | None:
    """Matches loadtest/locustfile_queued.py's (and locustfile.py's)
    `calculator` task, e.g. "what is 12 * 7?" — app/agent/tools.py's real
    `calculator(expression: str)` tool then actually evaluates it, exactly
    as production would."""
    if "calculator" not in requested_tool_names:
        return None
    match = re.search(r"(\d+)\s*([*+/-])\s*(\d+)", last_user_text)
    if not match:
        return None
    a, op, b = match.groups()
    return ToolCall(name="calculator", arguments={"expression": f"{a}{op}{b}"})


@tool_simulator
def _remember(last_user_text: str, requested_tool_names: set[str]) -> ToolCall | None:
    """Matches a "please remember <fact>"-shaped message (see
    loadtest/locustfile_queued.py's own HITL task) — app/agent/tools.py's
    real `remember(content: str)` tool then actually writes it to Qdrant.
    `remember` is declared "mutating" (TOOL_CAPABILITIES), so this ALWAYS
    pauses at human_approval regardless of this server's own decision —
    that gate is enforced by the real graph, not simulated here; this
    file only fakes the LLM's decision to call the tool in the first
    place, both before AND after a real approval round trip (the calling
    conversation looks identical to this server either way)."""
    if "remember" not in requested_tool_names:
        return None
    match = re.search(r"remember (.+)", last_user_text, re.IGNORECASE)
    if not match:
        return None
    return ToolCall(name="remember", arguments={"content": match.group(1).strip()})


@tool_simulator
def _run_subagent(last_user_text: str, requested_tool_names: set[str]) -> ToolCall | None:
    """Matches a "please delegate <task>"-shaped message (see
    loadtest/locustfile_queued.py's own subagent task) —
    app/agent/tools.py's real `run_subagent` tool then actually runs a
    genuinely separate, nested graph turn — including its OWN nested LLM
    call back to THIS SAME server (its `ChatOpenAI` construction also
    reads `OPENAI_API_BASE`), which `_final_answer_text` below handles
    generically (no subagent-specific branch needed there): with no tool
    result yet and no further tool call matched, it just echoes the
    nested run's own delegated task back, the same as any other
    plain-answer turn would."""
    if "run_subagent" not in requested_tool_names:
        return None
    match = re.search(r"delegate (.+)", last_user_text, re.IGNORECASE)
    if not match:
        return None
    return ToolCall(
        name="run_subagent",
        arguments={"subagent_name": "researcher", "task": match.group(1).strip()},
    )


# add more @tool_simulator functions here to simulate more tools


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):  # OpenAI's multimodal content-parts shape
            return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _requested_tool_names(tools: list[dict] | None) -> set[str]:
    return {t["function"]["name"] for t in (tools or []) if t.get("type") == "function"}


def _pick_tool_call(messages: list[dict], tools: list[dict] | None) -> ToolCall | None:
    if not tools or (messages and messages[-1].get("role") == "tool"):
        return None  # no tools offered, or this IS the post-tool-result round trip
    last_user_text = _last_user_text(messages)
    requested = _requested_tool_names(tools)
    for simulate in _TOOL_SIMULATORS:
        call = simulate(last_user_text, requested)
        if call is not None:
            return call
    return None


def _final_answer_text(messages: list[dict]) -> str:
    """Echoes something DERIVED from this specific turn's own input rather
    than a fixed generic string, so a caller running many of these
    concurrently can verify a given turn's answer actually reflects ITS
    OWN input and never a concurrent sibling's — the same no-cross-wiring
    property tests/agent/test_concurrent_turns.py's `_echoing_llm` proves
    in-process, proven here instead over the real wire.

    The tool-result branch includes the ORIGINAL user text too, not just
    the tool's own return value — verified directly that this matters:
    `remember`'s real return value is the fixed string "Remembered."
    (app/agent/tools.py::_remember_impl), not an echo of what was
    remembered, so echoing ONLY the tool result would make every
    concurrent `remember` call's final answer identical and useless for
    telling them apart. `run_subagent`'s own tool result IS already
    distinguishing (the nested run's own answer, see `_run_subagent`'s
    docstring) — including the user text alongside it there too is
    redundant, not wrong."""
    last_user_text = _last_user_text(messages)
    if messages and messages[-1].get("role") == "tool":
        return f"The result is {messages[-1].get('content')}, regarding: {last_user_text}"
    if last_user_text:
        return f"Simulated answer regarding: {last_user_text}"
    return "This is a simulated response from the fake load-test LLM."


async def _think() -> None:
    """Simulated per-completion latency, gated by `_semaphore` if
    `FAKE_LLM_MAX_CONCURRENCY` bounds it — this is the ONE place concurrency
    is actually shaped, so both the streaming and non-streaming branches
    below share it rather than each reimplementing the gate."""
    delay = LATENCY_SECONDS + random.uniform(0, LATENCY_JITTER_SECONDS)
    if _semaphore is None:
        await asyncio.sleep(delay)
    else:
        async with _semaphore:
            await asyncio.sleep(delay)


# --- OpenAI wire format ------------------------------------------------------


def _completion_response(model: str, tool_call: ToolCall | None, messages: list[dict]) -> dict:
    if tool_call is not None:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {"name": tool_call.name, "arguments": json.dumps(tool_call.arguments)},
                }
            ],
        }
        finish_reason, completion_tokens = "tool_calls", 12
    else:
        text = _final_answer_text(messages)
        message = {"role": "assistant", "content": text}
        finish_reason, completion_tokens = "stop", len(text.split())

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": 40,
            "completion_tokens": completion_tokens,
            "total_tokens": 40 + completion_tokens,
        },
    }


async def _stream_completion(model: str, tool_call: ToolCall | None, messages: list[dict]):
    await _think()
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    def chunk(delta: dict, finish_reason: str | None = None, usage: dict | None = None) -> str:
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": (
                []
                if usage is not None
                else [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
            ),
        }
        if usage is not None:
            payload["usage"] = usage
        return f"data: {json.dumps(payload)}\n\n"

    if tool_call is not None:
        call_id = f"call_{uuid.uuid4().hex[:24]}"
        yield chunk({"role": "assistant", "content": None})
        yield chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": tool_call.name, "arguments": ""},
                    }
                ]
            }
        )
        yield chunk(
            {"tool_calls": [{"index": 0, "function": {"arguments": json.dumps(tool_call.arguments)}}]}
        )
        yield chunk({}, finish_reason="tool_calls")
        completion_tokens = 12
    else:
        text = _final_answer_text(messages)
        yield chunk({"role": "assistant", "content": ""})
        for word in text.split(" "):
            yield chunk({"content": word + " "})
        yield chunk({}, finish_reason="stop")
        completion_tokens = len(text.split())

    # A final usage-only chunk (empty `choices`) — matches what a real
    # OpenAI-compatible server sends when the request set
    # `stream_options: {"include_usage": true}`, which langchain_openai's
    # `stream_usage=True` (app/agent/graph.py::_make_llm) always requests.
    yield chunk(
        {},
        usage={
            "prompt_tokens": 40,
            "completion_tokens": completion_tokens,
            "total_tokens": 40 + completion_tokens,
        },
    )
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    tools = body.get("tools")
    model = body.get("model", "fake-llm")
    tool_call = _pick_tool_call(messages, tools)

    if body.get("stream"):
        return StreamingResponse(
            _stream_completion(model, tool_call, messages), media_type="text/event-stream"
        )
    await _think()
    return JSONResponse(_completion_response(model, tool_call, messages))


FAKE_EMBED_DIM = 32  # len(hashlib.sha256(...).digest()) — callers that
# create a Qdrant collection against this server (tests/integration/
# test_worker_scaling.py's own qdrant_store.ensure_collection(dim=...))
# must use this same value.


def _fake_embedding(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [b / 255.0 for b in digest]


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    raw_input = body.get("input", "")
    texts = raw_input if isinstance(raw_input, list) else [raw_input]
    model = body.get("model", "fake-embed")
    data = [
        {"object": "embedding", "index": i, "embedding": _fake_embedding(text)}
        for i, text in enumerate(texts)
    ]
    tokens = sum(len(t.split()) for t in texts)
    return JSONResponse(
        {
            "object": "list",
            "data": data,
            "model": model,
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        }
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "latency_seconds": LATENCY_SECONDS,
        "max_concurrency": _MAX_CONCURRENCY or "unbounded",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("FAKE_LLM_PORT", "9009")))

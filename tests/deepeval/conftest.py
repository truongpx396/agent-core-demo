"""Fixtures for tests/deepeval/ — LLM-judged quality checks
(`@pytest.mark.deepeval`, `make deepeval`; see GRAPH_PATTERNS.md pattern 48
for the full design writeup). Split out of `tests/live/conftest.py`
2026-09-16 alongside this package itself: these tests are manual-only
(never swept into `test-live`'s own `-m "llm or e2e"` selection) and need
their own TARGET/JUDGE model knobs, distinct from `tests/live/`'s
CI-speed `TEST_LLM_MODEL`.

`deepeval_ollama` — the TARGET model (`DEEPEVAL_MODEL`, the real graph's
own `CHAT_MODEL`, monkeypatched in by each test file's own
`real_ollama_chat_model` fixture). Stays local/offline, same reasoning as
promptfoo's `PROMPTFOO_MODEL`: a finding needs to be about the model this
app actually deploys, not a smaller CI-speed stand-in.

`deepeval_judge` — the GRADER, a SEPARATE knob from `deepeval_ollama`
(`DEEPEVAL_JUDGE_MODEL`) — same target/judge split as promptfoo's
`PROMPTFOO_MODEL`/`PROMPTFOO_REDTEAM_MODEL` (GRAPH_PATTERNS.md pattern 48),
fixed 2026-09-16 for the identical reason: strengthening the grader
shouldn't require also changing what's under test. Originally just a
bigger LOCAL model (qwen2.5:3b doing double duty as both target and judge)
— real, verified finding from that setup: FaithfulnessMetric scored a
hand-verified good answer 0.0 with a `reason` that contradicted its own
score outright. Moved to Groq's `openai/gpt-oss-120b` (a plain instruct
model, not `compound` — `compound` autonomously invokes web search/code
execution mid-request, up to 10 tool calls per call, a bad fit for a judge
that needs one predictable structured verdict, not an agentic loop) for
the same reason promptfoo's `redteam.provider` moved to Gemini: a stronger
judge is worth more than staying local for a MANUAL, occasional,
non-target role. Needs GROQ_API_KEY (.env.example). `llama-3.3-70b-versatile`
(the original choice here) doesn't exist on Groq's current API at all —
caught by actually hitting `GET /v1/models` rather than trusting the web
search results that suggested it. This account's real limits for the
actual model, read off its own `x-ratelimit-*` response headers across
several rapid real calls, not guessed: ~8,000 TPM (the token bucket
refills back to full within about a second), and 1,000 RPD — NOT RPM —
that refills continuously afterward (`reset-requests` grew +86.4s per call
across 3 back-to-back requests; 86.4s * 1000 = 24h exactly). Comfortably
above this suite's low call volume (a couple of test files, not a
redteam-scale sweep) either way, so no extra pacing/concurrency limiting
was added here the way `make promptfoo-redteam` needed for its much
higher volume.
"""
import os

import pytest

from tests.containers import ensure_crawl4ai, ensure_ollama

# Separate from tests/live/conftest.py's TEST_LLM_MODEL, deliberately: these
# `deepeval`-marked tests are manual-only, unlike that file's CI-speed 1.5b.
# DEEPEVAL_MODEL drives the TARGET only — see this module's own docstring.
DEEPEVAL_MODEL = os.environ.get("DEEPEVAL_MODEL", "qwen2.5:3b")
DEEPEVAL_JUDGE_MODEL = os.environ.get("DEEPEVAL_JUDGE_MODEL", "openai/gpt-oss-120b")


@pytest.fixture(scope="session")
def deepeval_ollama() -> dict[str, str]:
    return ensure_ollama(DEEPEVAL_MODEL)


@pytest.fixture(scope="session")
def deepeval_judge():
    """The GRADER, pointed at Groq — see DEEPEVAL_JUDGE_MODEL's own module
    comment for why this is separate from `deepeval_ollama` (the TARGET).
    `deepeval.models.LocalModel` is a plain OpenAI-SDK client under a
    generic name (confirmed by reading its own source, not assumed from
    the name) — any OpenAI-compatible `base_url` works, same shape this
    app's own `OllamaModel` usage and its production LiteLLM proxy already
    take, just pointed at Groq's real endpoint instead of a local one.
    Fails fast with a clear message if GROQ_API_KEY isn't set, rather than
    an opaque 401 mid-test.
    """
    from deepeval.models import LocalModel

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        pytest.fail(
            "GROQ_API_KEY is not set — required for the deepeval judge "
            "(DEEPEVAL_JUDGE_MODEL, see tests/deepeval/conftest.py). Get one "
            "at https://console.groq.com/keys and set it in .env."
        )
    return LocalModel(
        model=DEEPEVAL_JUDGE_MODEL,
        api_key=api_key,
        base_url="https://api.groq.com/openai/v1",
        temperature=0,
    )


@pytest.fixture(scope="session")
def crawl4ai_server() -> dict[str, str]:
    """`tests/containers.py::ensure_crawl4ai()` (2026-09-17) — used only by
    `test_tool_correctness_deepeval.py`'s own
    `test_fetch_external_reference_tool_call_is_correct_and_well_argued`,
    via that file's non-autouse `_use_crawl4ai_server` fixture. Own copy
    rather than reusing `tests/live/conftest.py`'s or
    `tests/integration/conftest.py`'s fixture of the same name — same
    "each package wraps the shared `ensure_*()` helper itself" split
    `deepeval_ollama` above already takes relative to
    `tests/live/conftest.py`'s `ollama_endpoint` (both call `ensure_ollama`,
    just with different models) — `ensure_crawl4ai()`'s own cross-worker
    `_acquire` cache means calling it from any number of packages' conftests
    within the same pytest run still only starts one real container."""
    return ensure_crawl4ai()

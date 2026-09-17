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
score outright. Moved 2026-09-16 to Groq's `openai/gpt-oss-120b`, then
2026-09-17 to `gemini-3.1-flash-lite` via deepeval's own native
`GeminiModel` — THE SAME model `promptfoo/redteam.yaml`'s
`redteam.provider` already uses (`google:gemini-3.1-flash-lite`), so this
is now one deliberate offline-by-default exception with a single shared
API key (`GOOGLE_API_KEY`, already a repo secret for
`.github/workflows/redteam.yml`) rather than two separate ones — no new
secret needed, unlike the Groq detour. Needs `pip install google-genai`
(requirements-dev.txt; `.github/workflows/ci.yml`'s own `deepeval` job
installs it too) — not pulled in by a plain `pip install deepeval` the
same way `OllamaModel`'s `ollama` package isn't either. Fails fast with a
clear message if `GOOGLE_API_KEY` isn't set, rather than an opaque 401
mid-test, same posture `redteam.yml`'s own explicit key-check step takes.
"""
import os

import pytest

from tests.containers import ensure_crawl4ai, ensure_ollama

# Separate from tests/live/conftest.py's TEST_LLM_MODEL, deliberately: these
# `deepeval`-marked tests are manual-only, unlike that file's CI-speed 1.5b.
# DEEPEVAL_MODEL drives the TARGET only — see this module's own docstring.
DEEPEVAL_MODEL = os.environ.get("DEEPEVAL_MODEL", "qwen2.5:3b")
DEEPEVAL_JUDGE_MODEL = os.environ.get("DEEPEVAL_JUDGE_MODEL", "gemini-3.1-flash-lite")


@pytest.fixture(scope="session")
def deepeval_ollama() -> dict[str, str]:
    return ensure_ollama(DEEPEVAL_MODEL)


@pytest.fixture(scope="session")
def deepeval_judge():
    """The GRADER, pointed at Gemini — see DEEPEVAL_JUDGE_MODEL's own module
    comment for why this is separate from `deepeval_ollama` (the TARGET),
    and for why Gemini specifically (the same model+key
    `promptfoo/redteam.yaml`'s `redteam.provider` already uses). deepeval's
    own native `GeminiModel` (`deepeval.models`), not the generic
    OpenAI-SDK `LocalModel` this fixture used for Groq before — Gemini
    isn't OpenAI-compatible, so the real Google GenAI SDK is required
    (`pip install google-genai`). Fails fast with a clear message if
    GOOGLE_API_KEY isn't set, rather than an opaque 401 mid-test.
    """
    from deepeval.models import GeminiModel

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        pytest.fail(
            "GOOGLE_API_KEY is not set — required for the deepeval judge "
            "(DEEPEVAL_JUDGE_MODEL, see tests/deepeval/conftest.py). Get one "
            "at https://aistudio.google.com/app/apikey and set it in .env."
        )
    return GeminiModel(
        model=DEEPEVAL_JUDGE_MODEL,
        api_key=api_key,
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

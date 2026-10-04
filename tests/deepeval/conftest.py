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
Used by `test_rag_quality_deepeval.py`/`test_tool_correctness_deepeval.py`
only — see `deepeval_conversation_judge` below for why
`test_conversation_simulator_deepeval.py` can't use it.

Both judge fixtures below answer through a FAILOVER CHAIN (tests/deepeval/
fallback.py; `_with_fallbacks` here): the primary, then other models on the
same provider (`DEFAULT_*_FALLBACKS`, each with its own free-tier quota), then
the optional free Plugsky backup (https://plugsky.com, OpenAI-compatible,
`PLUGSKY_API_KEY`). A hand-over happens ONLY once the primary's own retry
policy (deepeval.models.retry_policy — a few attempts with backoff) has given
up on a rate limit OR a transient overload, and only for those. Absent
PLUGSKY_API_KEY and with `none` fallbacks, a fixture behaves exactly as it
always did — additive, never a new hard requirement.

The first version of this (a single Plugsky backup, widened for 503s after PR
#44) could not engage on the very case it existed for: deepeval's tenacity
policy has `reraise=False`, so when its attempts run out the exception that
reaches here is `tenacity.RetryError` with the real `RateLimitError` inside,
which the old classifier did not look into. A real CI run (PR #101) hit Groq's
per-day token limit with `PLUGSKY_API_KEY` set and the backup never engaged.
fallback.py looks inside, and has the tests the old code never had.

`deepeval_conversation_judge` — a THIRD knob, Groq's `openai/gpt-oss-120b`
again (`DEEPEVAL_CONVERSATION_JUDGE_MODEL`), added 2026-09-18 after a real
run (CI run 35230147154, PR #41) hard-crashed both
`test_conversation_simulator_deepeval.py` tests against Gemini:
`google.genai.errors.ClientError: 400 INVALID_ARGUMENT ... Unknown name
"additional_properties" at 'generation_config.response_schema'`. Root
cause, confirmed by reading deepeval's own source rather than guessed from
the error text: `KnowledgeRetentionMetric` (both of that file's tests use
it) unconditionally requests structured output against
`deepeval.metrics.knowledge_retention.schema.Knowledge.data:
Optional[Dict[str, Union[str, List[str]]]]` — Pydantic renders any
open-ended `Dict[...]` field as `additionalProperties` in the generated
JSON Schema, and Gemini's `response_schema` (a restricted OpenAPI-3.0
subset) doesn't support that keyword at all, for any Gemini model — not a
version/config issue, a structural one. `test_rag_quality_deepeval.py`/
`test_tool_correctness_deepeval.py`'s metrics never hit a Dict-typed
schema, so they stay on `deepeval_judge` (Gemini) above unaffected. Fails
fast the same way `deepeval_judge` does if `GROQ_API_KEY` isn't set.
"""
import os

import pytest

from tests.containers import ensure_crawl4ai, ensure_ollama
from tests.deepeval.fallback import build_chain, make_judge, model_list

# Separate from tests/live/conftest.py's TEST_LLM_MODEL, deliberately: these
# `deepeval`-marked tests are manual-only, unlike that file's CI-speed 1.5b.
# DEEPEVAL_MODEL drives the TARGET only — see this module's own docstring.
DEEPEVAL_MODEL = os.environ.get("DEEPEVAL_MODEL", "qwen2.5:3b")
DEEPEVAL_JUDGE_MODEL = os.environ.get("DEEPEVAL_JUDGE_MODEL", "gemini-3.1-flash-lite")
# test_conversation_simulator_deepeval.py ONLY — see this module's own
# docstring for why Gemini can't grade KnowledgeRetentionMetric at all.
DEEPEVAL_CONVERSATION_JUDGE_MODEL = os.environ.get(
    "DEEPEVAL_CONVERSATION_JUDGE_MODEL", "openai/gpt-oss-120b"
)
# Optional free backup judge for both fixtures below — see this module's
# own docstring for why (a transient-error-only fallback, never a replacement).
DEEPEVAL_BACKUP_MODEL = os.environ.get("DEEPEVAL_BACKUP_MODEL", "plugsky-micro")
PLUGSKY_BASE_URL = "https://api.plugsky.com/v1"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# Other models on the SAME provider (same key), tried in order before the Plugsky backup when a judge is rate
# limited or overloaded: each model has its own free-tier quota (a Groq 429 names the model; a Gemini one says
# `PerProjectPerModel`), so one being spent says nothing about the next. Comma-separated; `none` turns them off;
# unset means these defaults, so CI needs no new secret or variable. See tests/deepeval/fallback.py.
#  - Gemini judge: gemini-3.5-flash-lite is stable and free-tier.
#  - Conversation judge: both need Groq's STRICT structured outputs (KnowledgeRetentionMetric requests a schema),
#    which per Groq's docs only gpt-oss-120b, gpt-oss-20b and qwen3.8-27b support; each has its own 200K tokens/day.
DEFAULT_JUDGE_FALLBACKS = ("gemini-3.5-flash-lite",)
DEFAULT_CONVERSATION_JUDGE_FALLBACKS = ("openai/gpt-oss-20b", "qwen/qwen3.8-27b")


@pytest.fixture(scope="session")
def deepeval_ollama() -> dict[str, str]:
    return ensure_ollama(DEEPEVAL_MODEL)


def _plugsky_backup():
    """The optional free backup judge (https://plugsky.com, 100%
    OpenAI-compatible, `plugsky-micro` = NVIDIA Nemotron 3 Super 120B on
    the free tier) — `None` if PLUGSKY_API_KEY isn't set, so callers can
    treat "no backup configured" and "primary never rate-limited" the
    same way (just use the primary). Uses deepeval's own `LocalModel`
    (a generic OpenAI-SDK client), the same class `deepeval_conversation_judge`
    already uses for Groq — Plugsky needs no dedicated model class, only a
    different `base_url`."""
    api_key = os.environ.get("PLUGSKY_API_KEY")
    if not api_key:
        return None  # checked BEFORE the import below, so "no backup" needs no deepeval (the fast `test` job has none)
    from deepeval.models import LocalModel

    return LocalModel(
        model=DEEPEVAL_BACKUP_MODEL,
        api_key=api_key,
        base_url=PLUGSKY_BASE_URL,
        temperature=0,
    )


def _with_fallbacks(primary, fallbacks):
    """`primary` answered through the failover chain (tests/deepeval/fallback.py): then `fallbacks` (other models
    on the same provider), then the optional Plugsky backup. Returns `primary` unchanged when there is nothing
    to fall back to, so a bare checkout behaves exactly as it always did.

    Engages only on a TRANSIENT failure (rate limit, overload, timeout), judged by looking inside deepeval's own
    tenacity `RetryError` wrapper: see fallback.py for the bug that made the previous version miss exactly that.
    """
    backup = _plugsky_backup()
    if not fallbacks and backup is None:
        return primary
    return make_judge(build_chain(primary, fallbacks, backup))


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
    GOOGLE_API_KEY isn't set, rather than an opaque 401 mid-test. Wrapped
    in `_with_optional_backup` — see this module's own docstring for the
    optional Plugsky fallback on a transient error.
    """
    from deepeval.models import GeminiModel

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        pytest.fail(
            "GOOGLE_API_KEY is not set — required for the deepeval judge "
            "(DEEPEVAL_JUDGE_MODEL, see tests/deepeval/conftest.py). Get one "
            "at https://aistudio.google.com/app/apikey and set it in .env."
        )
    primary = GeminiModel(model=DEEPEVAL_JUDGE_MODEL, api_key=api_key, temperature=0)
    names = model_list(os.environ.get("DEEPEVAL_JUDGE_FALLBACK_MODELS"), DEFAULT_JUDGE_FALLBACKS, exclude=[DEEPEVAL_JUDGE_MODEL])
    return _with_fallbacks(primary, [GeminiModel(model=name, api_key=api_key, temperature=0) for name in names])


@pytest.fixture(scope="session")
def deepeval_conversation_judge():
    """The GRADER (and simulated-persona model) for
    test_conversation_simulator_deepeval.py ONLY — see this module's own
    docstring, `deepeval_conversation_judge` paragraph, for the real 400
    INVALID_ARGUMENT finding that put this fixture back on Groq rather than
    `deepeval_judge`'s Gemini. Same `LocalModel` shape `deepeval_judge` used
    for Groq before it moved to Gemini (`deepeval.models.LocalModel` is a
    plain OpenAI-SDK client under a generic name — any OpenAI-compatible
    `base_url` works). Fails fast with a clear message if GROQ_API_KEY
    isn't set, rather than an opaque 401 mid-test. Wrapped in
    `_with_optional_backup` — see this module's own docstring for the
    optional Plugsky fallback on a transient error.
    """
    from deepeval.models import LocalModel

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        pytest.fail(
            "GROQ_API_KEY is not set — required for "
            "test_conversation_simulator_deepeval.py's judge "
            "(DEEPEVAL_CONVERSATION_JUDGE_MODEL, see "
            "tests/deepeval/conftest.py). Get one at "
            "https://console.groq.com/keys and set it in .env."
        )
    primary = LocalModel(model=DEEPEVAL_CONVERSATION_JUDGE_MODEL, api_key=api_key, base_url=GROQ_BASE_URL, temperature=0)
    names = model_list(
        os.environ.get("DEEPEVAL_CONVERSATION_JUDGE_FALLBACK_MODELS"),
        DEFAULT_CONVERSATION_JUDGE_FALLBACKS,
        exclude=[DEEPEVAL_CONVERSATION_JUDGE_MODEL],
    )
    fallbacks = [LocalModel(model=name, api_key=api_key, base_url=GROQ_BASE_URL, temperature=0) for name in names]
    return _with_fallbacks(primary, fallbacks)


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

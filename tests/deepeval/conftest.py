"""Fixtures for tests/deepeval/: LLM-judged quality checks (`@pytest.mark.deepeval`, `make deepeval`;
GRAPH_PATTERNS.md pattern 48). Split out of `tests/live/conftest.py` on 2026-09-16: these tests are
manual-only (never in `test-live`'s `-m "llm or e2e"` selection) and need their own TARGET/JUDGE model knobs,
distinct from `tests/live/`'s CI-speed `TEST_LLM_MODEL`.

`deepeval_ollama`: the TARGET model (`DEEPEVAL_MODEL`, the real graph's `CHAT_MODEL`, monkeypatched in by each
test file's `real_ollama_chat_model` fixture). It stays local/offline, as with promptfoo's `PROMPTFOO_MODEL`:
a finding must be about the model this app deploys, not a smaller CI-speed stand-in.

`deepeval_judge`: the GRADER, a SEPARATE knob (`DEEPEVAL_JUDGE_MODEL`), the same target/judge split as
promptfoo's `PROMPTFOO_MODEL`/`PROMPTFOO_REDTEAM_MODEL` (pattern 48): strengthening the grader shouldn't
require changing what is under test. It was a bigger LOCAL model first (qwen2.5:3b as both target and judge);
the verified finding was that FaithfulnessMetric scored a hand-verified good answer 0.0 with a `reason` that
contradicted its own score. It moved to Groq's `openai/gpt-oss-120b`, then (2026-09-17) to
`gemini-3.1-flash-lite` via deepeval's native `GeminiModel`: the same model `promptfoo/redteam.yaml`'s
`redteam.provider` uses (`google:gemini-3.1-flash-lite`), so there is one deliberate offline-by-default
exception with one shared key (`GOOGLE_API_KEY`, already a repo secret for `.github/workflows/redteam.yml`).
It needs `pip install google-genai` (requirements-dev.txt; ci.yml's `deepeval` job installs it too), which a
plain `pip install deepeval` doesn't pull in, as with `OllamaModel`'s `ollama` package. It fails fast with a
clear message if `GOOGLE_API_KEY` is unset, not an opaque 401 mid-test, as `redteam.yml`'s key check does.
Used by `test_rag_quality_deepeval.py`/`test_tool_correctness_deepeval.py`; see `deepeval_conversation_judge`
for why `test_conversation_simulator_deepeval.py` can't.

Both judge fixtures answer through a FAILOVER CHAIN (tests/deepeval/fallback.py; `_with_fallbacks` here): the
primary, then other models on the same provider (`DEFAULT_*_FALLBACKS`, each with its own free-tier quota),
then the optional free backup providers (tests/deepeval/backup_providers.py: Plugsky, then OpenRouter, each
enabled by its own key). A hand-over happens ONLY once the primary's own retry policy
(deepeval.models.retry_policy: a few attempts with backoff) has given up on a rate limit or transient
overload, and only for those. With no backup key set and `none` fallbacks, a fixture behaves as it always did:
additive, never a new hard requirement.

The first version (a single Plugsky backup, widened for 503s after PR #44) couldn't engage on the very case it
existed for: deepeval's tenacity policy has `reraise=False`, so when attempts run out the exception reaching
here is `tenacity.RetryError` with the real `RateLimitError` inside, which the old classifier didn't look
into. A CI run (PR #101) hit Groq's per-day token limit with `PLUGSKY_API_KEY` set and the backup never
engaged. fallback.py looks inside, and has the tests the old code lacked.

`deepeval_conversation_judge`: a THIRD knob, Groq's `openai/gpt-oss-120b`
(`DEEPEVAL_CONVERSATION_JUDGE_MODEL`), added 2026-09-18 after CI run 35230147154 (PR #41) hard-crashed both
`test_conversation_simulator_deepeval.py` tests against Gemini: `google.genai.errors.ClientError: 400
INVALID_ARGUMENT ... Unknown name "additional_properties" at 'generation_config.response_schema'`. Root cause,
from deepeval's source: `KnowledgeRetentionMetric` (both tests use it) unconditionally requests structured
output against `deepeval.metrics.knowledge_retention.schema.Knowledge.data: Optional[Dict[str, Union[str,
List[str]]]]`; Pydantic renders an open-ended `Dict[...]` as `additionalProperties`, and Gemini's
`response_schema` (a restricted OpenAPI-3.0 subset) doesn't support that keyword for any Gemini model, a
structural limit, not a version issue. The other two files' metrics never hit a Dict-typed schema, so they
stay on `deepeval_judge`. It fails fast the same way if `GROQ_API_KEY` is unset.
"""
import os

import pytest

from tests.containers import ensure_crawl4ai, ensure_ollama
from tests.deepeval.backup_providers import build_backup_models
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
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# Other models on the SAME provider (same key), tried in order before the backup providers when a judge is rate
# limited or overloaded: each model has its own free-tier quota (a Groq 429 names the model; a Gemini one says
# `PerProjectPerModel`), so one being spent says nothing about the next. Comma-separated; `none` turns them off;
# unset means these defaults, so CI needs no new secret or variable. See tests/deepeval/fallback.py.
#  - Gemini judge: gemini-3.5-flash-lite is stable and free-tier.
#  - Conversation judge: each has its own 200K tokens/day. These three were picked because per Groq's docs they are the
#    ones with strict structured outputs, but deepeval 4.2.0's `LocalModel` sends no `response_format` (checked in its
#    source), so nothing here is enforced: the model is only asked for JSON in the prompt. A fallback can therefore answer
#    in the wrong shape or not at all; the chain treats that as a reason to try the next model (fallback.py, UNUSABLE
#    ANSWERS). Not verified against live Groq: whether passing a `response_format` through `generation_kwargs` would help.
DEFAULT_JUDGE_FALLBACKS = ("gemini-3.5-flash-lite",)
DEFAULT_CONVERSATION_JUDGE_FALLBACKS = ("openai/gpt-oss-20b", "qwen/qwen3.8-27b")


@pytest.fixture(scope="session")
def deepeval_ollama() -> dict[str, str]:
    return ensure_ollama(DEEPEVAL_MODEL)


def _with_fallbacks(primary, fallbacks):
    """`primary` answered through the failover chain (tests/deepeval/fallback.py): then `fallbacks` (other models
    on the same provider), then the optional backup providers' models (tests/deepeval/backup_providers.py:
    Plugsky, then OpenRouter, each only if its key is set). Returns `primary` unchanged when there is nothing
    to fall back to, so a bare checkout behaves exactly as it always did.

    Engages only on a TRANSIENT failure (rate limit, overload, timeout), judged by looking inside deepeval's own
    tenacity `RetryError` wrapper: see fallback.py for the bug that made the previous version miss exactly that.
    """
    backups = build_backup_models()  # [] (and no deepeval import) when no backup key is set
    if not fallbacks and not backups:
        return primary
    return make_judge(build_chain(primary, fallbacks, backups))


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
    through `_with_fallbacks` — see this module's own docstring for the
    failover chain.
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
    isn't set, rather than an opaque 401 mid-test. Answers through
    `_with_fallbacks` — see this module's own docstring for the failover
    chain.
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

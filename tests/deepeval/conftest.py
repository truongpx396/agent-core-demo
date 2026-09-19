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

Both judge fixtures below wrap their primary in `_JudgeWithBackup`, an
optional free fallback (https://plugsky.com, OpenAI-compatible,
`PLUGSKY_API_KEY`) that engages ONLY once the primary's own retry policy
(deepeval.models.retry_policy — a few attempts with backoff) has already
given up on a rate limit OR a transient server overload (see
`_is_transient_provider_error`'s own docstring — widened 2026-09-19
after a real CI run, PR #44, hit exactly the 503 case a 429-only check
missed) — added proactively, not from an observed rate-limit incident,
since both GOOGLE_API_KEY's free tier (30 req/min) and GROQ_API_KEY's
(1,000 RPD) are real, finite ceilings a busy `make deepeval` run could
plausibly hit (see this module's own comments above for where those
numbers came from). Absent PLUGSKY_API_KEY, both fixtures behave exactly
as before — this is additive, never a new hard requirement.

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


@pytest.fixture(scope="session")
def deepeval_ollama() -> dict[str, str]:
    return ensure_ollama(DEEPEVAL_MODEL)


def _is_transient_provider_error(exc: Exception) -> bool:
    """True for the same known-transient signatures
    `.github/workflows/ci.yml`'s own `--only-rerun` regex already treats
    as worth retrying (rate limit OR temporary overload) from either
    provider the judge fixtures below use — Gemini's google-genai raises
    `APIError`/`ServerError` with `.code` set to the HTTP status (429
    RESOURCE_EXHAUSTED, or a 5xx "high demand" `ServerError` — deepeval's
    own retry_policy treats `ServerError` as transient/network-like, same
    reasoning here), Groq's/Plugsky's OpenAI-SDK client raises
    `RateLimitError`/`APITimeoutError`/`APIConnectionError`/
    `InternalServerError` directly. Deliberately still narrow: any OTHER
    failure (auth, bad request, a real bad-argument bug) must still
    surface immediately rather than get silently masked by a fallback
    that can't fix it. Verified against a real CI run (PR #44) that hit
    exactly the 503 case this widening now catches — `getattr(exc,
    'code', None) == 429` alone missed it."""
    import openai

    if isinstance(
        exc, (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError)
    ):
        return True
    return getattr(exc, "code", None) in (429, 500, 502, 503, 504)


def _plugsky_backup():
    """The optional free backup judge (https://plugsky.com, 100%
    OpenAI-compatible, `plugsky-micro` = NVIDIA Nemotron 3 Super 120B on
    the free tier) — `None` if PLUGSKY_API_KEY isn't set, so callers can
    treat "no backup configured" and "primary never rate-limited" the
    same way (just use the primary). Uses deepeval's own `LocalModel`
    (a generic OpenAI-SDK client), the same class `deepeval_conversation_judge`
    already uses for Groq — Plugsky needs no dedicated model class, only a
    different `base_url`."""
    from deepeval.models import LocalModel

    api_key = os.environ.get("PLUGSKY_API_KEY")
    if not api_key:
        return None
    return LocalModel(
        model=DEEPEVAL_BACKUP_MODEL,
        api_key=api_key,
        base_url=PLUGSKY_BASE_URL,
        temperature=0,
    )


def _with_optional_backup(primary):
    """Wraps `primary` in a fallback that only engages on a known-transient
    failure (`_is_transient_provider_error` above), or returns `primary`
    unchanged if PLUGSKY_API_KEY isn't set (`_plugsky_backup` returns
    `None`) — see this module's own docstring. `DeepEvalBaseLLM` is
    imported, and the wrapper class defined, INSIDE this function rather
    than at module level — this file's own docstring already establishes
    that importing `deepeval` at collection time has to stay optional
    (the fast `test` job's pytest run collects this whole file without
    `deepeval` installed at all), and a module-level `class
    X(DeepEvalBaseLLM)` would import it unconditionally just by being
    defined."""
    backup = _plugsky_backup()
    if backup is None:
        return primary

    from deepeval.models import DeepEvalBaseLLM

    class _JudgeWithBackup(DeepEvalBaseLLM):
        """MUST subclass DeepEvalBaseLLM, not just duck-type `generate`/
        `a_generate`/`get_model_name` — deepeval's own
        `metrics/utils.py::initialize_model` does `isinstance(model,
        DeepEvalBaseLLM)` before trusting a passed-in model object at
        all; a plain wrapper object fails that check and falls through
        to deepeval's env-based auto-detection instead of raising,
        silently grading with the wrong model rather than this
        fixture's chosen one.

        `generate`/`a_generate` return just the content (a `str` or, if
        `schema` was given, a validated schema instance) — NOT the
        `(content, cost)` tuple `self._primary`/`self._backup`
        (deepeval's own native `GeminiModel`/`LocalModel`) actually
        return. Real bug, caught live in CI (PR #44,
        test_conversation_simulator_deepeval.py): deepeval's own
        `metrics/utils.py::initialize_model` marks any CUSTOM
        `DeepEvalBaseLLM` subclass (this one included — it isn't one of
        deepeval's own native provider classes) as `using_native_model =
        False`, and callers like
        `deepeval.simulator.conversation_simulator.py::generate_schema`
        branch on that flag: the native-model branch unpacks a 2-tuple,
        but the non-native branch (this class's branch) takes the
        return value AS THE CONTENT DIRECTLY, matching
        `DeepEvalBaseLLM.generate`'s own documented contract ("Returns: A
        string.") — passing the raw tuple through crashed with
        `AttributeError: 'tuple' object has no attribute
        'simulated_input'` the first time this class's fallback actually
        engaged against a real key."""

        def __init__(self, primary, backup):
            self._primary = primary
            self._backup = backup
            super().__init__(primary.get_model_name())

        def load_model(self):
            return self._primary

        def get_model_name(self) -> str:
            return self._primary.get_model_name()

        @staticmethod
        def _content(result):
            return result[0] if isinstance(result, tuple) else result

        def generate(self, prompt: str, schema=None):
            try:
                result = self._primary.generate(prompt, schema=schema)
            except Exception as exc:
                if not _is_transient_provider_error(exc):
                    raise
                print(
                    f"[deepeval] {self._primary.get_model_name()} hit a transient "
                    f"error, falling back to {self._backup.get_model_name()}: {exc}"
                )
                result = self._backup.generate(prompt, schema=schema)
            return self._content(result)

        async def a_generate(self, prompt: str, schema=None):
            try:
                result = await self._primary.a_generate(prompt, schema=schema)
            except Exception as exc:
                if not _is_transient_provider_error(exc):
                    raise
                print(
                    f"[deepeval] {self._primary.get_model_name()} hit a transient "
                    f"error, falling back to {self._backup.get_model_name()}: {exc}"
                )
                result = await self._backup.a_generate(prompt, schema=schema)
            return self._content(result)

    return _JudgeWithBackup(primary, backup)


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
    primary = GeminiModel(
        model=DEEPEVAL_JUDGE_MODEL,
        api_key=api_key,
        temperature=0,
    )
    return _with_optional_backup(primary)


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
    primary = LocalModel(
        model=DEEPEVAL_CONVERSATION_JUDGE_MODEL,
        api_key=api_key,
        base_url="https://api.groq.com/openai/v1",
        temperature=0,
    )
    return _with_optional_backup(primary)


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

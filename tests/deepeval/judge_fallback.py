"""The deepeval judges' optional backup chain — which free providers back a
primary judge up, in what order, and the rule for handing off between them.
Split out of `tests/deepeval/conftest.py` because none of it is a fixture:
it is policy (what counts as transient, who is tried next) plus a small
provider registry, and keeping it in conftest made the hermetic tests
import underscore-private names out of a conftest just to pin that rule.
`conftest.py` now only builds each judge's PRIMARY model and passes it to
`with_optional_backup`.

Why a chain at all: both primaries' free tiers are real, finite ceilings a
busy `make deepeval` run could plausibly hit (GOOGLE_API_KEY's 30 req/min,
GROQ_API_KEY's 1,000 RPD — see `tests/deepeval/conftest.py` for where those
numbers came from). A backup engages ONLY once the primary's own retry
policy (deepeval.models.retry_policy — a few attempts with backoff) has
already given up on a rate limit OR a transient server overload (see
`is_transient_provider_error`'s own docstring — widened 2026-09-19 after a
real CI run, PR #44, hit exactly the 503 case a 429-only check missed).
Each link in the chain is tried only after the one before it failed with
that same transient signature; see `call_with_fallbacks` for the exact
rule. Absent every provider's API key the judge fixtures behave exactly as
before — this is additive, never a new hard requirement.

The providers, tried in this order (`BACKUP_PROVIDERS`):

1. Plugsky (https://plugsky.com, OpenAI-compatible, `PLUGSKY_API_KEY`):
   `plugsky-micro` (NVIDIA Nemotron 3 Super 120B) then `plugsky-lite`, the
   two models its free plan grants. Override with `DEEPEVAL_BACKUP_MODEL`
   (the knob's original name, kept so an existing `.env` still works).
2. OpenRouter (https://openrouter.ai, OpenAI-compatible,
   `OPENROUTER_API_KEY`): `nvidia/nemotron-3-super-120b-a12b:free` then
   `qwen/qwen3.8-27b:free`. Override with `DEEPEVAL_OPENROUTER_MODEL`. Last
   because its free quota is the thinnest (below).

What is NOT fixed, stated so a longer chain isn't mistaken for more
capacity:
- Plugsky documents its free plan's 30 req/min per PLAN, not per model, so
  `plugsky-lite` only rescues a limit that is actually per-model, or an
  outage specific to `plugsky-micro`'s upstream.
- OpenRouter's `:free` variants are limited ACCOUNT-WIDE (docs: "we govern
  capacity globally"): 20 req/min, and 50 requests/day — 1,000/day once the
  account has bought $10 of credits. A second OpenRouter model therefore
  adds no quota, only a different upstream; the link's real value is an
  independent provider whose quota is separate from Gemini's, Groq's and
  Plugsky's. 50/day is small enough that one `make deepeval` run can
  exhaust it, at which point the chain is exhausted and the last 429
  propagates.
- `:free` model ids churn (several carry an `expiration_date`). A retired id
  comes back as a 404, which is deliberately NOT transient, so it fails
  loudly rather than being skipped — re-pick it from
  https://openrouter.ai/models?variant=free (or `curl
  https://openrouter.ai/api/v1/models`, filtering ids ending in `:free`).
  The defaults were chosen 2026-10-05 for: a large model (small ones are
  unreliable graders — see GRAPH_PATTERNS.md pattern 48), no
  `expiration_date`, ~99.9% live endpoint uptime, and different upstreams
  from each other and from Gemini. `openrouter/free` (a router that picks a
  random free model per request) was left out on purpose: a grader that
  changes between calls makes scores irreproducible. Neither default was
  exercised against a real OpenRouter key when this landed.

Nothing here imports deepeval, openai or a provider SDK at module level —
the fast `test` job's pytest run collects `tests/deepeval/` without
`deepeval` installed at all, so each such import lives inside the function
that needs it.
"""
import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class BackupProvider:
    """One OpenAI-compatible free backup provider. Needs no dedicated model
    class — deepeval's `LocalModel` is a generic OpenAI-SDK client, the same
    one `deepeval_conversation_judge` already uses for Groq, so a provider
    is only a different `base_url` + key + model list."""

    name: str
    api_key_env: str
    base_url: str
    models_env: str
    default_models: tuple[str, ...]


PLUGSKY = BackupProvider(
    name="plugsky",
    api_key_env="PLUGSKY_API_KEY",
    base_url="https://api.plugsky.com/v1",
    models_env="DEEPEVAL_BACKUP_MODEL",
    default_models=("plugsky-micro", "plugsky-lite"),
)
OPENROUTER = BackupProvider(
    name="openrouter",
    api_key_env="OPENROUTER_API_KEY",
    base_url="https://openrouter.ai/api/v1",
    models_env="DEEPEVAL_OPENROUTER_MODEL",
    default_models=("nvidia/nemotron-3-super-120b-a12b:free", "qwen/qwen3.8-27b:free"),
)
# Try-order, not alphabetical: the provider with the thinnest quota is last.
BACKUP_PROVIDERS = (PLUGSKY, OPENROUTER)


def parse_model_list(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    """A provider's models env var: comma-separated, in try-order; a single
    name still works, which is all `DEEPEVAL_BACKUP_MODEL` was before it
    became a list. Blank or whitespace-only (an empty `DEEPEVAL_..._MODEL=`
    line in `.env`) falls back to `default` instead of silently disabling
    that provider's backup."""
    names = tuple(name.strip() for name in (raw or "").split(",") if name.strip())
    return names or default


def backup_chain_spec(environ: Mapping[str, str] | None = None) -> list[tuple[BackupProvider, str]]:
    """The enabled `(provider, model)` links, in try-order: every provider
    whose API key is set, each expanded to its model list. `[]` when no key
    is set, so callers can treat "no backup configured" and "primary never
    rate-limited" the same way (just use the primary). Pure (reads only
    `environ`, builds no client) so the default hermetic suite can pin the
    ordering and the env overrides without deepeval or a network."""
    env = os.environ if environ is None else environ
    return [
        (provider, model)
        for provider in BACKUP_PROVIDERS
        if env.get(provider.api_key_env)
        for model in parse_model_list(env.get(provider.models_env), provider.default_models)
    ]


def is_transient_provider_error(exc: Exception) -> bool:
    """True for the same known-transient signatures
    `.github/workflows/ci.yml`'s own `--only-rerun` regex already treats
    as worth retrying (rate limit OR temporary overload) from any
    provider the judge fixtures use — Gemini's google-genai raises
    `APIError`/`ServerError` with `.code` set to the HTTP status (429
    RESOURCE_EXHAUSTED, or a 5xx "high demand" `ServerError` — deepeval's
    own retry_policy treats `ServerError` as transient/network-like, same
    reasoning here), Groq's/Plugsky's/OpenRouter's OpenAI-SDK client raises
    `RateLimitError`/`APITimeoutError`/`APIConnectionError`/
    `InternalServerError` directly. Deliberately still narrow: any OTHER
    failure (auth, bad request, a retired model id's 404, a real
    bad-argument bug) must still surface immediately rather than get
    silently masked by a fallback that can't fix it. Verified against a
    real CI run (PR #44) that hit exactly the 503 case this widening now
    catches — `getattr(exc, 'code', None) == 429` alone missed it."""
    import openai

    if isinstance(
        exc, (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError)
    ):
        return True
    return getattr(exc, "code", None) in (429, 500, 502, 503, 504)


def call_with_fallbacks(models, call):
    """Runs `call(model)` against each of `models` in order and returns the
    first result, moving to the next model ONLY when the current one fails
    with a known-transient error (`is_transient_provider_error`). Any other
    error is re-raised at once, from whichever link raised it — a bad
    model name (404/400) or a revoked key must stay loud, not be papered
    over by a link further down the chain that can't fix it. The LAST
    model's error is never caught: once the chain is exhausted the final
    transient error propagates unchanged, which is what
    `.github/workflows/ci.yml`'s `--only-rerun` regex needs to see to rerun
    the whole test. Pure on purpose (no deepeval import, `models` only need
    `get_model_name()`) so the default hermetic suite can pin this rule
    without deepeval installed — `with_optional_backup`'s wrapper below is
    a thin shell around it. Needs a non-empty `models`."""
    for position, model in enumerate(models[:-1]):
        try:
            return call(model)
        except Exception as exc:  # noqa: BLE001 - classified right below; anything non-transient is re-raised untouched
            if not is_transient_provider_error(exc):
                raise
            print(
                f"[deepeval] {model.get_model_name()} hit a transient "
                f"error, falling back to {models[position + 1].get_model_name()}: {exc}"
            )
    return call(models[-1])


async def acall_with_fallbacks(models, call):
    """`call_with_fallbacks` for an `async` `call` — same rule, same
    reason it is a separate function rather than a shared one: the `try`
    has to `await` inside itself for the exception to be caught here, and
    a sync loop returning an un-awaited coroutine would never raise."""
    for position, model in enumerate(models[:-1]):
        try:
            return await call(model)
        except Exception as exc:  # noqa: BLE001 - classified right below; anything non-transient is re-raised untouched
            if not is_transient_provider_error(exc):
                raise
            print(
                f"[deepeval] {model.get_model_name()} hit a transient "
                f"error, falling back to {models[position + 1].get_model_name()}: {exc}"
            )
    return await call(models[-1])


def build_backup_models(environ: Mapping[str, str] | None = None):
    """A deepeval `LocalModel` per link of `backup_chain_spec`, in order —
    `[]` if no provider's key is set. `temperature=0`, same as every judge
    here: a grader should not vary between identical calls."""
    from deepeval.models import LocalModel

    env = os.environ if environ is None else environ
    return [
        LocalModel(
            model=model,
            api_key=env[provider.api_key_env],
            base_url=provider.base_url,
            temperature=0,
        )
        for provider, model in backup_chain_spec(env)
    ]


def with_optional_backup(primary):
    """Wraps `primary` in a fallback chain that only engages on a
    known-transient failure (`is_transient_provider_error` above), or
    returns `primary` unchanged if no provider's key is set
    (`build_backup_models` returns `[]`) — see this module's own
    docstring. `DeepEvalBaseLLM` is imported, and the wrapper class
    defined, INSIDE this function rather than at module level — this
    package's fast `test` job collects it without `deepeval` installed at
    all, and a module-level `class X(DeepEvalBaseLLM)` would import it
    unconditionally just by being defined."""
    backups = build_backup_models()
    if not backups:
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
        `(content, cost)` tuple `self._primary`/each of `self._models`
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

        def __init__(self, primary, backups):
            self._primary = primary
            # The full try-order `call_with_fallbacks` walks: the primary
            # first, then each backup in `backup_chain_spec` order.
            self._models = [primary, *backups]
            super().__init__(primary.get_model_name())

        def load_model(self):
            return self._primary

        def get_model_name(self) -> str:
            return self._primary.get_model_name()

        @staticmethod
        def _content(result):
            return result[0] if isinstance(result, tuple) else result

        def generate(self, prompt: str, schema=None):
            return self._content(call_with_fallbacks(self._models, lambda model: model.generate(prompt, schema=schema)))

        async def a_generate(self, prompt: str, schema=None):
            return self._content(
                await acall_with_fallbacks(self._models, lambda model: model.a_generate(prompt, schema=schema))
            )

    return _JudgeWithBackup(primary, backups)

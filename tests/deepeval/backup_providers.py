"""Which free providers sit at the END of a deepeval judge's failover chain, and in what order.

The chain itself (what counts as transient, the hand-off, the cooldown, the log line) is
`tests/deepeval/fallback.py`; this module is only the registry of backup providers that are appended to
it after the primary and its same-provider fallbacks. Keeping the two apart means there is one rule for
"is this error worth handing off on", not one per provider.

Why backups at all: both primaries' free tiers are real, finite ceilings a busy `make deepeval` run can
hit (GOOGLE_API_KEY's 30 req/min, GROQ_API_KEY's 1,000 RPD; see tests/deepeval/conftest.py). A backup
engages only once the primary, and the other models on its own provider, have been rate limited or
overloaded past their own retry policy. With no backup key set a judge behaves exactly as without them.

The providers, in try-order (`BACKUP_PROVIDERS`); each is enabled by its own key alone:

1. Plugsky (https://plugsky.com, OpenAI-compatible, `PLUGSKY_API_KEY`): `plugsky-micro` (NVIDIA Nemotron 3
   Super 120B), then `plugsky-lite`, the two models its free plan grants. `DEEPEVAL_BACKUP_MODEL` overrides
   the list (the knob's original name, kept so an existing `.env` still works).
2. OpenRouter (https://openrouter.ai, OpenAI-compatible, `OPENROUTER_API_KEY`):
   `nvidia/nemotron-3-super-120b-a12b:free`, then `qwen/qwen3.8-27b:free`. `DEEPEVAL_OPENROUTER_MODEL`
   overrides the list. Last, because its free quota is the thinnest (below).

Both lists are comma-separated; `none` switches one provider's models off even when its key is set.

What is NOT fixed, stated so a longer chain is not mistaken for more capacity:
- Plugsky documents its free plan's 30 req/min per PLAN, not per model, so `plugsky-lite` only rescues a
  limit that is actually per model, or an outage specific to `plugsky-micro`'s upstream.
- OpenRouter limits `:free` models ACCOUNT-WIDE ("we govern capacity globally"): 20 req/min, and 50
  requests/day, or 1,000/day once the account has bought $10 of credits. A second OpenRouter model adds a
  different upstream, not more quota; the link's real value is a provider whose quota is separate from
  Gemini's, Groq's and Plugsky's. 50/day is small enough for one `make deepeval` run to exhaust.
- `:free` ids churn (several carry an `expiration_date`). A retired id comes back as a 404, which is
  deliberately not transient, so it fails loudly instead of being skipped. Re-pick it from
  https://openrouter.ai/models?variant=free. The defaults were chosen on 2026-10-05 for being large (small
  models are unreliable graders), having no `expiration_date`, ~99.9% endpoint uptime, and different
  upstreams from each other and from Gemini. `openrouter/free` (a router that picks a random model per
  call) was left out on purpose: a grader that changes between calls makes scores irreproducible.
- Neither OpenRouter default has been exercised against a real key, and how those reasoning models behave
  with deepeval's JSON-in-text parsing is untested.

Imports no deepeval, openai or SDK at module level: the fast `test` job collects `tests/deepeval/` without
deepeval installed, and the registry is tested there.
"""
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tests.deepeval.fallback import model_list


@dataclass(frozen=True)
class BackupProvider:
    """One OpenAI-compatible free backup provider. It needs no dedicated model class: deepeval's `LocalModel`
    is a generic OpenAI-SDK client (the one the Groq judge already uses), so a provider is only a base URL,
    a key and a model list."""

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
# Try-order, not alphabetical: the provider with the thinnest quota goes last.
BACKUP_PROVIDERS = (PLUGSKY, OPENROUTER)


def backup_chain_spec(environ: Mapping[str, str] | None = None) -> list[tuple[BackupProvider, str]]:
    """The enabled `(provider, model)` links in try-order: every provider whose key is set (a blank key, as
    CI passes an unset secret, is not set), each expanded to its model list. `[]` when no key is set.
    Pure: it reads only `environ` and builds no client, so the fast suite can pin the order and the
    overrides without deepeval or a network."""
    env = os.environ if environ is None else environ
    return [
        (provider, model)
        for provider in BACKUP_PROVIDERS
        if (env.get(provider.api_key_env) or "").strip()
        for model in model_list(env.get(provider.models_env), provider.default_models)
    ]


def build_backup_models(environ: Mapping[str, str] | None = None) -> list[Any]:
    """A deepeval `LocalModel` per link of `backup_chain_spec`, in order; `[]` (and no deepeval import) when
    no provider's key is set. `temperature=0` like every judge here: a grader must not vary between
    identical calls."""
    spec = backup_chain_spec(environ)
    if not spec:
        return []
    from deepeval.models import LocalModel

    env = os.environ if environ is None else environ
    return [
        LocalModel(model=model, api_key=env[provider.api_key_env].strip(), base_url=provider.base_url, temperature=0)
        for provider, model in spec
    ]

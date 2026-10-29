"""Which free providers sit at the END of a deepeval judge's failover chain, and in what order.

The chain itself (what is transient, the hand-off, the cooldown, the log line) is
`tests/deepeval/fallback.py`; this module is only the registry of backups appended after the primary and its
same-provider fallbacks, so there is one rule for "is this error worth handing off on", not one per provider.

Why backups: the primaries' free tiers are finite (GOOGLE_API_KEY 30 req/min, GROQ_API_KEY 1,000 RPD; see
tests/deepeval/conftest.py). A backup engages only once the primary and its provider's other models were rate
limited or overloaded past their own retry policy. With no backup key set a judge behaves exactly as without
them.

In try-order (`BACKUP_PROVIDERS`), each enabled by its own key alone:
1. Plugsky (OpenAI-compatible, `PLUGSKY_API_KEY`): `plugsky-micro` (NVIDIA Nemotron 3 Super 120B), then
   `plugsky-lite`. `DEEPEVAL_BACKUP_MODEL` overrides the list (the original knob name, kept so an existing
   `.env` works).
2. OpenRouter (`OPENROUTER_API_KEY`): `nvidia/nemotron-3-super-120b-a12b:free`, then `qwen/qwen3.8-27b:free`.
   `DEEPEVAL_OPENROUTER_MODEL` overrides. Last, because its free quota is thinnest.

Lists are comma-separated; `none` switches one provider's models off even with its key set.

A longer chain is not more capacity:
- Plugsky's free plan is 30 req/min per PLAN, so `plugsky-lite` only rescues a per-model limit or an outage
  specific to `plugsky-micro`'s upstream.
- OpenRouter limits `:free` models ACCOUNT-WIDE: 20 req/min and 50/day (1,000/day after $10 of credits). A
  second model adds an upstream, not quota; the value is a provider whose quota is separate from the others.
  50/day can be exhausted by one `make deepeval` run.
- `:free` ids churn (some carry an `expiration_date`). A retired id returns 404, deliberately not transient,
  so it fails loudly; re-pick from openrouter.ai/models?variant=free. The defaults (chosen 2026-10-05) are
  large (small models grade unreliably), have no `expiration_date`, ~99.9% uptime and distinct upstreams.
  `openrouter/free` (a random model per call) was left out: a changing grader makes scores irreproducible.
- Neither OpenRouter default has been run against a real key, and how those reasoning models handle deepeval's
  JSON-in-text parsing is untested.

No deepeval, openai or SDK import at module level: the fast `test` job collects tests/deepeval/ without
deepeval installed, and this registry is tested there.
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
    """The enabled `(provider, model)` links in try-order: every provider whose key is set (a blank key, as CI
    passes an unset secret, is not set), each expanded to its model list.
    """
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

"""Which providers the AI reviewer may try, in order. Pure functions, stdlib only.

The primary is `AI_REVIEW_BASE_URL` / `AI_REVIEW_MODEL` / `AI_REVIEW_API_KEY`. Up to `MAX_FALLBACKS`
fallbacks are numbered slots, tried in order when the one before has failed:

    AI_REVIEW_FALLBACK1_MODEL      required: a slot with no model is off
    AI_REVIEW_FALLBACK1_BASE_URL   optional: defaults to the primary's, i.e. "another model, same provider"
    AI_REVIEW_FALLBACK1_API_KEY    optional: see the key rule below
    AI_REVIEW_FALLBACK2_*          the same, for a third provider

Two-line reason this exists: a free tier's quota is per MODEL (and per provider), so when one is spent
the next may be untouched, and an advisory reviewer that goes dark for the day is worse than one that
quietly uses its second choice.

THE KEY RULE. One provider's API key is never sent to another provider's host. A fallback uses its own
key if it has one; otherwise it inherits the primary's key ONLY when it points at the same base URL
(the "another model, same provider" case). A fallback on a different host with no key of its own sends
none, which is right for a keyless endpoint and fails loudly with a 401 for one that needs a key.
"""
from collections.abc import Mapping
from dataclasses import dataclass

MAX_FALLBACKS = 2


@dataclass(frozen=True)
class Provider:
    name: str  # "primary", "fallback 1", ...: for logs and the comment header
    base_url: str
    model: str
    api_key: str = ""


def _clean_url(value: str) -> str:
    return value.strip().rstrip("/")


def build_fallbacks(env: Mapping[str, str], primary: Provider) -> tuple[Provider, ...]:
    """The configured fallbacks, in order. A slot without a model is skipped; so is one that repeats a
    provider already in the chain (same base URL and model), since trying it again cannot help.

    Raises ValueError for a slot whose base URL is not http(s): the same check the primary gets (urllib
    would otherwise also open file:// and ftp:// URLs).
    """
    chain = [primary]
    for slot in range(1, MAX_FALLBACKS + 1):
        prefix = f"AI_REVIEW_FALLBACK{slot}_"
        model = (env.get(prefix + "MODEL") or "").strip()
        if not model:
            continue
        base_url = _clean_url(env.get(prefix + "BASE_URL") or "") or primary.base_url
        if not base_url.startswith(("https://", "http://")):
            raise ValueError(f"{prefix}BASE_URL must be an http(s) URL")
        key = (env.get(prefix + "API_KEY") or "").strip()
        if not key and base_url == primary.base_url:
            key = primary.api_key  # same host, so the same credential is the right one; see THE KEY RULE
        candidate = Provider(f"fallback {slot}", base_url, model, key)
        if any((p.base_url, p.model) == (candidate.base_url, candidate.model) for p in chain):
            continue
        chain.append(candidate)
    return tuple(chain[1:])

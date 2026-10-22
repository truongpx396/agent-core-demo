"""What the app tells the LLM gateway about each call, and how it reads the gateway saying stop.

The gateway (LiteLLM) sits between the app and the model provider, so it is the one place that
can bound spend even if everything in the app is wrong: a bug in the allowance, a loop. Two things
make that work, both here. What it cannot cover is a model it has no price for: it prices from the
same list the app reads (`/model/info`), so an unpriced call is $0 to both and no budget of
LiteLLM's accumulates it. `UNPRICED_MODEL_POLICY=block` (app/agent/cost/pricing.py) is what covers
that, and COST_GOVERNANCE.md pattern 74 says why the shipped local models now state a price.

## Identity on every call

Without it the gateway's spend logs and Langfuse show one anonymous caller, so a runaway bill can
be found but not attributed. Each call carries:
  * `user` — LiteLLM's end-user id, recorded on every spend-log row as `end_user`. An opaque,
    stable hash of the tenant, NOT the tenant's name: whether LiteLLM passes `user` on to the model
    provider depends on the provider and version (checked against main-stable: an OpenAI-compatible
    backend received only `model` and `messages`), and an organisation's name has no business
    leaving for a third party if it does (OpenAI itself recommends a hashed id here). It is also
    what LiteLLM's per-end-user budgets are keyed by, so `max_end_user_budget_id` in the LiteLLM
    configs gives every tenant a gateway-side cap (pattern 74): a person-level id here would turn
    that cap into a per-person one and break `usage_reconcile.attribute_gateway`, which finds a
    tenant by hashing the names it knows.
  * `metadata` (inside `extra_body`) — the readable tenant, principal and a `tenant:<name>` tag,
    which LiteLLM records as a `request_tags` entry. It is for the gateway's own database and
    Langfuse, where an operator needs it; it was not forwarded to the backend either.
  * the conversation (`thread_id`), as `litellm_session_id` (LiteLLM's own "Session ID" in its
    logs) and `metadata.session_id` (the session the proxy's Langfuse trace is filed under). The
    app's own Langfuse trace uses the same `thread_id` as its session, so the app's turn and the
    proxy's per-call traces land in one Langfuse session; before, the proxy's traces had a session
    of their own and nothing joined them. Neither field is a model parameter, so LiteLLM keeps
    them out of the provider request (checked against a real proxy, pattern 74).

## Reading "stop"

When the app's key reaches its `max_budget`, LiteLLM refuses with an error whose `type` is
`budget_exceeded` (litellm/proxy/auth/auth_exception_handler.py). The HTTP status is the exception's
own and has changed between versions — 429 on main-stable, 400 on older code — so the `type` is what
is recognised, never the status. Left alone that is an `openai` status error out of the agent node,
which the stream reports as a generic internal error — and nobody learns that the backstop fired. It
means the app-level ceilings did not stop the spend first (or the backstop is sized too low), so it
gets its own error code, its own counter and a critical alert.

It is also never retried (`app/agent/graph/core.py::_retry_agent_on`): it repeats until the budget resets. The
openai SDK still retries a 429 twice inside one call (measured: 3 requests per `ainvoke`), which is
harmless — the gateway refuses at authentication, before any provider is called or any money spent.

## Reading "refused"

A content guardrail configured in the gateway (LiteLLM's admin UI keeps them in its own database, not
in this repo) answers a blocked call with HTTP 400 whose error body carries `provider_specific_fields`
(`guardrail_name`, `guardrail_mode`, `category`, `matched_phrase`). That is a policy decision, not a
failure of the app: the agent node turns it into the turn's answer (`content_block`), and the retry
policy never repeats it or any other 4xx that repeating cannot change (`is_permanent_client_error`).
Its text names the rule and the phrase that matched, so it never reaches the caller.
"""
import hashlib
from dataclasses import dataclass
from typing import Any

import openai
from langchain_core.runnables import RunnableConfig

from app.core import metrics
from app.core.errors import ErrorCode, ErrorEnvelope
from app.core.security import SecurityCtx, valid_ctx


def end_user_id(tenant: str) -> str:
    """The id the gateway and the model provider see for `tenant`: stable, so a tenant's calls
    group together, and opaque, so its name never leaves. Operators map it back with
    `python -m scripts.litellm_key end-user --tenant <name>`; 16 hex characters is 64 bits, ample
    to keep distinct tenants distinct."""
    return "tenant_" + hashlib.sha256(tenant.encode()).hexdigest()[:16]


def call_identity(ctx: SecurityCtx | None, session_id: str | None = None) -> dict:
    """Keyword arguments to pass to `llm.ainvoke(...)` so the gateway can attribute the call.
    Empty for an invalid ctx: an unattributable call is sent as it always was. `session_id` (the
    conversation's `thread_id`) is added when there is one. The cron scripts pass a synthetic
    thread id of their own (`ops-digest:<date>`, `followup-sweep:<item id>`), so each run is one
    session at the gateway; a caller with no thread at all simply sends none."""
    if not valid_ctx(ctx):
        return {}
    metadata: dict[str, Any] = {
        "tenant": ctx["tenant"],
        "principal": ctx["principal"],
        "tags": [f"tenant:{ctx['tenant']}"],
    }
    extra_body: dict[str, Any] = {"metadata": metadata}
    if session_id:
        metadata["session_id"] = session_id  # the Langfuse session of the proxy's own trace
        extra_body["litellm_session_id"] = session_id  # the "Session ID" in LiteLLM's logs
    return {"user": end_user_id(ctx["tenant"]), "extra_body": extra_body}


def identity_from_config(config: RunnableConfig | None) -> dict:
    """`call_identity` for the ctx and conversation a LangGraph node finds in its own `config`.
    `config` is optional because tests and scripts call a node with just a state; LangGraph passes
    it in a real run."""
    configurable = (config or {}).get("configurable") or {}
    thread_id = configurable.get("thread_id")
    return call_identity(configurable.get("ctx"), session_id=thread_id if isinstance(thread_id, str) else None)


def is_budget_exceeded(exc: BaseException) -> bool:
    """True if `exc` is the gateway refusing a call because a budget it enforces is spent. Every
    gateway budget (key, end user, team, tag) raises the same type, so this does not say WHICH ran
    out; LiteLLM's own message and spend log name it, and the operator reads those, not the caller.
    (The per-tenant budget of pattern 74 is an END-USER budget: read from the installed LiteLLM,
    `_check_end_user_budget` raises `BudgetExceededError`, which `auth_exception_handler` answers
    as this same type; a stop from a live gateway with a real database was not run.)"""
    return isinstance(exc, openai.APIStatusError) and getattr(exc, "type", None) == "budget_exceeded"


def note_budget_stop(exc: BaseException) -> bool:
    """If `exc` is the gateway's budget stop, count it and return True. The one place the counter
    is incremented, so a path that swallows the error (follow-ups, history compaction) still
    leaves the trace GatewayBudgetExceeded alerts on, instead of a log line nobody reads."""
    if not is_budget_exceeded(exc):
        return False
    metrics.agent_gateway_budget_exceeded_total.inc()
    return True


def budget_envelope() -> ErrorEnvelope:
    """The caller-facing error for a gateway budget stop. It names no figure and no key: how much
    the operator allows the app to spend is not the caller's to know."""
    return ErrorEnvelope(
        code=ErrorCode.PROVIDER_BUDGET_EXCEEDED,
        message=(
            "This service has reached its spending limit with its AI provider and cannot take new "
            "requests until the limit is raised or resets. This is on our side, not yours."
        ),
    )


# Client errors that can pass on a second try: a request timeout, a conflict with a concurrent change,
# "too early" and a rate limit. Every other 4xx says the request itself is refused, and the same
# request is refused again.
_TRANSIENT_CLIENT_STATUSES = frozenset({408, 409, 425, 429})


def is_permanent_client_error(exc: BaseException) -> bool:
    """True if `exc` is the gateway or the provider rejecting the request itself (a 4xx other than a
    timeout, conflict, "too early" or rate limit), so sending the identical request again cannot
    succeed. The status is read from the SDK's own exception: LangGraph's default retry predicate
    only knows `httpx`'s."""
    if not isinstance(exc, openai.APIStatusError):
        return False
    return 400 <= exc.status_code < 500 and exc.status_code not in _TRANSIENT_CLIENT_STATUSES


# The values LiteLLM puts in `guardrail_mode`; anything else is counted as "other" so the metric's label
# set stays bounded whatever a gateway version or a custom guardrail sends.
_GUARDRAIL_MODES = frozenset({"pre_call", "post_call", "during_call"})


@dataclass(frozen=True)
class ContentBlock:
    """A content guardrail's refusal. Only `mode` is kept: the guardrail's name, its category and the
    phrase it matched are the operator's (they are on the gateway's own log), not the caller's."""

    mode: str  # pre_call | post_call | during_call | other


def content_block(exc: BaseException) -> ContentBlock | None:
    """The guardrail refusal in `exc`, or None if it is anything else (a budget stop, another 400, a
    5xx). Recognised by the structured fields LiteLLM sets (`provider_specific_fields.guardrail_name`),
    else by the "Content blocked" prefix of its message; never by status alone, since a 400 is also how
    the gateway reports a malformed request."""
    if not isinstance(exc, openai.BadRequestError):
        return None
    body = exc.body if isinstance(exc.body, dict) else {}
    fields = body.get("provider_specific_fields")
    fields = fields if isinstance(fields, dict) else {}
    if not (fields.get("guardrail_name") or str(body.get("message") or "").startswith("Content blocked")):
        return None
    mode = fields.get("guardrail_mode")
    return ContentBlock(mode=mode if mode in _GUARDRAIL_MODES else "other")


def note_content_block(exc: BaseException) -> ContentBlock | None:
    """If `exc` is a guardrail refusal, count it and return it. The one place the counter is
    incremented (the same shape as `note_budget_stop`)."""
    block = content_block(exc)
    if block is not None:
        metrics.agent_gateway_content_blocked_total.labels(mode=block.mode).inc()
    return block

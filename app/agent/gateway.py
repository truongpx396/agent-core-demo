"""What the app tells the LLM gateway about each call, and how it reads the gateway saying stop.

The gateway (LiteLLM) sits between the app and the model provider, so it is the one place that
can bound spend even if everything in the app is wrong: a bug in the allowance, an unpriced model
(pricing.py), a loop. Two things make that work, both here.

## Identity on every call

Without it the gateway's spend logs and Langfuse show one anonymous caller, so a runaway bill can
be found but not attributed. Each call carries:
  * `user` — LiteLLM's end-user id, recorded on every spend-log row as `end_user`. An opaque,
    stable hash of the tenant, NOT the tenant's name: whether LiteLLM passes `user` on to the model
    provider depends on the provider and version (checked against main-stable: an OpenAI-compatible
    backend received only `model` and `messages`), and an organisation's name has no business
    leaving for a third party if it does (OpenAI itself recommends a hashed id here).
  * `metadata` (inside `extra_body`) — the readable tenant, principal and a `tenant:<name>` tag,
    which LiteLLM records as a `request_tags` entry. It is for the gateway's own database and
    Langfuse, where an operator needs it; it was not forwarded to the backend either.

## Reading "stop"

When the app's key reaches its `max_budget`, LiteLLM refuses with an error whose `type` is
`budget_exceeded` (litellm/proxy/auth/auth_exception_handler.py). The HTTP status is the exception's
own and has changed between versions — 429 on main-stable, 400 on older code — so the `type` is what
is recognised, never the status. Left alone that is an `openai` status error out of the agent node,
which the stream reports as a generic internal error — and nobody learns that the backstop fired. It
means the app-level ceilings did not stop the spend first (or the backstop is sized too low), so it
gets its own error code, its own counter and a critical alert.

It is also never retried (`graph.py::_retry_agent_on`): it repeats until the budget resets. The
openai SDK still retries a 429 twice inside one call (measured: 3 requests per `ainvoke`), which is
harmless — the gateway refuses at authentication, before any provider is called or any money spent.
"""
import hashlib

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


def call_identity(ctx: SecurityCtx | None) -> dict:
    """Keyword arguments to pass to `llm.ainvoke(...)` so the gateway can attribute the call.
    Empty for an invalid ctx: an unattributable call is sent as it always was."""
    if not valid_ctx(ctx):
        return {}
    return {
        "user": end_user_id(ctx["tenant"]),
        "extra_body": {
            "metadata": {
                "tenant": ctx["tenant"],
                "principal": ctx["principal"],
                "tags": [f"tenant:{ctx['tenant']}"],
            }
        },
    }


def identity_from_config(config: RunnableConfig | None) -> dict:
    """`call_identity` for the ctx a LangGraph node finds in its own `config`. `config` is optional
    because tests and scripts call a node with just a state; LangGraph passes it in a real run."""
    ctx = ((config or {}).get("configurable") or {}).get("ctx")
    return call_identity(ctx)


def is_budget_exceeded(exc: BaseException) -> bool:
    """True if `exc` is the gateway refusing a call because a budget it enforces is spent. Every
    gateway budget (key, end user, team, tag) raises the same type, so this does not say WHICH ran
    out; LiteLLM's own message and spend log name it, and the operator reads those, not the caller."""
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

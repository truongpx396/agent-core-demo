"""What the app tells the LLM gateway about each call, and how it reads the gateway saying stop.

The gateway (LiteLLM) sits between the app and the model provider, so it is the one place that can bound spend
even if the app is wrong (a bug in the allowance, a loop). It can't cover a model it has no price for: it
prices from the same list the app reads (`/model/info`), so an unpriced call is $0 to both.
`UNPRICED_MODEL_POLICY=block` (app/agent/cost/pricing.py) covers that; COST_GOVERNANCE.md pattern 74 says why
the shipped local models state a price.

## Identity on every call

Without it the gateway's spend logs and Langfuse show one anonymous caller, so a runaway bill can't be
attributed. Each call carries:
- `user`: the end-user id on every spend-log row, the PERSON as `tenant:<tenant>/user:<principal>` (each part
  percent-encoded; `parse_end_user_id` reads it back). LiteLLM's per-end-user budgets key on it, so
  `max_end_user_budget_id` is a default cap per person (pattern 74). A tenant-wide gateway cap would need the
  tenant to be the end user.
- `metadata` (inside `extra_body`): the readable tenant, principal and a `tenant:<name>` tag (a `request_tags`
  entry), for the gateway's database and Langfuse.
- the conversation (`thread_id`) as `litellm_session_id` and `metadata.session_id`, so the app's turn and the
  proxy's per-call traces land in one Langfuse session.

None of these reaches the model provider: checked against the pinned 1.94.1 image with a stub backend, on the
Ollama route and an OpenAI-compatible one. That is an assumption about LiteLLM (a person's email has no
business reaching a third party), so the image is pinned by digest and a bump re-runs the check
(infra/README.md).

## Reading "stop"

When the app's key reaches its `max_budget`, LiteLLM refuses with an error whose `type` is `budget_exceeded`.
The HTTP status has changed between versions (429, 400), so the `type` is recognised, never the status. Left
alone it would be a generic internal error and nobody would learn the backstop fired; it means the app-level
ceilings didn't stop the spend first, so it has its own error code, counter and a critical alert.

It is never retried (`app/agent/graph/core.py::_retry_agent_on`). The openai SDK still retries a 429 twice
inside one call, harmlessly: the gateway refuses at authentication, before any money is spent.

## Reading "refused"

A content guardrail in the gateway answers a blocked call with HTTP 400 whose body carries
`provider_specific_fields` (`guardrail_name`, `guardrail_mode`, `category`, `matched_phrase`). That is a
policy decision, not an app failure: the agent node turns it into the turn's answer (`content_block`), and the
retry policy never repeats it or any 4xx that repeating can't change (`is_permanent_client_error`). The text
names the rule and the matched phrase, so it never reaches the caller.
"""
import hashlib
import re
from typing import Any
from urllib.parse import quote, unquote

from langchain_core.runnables import RunnableConfig

from app.core import metrics
from app.core.errors import ErrorCode, ErrorEnvelope
from app.core.security import SecurityCtx, valid_ctx
from app.ports.llm import ContentBlock, llm_failures


def end_user_id(tenant: str, principal: str) -> str:
    """The gateway's end user for one person: `tenant:<tenant>/user:<principal>`, each part
    percent-encoded with nothing left unescaped but letters, digits and `_.-~`. Tenants and
    principals are arbitrary client-supplied strings (an `x-principal-id` header, `telegram:123`,
    an email), so the encoding is what keeps the id unambiguous: a `:` or `/` inside a part becomes
    `%3A` / `%2F`, and the two parts always split at the one literal `/`. Readable on purpose, so
    LiteLLM's Customer Usage dropdown names people; `parse_end_user_id` is the inverse."""
    return f"tenant:{quote(tenant, safe='')}/user:{quote(principal, safe='')}"


_END_USER = re.compile(r"tenant:([A-Za-z0-9_.~%-]*)/user:([A-Za-z0-9_.~%-]*)")


def parse_end_user_id(end_user: str) -> tuple[str, str] | None:
    """`(tenant, principal)` for an id this app made with `end_user_id`, else None. Strict: only
    the canonical encoding parses, so another application's end user on a shared gateway that
    merely starts with `tenant:` is not mistaken for ours."""
    match = _END_USER.fullmatch(end_user)
    if match is None:
        return None
    tenant, principal = unquote(match[1]), unquote(match[2])
    return (tenant, principal) if end_user_id(tenant, principal) == end_user else None


def legacy_end_user_id(tenant: str) -> str:
    """The end user this app sent BEFORE it named the person: `tenant_` plus 16 hex characters of
    a hash of the tenant (a one-way id, so the tenant's name never left). Nothing sends it any
    more; it exists so `usage_reconcile` can still attribute the spend-log rows written under it,
    and so an operator can read one (`litellm_key end-user --tenant <name>`). Delete it, and the
    branch in `attribute_gateway`, once the gateway's spend logs no longer hold such rows."""
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
    return {"user": end_user_id(ctx["tenant"], ctx["principal"]), "extra_body": extra_body}


def identity_from_config(config: RunnableConfig | None) -> dict:
    """`call_identity` for the ctx and conversation a LangGraph node finds in its own `config`.
    `config` is optional because tests and scripts call a node with just a state; LangGraph passes
    it in a real run."""
    configurable = (config or {}).get("configurable") or {}
    thread_id = configurable.get("thread_id")
    return call_identity(configurable.get("ctx"), session_id=thread_id if isinstance(thread_id, str) else None)


def note_budget_stop(exc: BaseException) -> bool:
    """If `exc` is the gateway's budget stop, count it and return True. The one place the counter
    is incremented, so a path that swallows the error (follow-ups, history compaction) still
    leaves the trace GatewayBudgetExceeded alerts on, instead of a log line nobody reads."""
    if not llm_failures.get().is_budget_exceeded(exc):
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


def note_content_block(exc: BaseException) -> ContentBlock | None:
    """If `exc` is a guardrail refusal, count it and return it. The one place the counter is
    incremented (the same shape as `note_budget_stop`)."""
    block = llm_failures.get().content_block(exc)
    if block is not None:
        metrics.agent_gateway_content_blocked_total.labels(mode=block.mode).inc()
    return block

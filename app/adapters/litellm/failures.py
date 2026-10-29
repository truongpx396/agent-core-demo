"""The `LlmFailures` adapter (`app/ports/llm.py`): what the `openai` SDK's exceptions mean, for a model reached through the LiteLLM gateway.

Moved here from `app/core/gateway.py` so the kernel names no SDK class. The recognition is unchanged:

- a budget stop is an `APIStatusError` whose `type` is `budget_exceeded` (the HTTP status has changed between LiteLLM versions, 429 and 400, so
  the `type` is recognised, never the status). The per-person budget of pattern 74 is an END-USER budget: read from the installed LiteLLM,
  `_check_end_user_budget` raises `BudgetExceededError`, which `auth_exception_handler` answers as this same type; a stop from a live gateway
  with a real database was not run;
- a permanent client error is a 4xx other than 408, 409, 425 and 429. The status is read from the SDK's own exception: LangGraph's default
  retry predicate only knows `httpx`'s;
- a content-guardrail refusal is a `BadRequestError` carrying the structured fields LiteLLM sets (`provider_specific_fields.guardrail_name`),
  else the "Content blocked" prefix of its message; never recognised by status alone, since a 400 is also how the gateway reports a malformed
  request.
"""
import openai

from app.ports.llm import ContentBlock

# Client errors that can pass on a second try: a request timeout, a conflict with a concurrent change, "too early" and a rate limit. Every
# other 4xx says the request itself is refused, and the same request is refused again.
_TRANSIENT_CLIENT_STATUSES = frozenset({408, 409, 425, 429})

# The values LiteLLM puts in `guardrail_mode`; anything else is counted as "other" so the metric's label set stays bounded whatever a gateway
# version or a custom guardrail sends.
_GUARDRAIL_MODES = frozenset({"pre_call", "post_call", "during_call"})


class OpenAiFailures:
    def is_budget_exceeded(self, exc: BaseException) -> bool:
        return isinstance(exc, openai.APIStatusError) and getattr(exc, "type", None) == "budget_exceeded"

    def is_permanent_client_error(self, exc: BaseException) -> bool:
        if not isinstance(exc, openai.APIStatusError):
            return False
        return 400 <= exc.status_code < 500 and exc.status_code not in _TRANSIENT_CLIENT_STATUSES

    def is_unreachable(self, exc: BaseException) -> bool:
        # Checked in this order, so a budget stop that arrives as a 5xx is still not caught: it repeats until the budget resets and is the
        # operator's signal, which a silent fallback would hide. Not any 4xx either: the model was reached and refused.
        if self.is_budget_exceeded(exc):
            return False
        if isinstance(exc, openai.APIConnectionError):  # includes APITimeoutError
            return True
        return isinstance(exc, openai.APIStatusError) and exc.status_code >= 500

    def content_block(self, exc: BaseException) -> ContentBlock | None:
        if not isinstance(exc, openai.BadRequestError):
            return None
        body = exc.body if isinstance(exc.body, dict) else {}
        fields = body.get("provider_specific_fields")
        fields = fields if isinstance(fields, dict) else {}
        if not (fields.get("guardrail_name") or str(body.get("message") or "").startswith("Content blocked")):
            return None
        mode = fields.get("guardrail_mode")
        return ContentBlock(mode=mode if mode in _GUARDRAIL_MODES else "other")

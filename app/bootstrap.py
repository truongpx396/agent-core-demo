"""The composition root: the one module that names both a port and the adapter that implements it.

Every process entrypoint (the API, the workers, the channels, the MCP servers, each operator script) calls `wire()` before it does
anything else. `wire()` binds each port's `Slot` (`app/ports/slot.py`) to the production adapter; it opens no connection and reads no
network, so it is cheap and safe to call twice. `verify_wired()` is the startup check that every port has an adapter, so a forgotten
binding stops the process at boot instead of failing the first request that happens to need it.

Adding a port is three edits, and `tests/core/test_bootstrap.py` fails on the first one without the other two:
1. the Protocol and its `Slot` in `app/ports/<capability>.py`;
2. the adapter in `app/adapters/<technology>/`;
3. one entry in `adapters()` here.

A test that wants a different adapter for one port uses `Slot.overriding` (or binds a fake from `tests/fakes/`) and never edits this file.
"""
import psycopg

from app.adapters.litellm.chat_model import OpenAiChatModels
from app.adapters.litellm.failures import OpenAiFailures
from app.adapters.litellm.model_catalog import LiteLLMModelCatalog
from app.adapters.mlservice.classification import MlServiceClassifier
from app.adapters.mlservice.prompt_guard import MlServicePromptGuard
from app.adapters.postgres.budget_holds import PgBudgetHoldStore
from app.adapters.postgres.budget_policies import PgBudgetPolicyStore
from app.adapters.postgres.checkpointing import PgCheckpointing
from app.adapters.postgres.sessions import PgSessionStore
from app.adapters.postgres.tool_idempotency import PgToolCallLedger
from app.adapters.postgres.usage_events import PgUsageEventStore
from app.core.errors import register_store_error
from app.domains.registry import RegistryDomainCatalog
from app.ports import (
    checkpointing,
    classification,
    cost,
    domains,
    idempotency,
    llm,
    models,
    retrieval,
    screening,
    sessions,
)
from app.ports.slot import PortNotBound, Slot, unbound
from app.retrieval.qdrant_store import QdrantDocumentIndex
from app.retrieval.semantic_cache import RedisAnswerCache


def adapters() -> dict[Slot, object]:
    """The production adapter for every port, new instances each call. One table, so `wire()`, `verify_wired()` and the tests that check each
    adapter against its Protocol all read the same list."""
    return {
        cost.usage_events: PgUsageEventStore(),
        cost.budget_holds: PgBudgetHoldStore(),
        cost.budget_policies: PgBudgetPolicyStore(),
        models.model_catalog: LiteLLMModelCatalog(),
        sessions.session_store: PgSessionStore(),
        idempotency.tool_call_ledger: PgToolCallLedger(),
        checkpointing.checkpointing: PgCheckpointing(),
        domains.domain_catalog: RegistryDomainCatalog(),
        retrieval.document_index: QdrantDocumentIndex(),
        retrieval.answer_cache: RedisAnswerCache(),
        llm.llm_failures: OpenAiFailures(),
        llm.chat_models: OpenAiChatModels(),
        classification.route_classifier: MlServiceClassifier(),
        screening.injection_scorer: MlServicePromptGuard(),
    }


def wire() -> None:
    """Binds every port that has no adapter yet to its production adapter. Safe to call again: it never replaces one that is bound."""
    for slot, adapter in adapters().items():
        slot.provide(adapter)
    # The kernel asks whether an error came from a data store (its text must never reach a prompt) but names no driver.
    register_store_error(psycopg.Error)


def verify_wired() -> None:
    """Raises `PortNotBound`, naming them, if any port module that has been imported still has no adapter."""
    missing = unbound()
    if missing:
        raise PortNotBound("ports with no adapter: " + ", ".join(sorted(missing)) + " (add them to app/bootstrap.py::adapters)")

"""The composition root: the one module that names both a port and the adapter that implements it.

Every process entrypoint (the API, the workers, the channels, the MCP servers, each operator script) calls `wire()` before it does
anything else. `wire()` binds each port's `Slot` (`app/ports/slot.py`) to the production adapter; it opens no connection and reads no
network, so it is cheap and safe to call twice. `verify_wired()` is the startup check that every port has an adapter, so a forgotten
binding stops the process at boot instead of failing the first request that happens to need it.

Adding a port is three edits, and `tests/core/test_bootstrap.py` fails on the first one without the other two:
1. the Protocol and its `Slot` in `app/ports/outbound/<capability>.py` (a business module's own repository: `app/domains/<module>/ports.py`);
2. the adapter in `app/adapters/<technology>/`;
3. one entry in `adapters()` here.

A test that wants a different adapter for one port uses `Slot.overriding` (or binds a fake from `tests/fakes/`) and never edits this file.
"""
import psycopg

from app.adapters.outbound.crawl4ai.web_crawler import Crawl4aiPageRenderer
from app.adapters.outbound.embeddings.embeddings import HybridEmbedder
from app.adapters.outbound.litellm.chat_model import OpenAiChatModels
from app.adapters.outbound.litellm.failures import OpenAiFailures
from app.adapters.outbound.litellm.model_catalog import LiteLLMModelCatalog
from app.adapters.outbound.mcp.connection import close_all_connections
from app.adapters.outbound.mcp.sandbox_tools import OpenSandboxCatalog
from app.adapters.outbound.minio.object_store import MinioObjectStore
from app.adapters.outbound.mlservice.classification import MlServiceClassifier
from app.adapters.outbound.mlservice.prompt_guard import MlServicePromptGuard
from app.adapters.outbound.notify.team_channel import LocalAndSlackTeamChannel
from app.adapters.outbound.postgres.budget_holds import PgBudgetHoldStore
from app.adapters.outbound.postgres.budget_policies import PgBudgetPolicyStore
from app.adapters.outbound.postgres.checkpointing import PgCheckpointing
from app.adapters.outbound.postgres.sessions import PgSessionStore
from app.adapters.outbound.postgres.tool_idempotency import PgToolCallLedger
from app.adapters.outbound.postgres.usage_events import PgUsageEventStore
from app.adapters.outbound.qdrant.qdrant_store import (
    QdrantDocumentIndex,
    QdrantDocumentWriter,
)
from app.adapters.outbound.redis.ingest_queue import RedisIngestQueue
from app.adapters.outbound.redis.queue import RedisTurnQueue
from app.adapters.outbound.redis.semantic_cache import RedisAnswerCache
from app.adapters.outbound.web.page_fetch import HttpxPageFetcher
from app.adapters.outbound.web.serper import SerperWebSearch
from app.core.errors import register_store_error
from app.domains.ecorp import ports as ecorp_ports
from app.domains.ecorp import store as ecorp_store
from app.domains.ops import metrics_client as ops_metrics_client
from app.domains.ops import ports as ops_ports
from app.domains.ops import store as ops_store
from app.domains.registry import RegistryDomainCatalog
from app.domains.sales import ports as sales_ports
from app.domains.sales import store as sales_store
from app.domains.support import ports as support_ports
from app.domains.support import store as support_store
from app.ports.outbound import (
    checkpointing,
    classification,
    cost,
    domains,
    idempotency,
    jobs,
    llm,
    models,
    notify,
    retrieval,
    sandbox,
    screening,
    sessions,
    storage,
    web,
)
from app.ports.slot import PortNotBound, Slot, unbound


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
        retrieval.document_writer: QdrantDocumentWriter(),
        retrieval.embedder: HybridEmbedder(),
        retrieval.answer_cache: RedisAnswerCache(),
        llm.llm_failures: OpenAiFailures(),
        llm.chat_models: OpenAiChatModels(),
        classification.route_classifier: MlServiceClassifier(),
        screening.injection_scorer: MlServicePromptGuard(),
        web.web_search: SerperWebSearch(),
        web.page_renderer: Crawl4aiPageRenderer(),
        web.page_fetcher: HttpxPageFetcher(),
        notify.team_channel: LocalAndSlackTeamChannel(),
        storage.object_store: MinioObjectStore(),
        jobs.turn_queue: RedisTurnQueue(),
        jobs.ingest_queue: RedisIngestQueue(),
        sandbox.sandbox_catalog: OpenSandboxCatalog(),
        # A domain's `store.py` is its adapter: the MODULE is bound (its functions are looked up at call time, which is how the module's tests patch them).
        support_ports.ticket_store: support_store,
        sales_ports.lead_store: sales_store,
        ops_ports.incident_store: ops_store,
        ops_ports.metrics_reader: ops_metrics_client.PrometheusMetricsReader(),
        ecorp_ports.knowledge_base: ecorp_store,
        ecorp_ports.employee_directory: ecorp_store,
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


async def close_adapters() -> None:
    """Ends what the adapters hold open on the running loop: the MCP connections, each a child process (the sandbox bridge). Call it where a
    process stops, on the loop that served the requests, before it closes the pools the application opened. A no-op if nothing was opened."""
    await close_all_connections()

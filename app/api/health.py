"""Real dependency checks for `GET /health/ready` (app/api/main.py) — distinct
from `GET /health`'s unconditional liveness probe.

A liveness probe answers "is this process alive"; a readiness probe
answers "can this process actually serve a request right now." Before this
module existed, `GET /health` only ever answered the first question — a
static `{"status": "ok"}` regardless of whether Qdrant/Postgres/Redis were
reachable (verified: it doesn't touch any of them) — so an orchestrator
had no signal to hold traffic back from an instance that was up but
couldn't actually complete a turn. `GET /health` stays exactly as it was
(a real liveness probe still needs to be cheap and dependency-free, so an
orchestrator can tell "the process itself is wedged" apart from "a
downstream store is down" — conflating the two into one endpoint would
make both questions unanswerable); this is the new, separate readiness
question.

Each check is independent and bounded by its own short timeout — a hung
dependency must make readiness FAIL FAST (report that one dependency as
down), not hang this endpoint itself waiting on it.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx
import psycopg

from app.agent import sql_store
from app.core.config import CHECKPOINTER_DATABASE_URL, ML_SERVICE_URL
from app.job_queue import queue
from app.retrieval import qdrant_store

logger = logging.getLogger(__name__)

_CHECK_TIMEOUT_SECONDS = 2.0


async def _check_appdata_postgres() -> None:
    async with sql_store.get_connection() as conn:
        await conn.execute("SELECT 1")


async def _check_checkpointer_postgres() -> None:
    # A separate, throwaway connection — not the graph's own
    # AsyncPostgresSaver (app/agent/runtime.py), which may not even be open on
    # THIS event loop yet when this is checked (see that module's own
    # docstring on why the checkpointer is tied to a specific loop).
    # Reachability of the DATABASE is what matters for readiness,
    # independent of whether the graph singleton has been initialized.
    async with await psycopg.AsyncConnection.connect(
        CHECKPOINTER_DATABASE_URL, connect_timeout=2
    ) as conn:
        await conn.execute("SELECT 1")


async def _check_qdrant() -> None:
    await qdrant_store.get_client().get_collections()


async def _check_redis() -> None:
    await queue.get_client().ping()


async def _check_ml_service() -> None:
    # Same "check it, but a turn still completes without it" posture as
    # `redis` above: both of this container's consumers already degrade
    # rather than fail the turn on its absence —
    # app/retrieval/qdrant_store.py::hybrid_search degrades a reranker
    # failure to the RRF-fused order (agent_retrieval_degraded_total
    # {stage="rerank"} records it), and app/agent/moderation.py::screen
    # fails open on its ML layer, falling back to the pattern-based result
    # alone (agent_moderation_ml_degraded_total records it) — so this
    # being down never makes readiness overall fail a turn's worth of
    # traffic. Still worth surfacing here rather than silently absorbed,
    # same reasoning.
    async with httpx.AsyncClient(timeout=_CHECK_TIMEOUT_SECONDS) as client:
        resp = await client.get(f"{ML_SERVICE_URL}/health")
    resp.raise_for_status()


async def _bounded(name: str, check: Callable[[], None | Awaitable[None]]) -> bool:
    try:
        # iscoroutinefunction, checked BEFORE calling `check` — not after:
        # a plain sync function runs its ENTIRE body the instant it's
        # called, so deciding to run it via asyncio.to_thread only after
        # already calling it directly would defeat the whole point (it'd
        # have already blocked this event loop by then).
        if asyncio.iscoroutinefunction(check):
            await asyncio.wait_for(check(), timeout=_CHECK_TIMEOUT_SECONDS)
        else:
            await asyncio.wait_for(
                asyncio.to_thread(check), timeout=_CHECK_TIMEOUT_SECONDS
            )
        return True
    except Exception as exc:  # noqa: BLE001 - a dependency being down is this function's normal "false" outcome, not a bug
        logger.warning(
            "readiness_check_failed",
            extra={"dependency": name, "error_class": type(exc).__name__},
        )
        return False


async def check_dependencies() -> dict[str, bool]:
    """Every dependency this app needs to actually complete a turn, probed
    concurrently (not sequentially — a slow one shouldn't queue behind
    another slow one when both are independent). `qdrant`/`appdata_postgres`/
    `checkpointer_postgres` are always checked; `redis` only matters to a
    deployment actually using the semantic cache or the queued path, but
    checking it unconditionally is cheap and its absence is still a real
    degradation worth surfacing rather than silently ignoring.
    """
    qdrant_ok, appdata_ok, checkpointer_ok, redis_ok, ml_service_ok = await asyncio.gather(
        _bounded("qdrant", _check_qdrant),
        _bounded("appdata_postgres", _check_appdata_postgres),
        _bounded("checkpointer_postgres", _check_checkpointer_postgres),
        _bounded("redis", _check_redis),
        _bounded("ml_service", _check_ml_service),
    )
    return {
        "qdrant": qdrant_ok,
        "appdata_postgres": appdata_ok,
        "checkpointer_postgres": checkpointer_ok,
        "redis": redis_ok,
        "ml_service": ml_service_ok,
    }

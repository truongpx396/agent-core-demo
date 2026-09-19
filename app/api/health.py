"""Real dependency checks for `GET /health/ready` (app/api/main.py) — distinct
from `GET /health`'s unconditional liveness probe.

A liveness probe answers "is this process alive"; readiness answers "can
it actually serve a request right now." Keeping them separate lets an
orchestrator tell "the process is wedged" apart from "a downstream store
is down" — conflating the two would make both questions unanswerable.

Each check is independent and bounded by its own short timeout — a hung
dependency must make readiness FAIL FAST, not hang this endpoint waiting
on it.
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
    # A separate, throwaway connection — not the graph's own AsyncPostgresSaver
    # (app/agent/runtime.py), which may not be open on THIS event loop yet
    # (it's bound to whichever loop calls it). Readiness only cares whether
    # the database itself is reachable, not whether the singleton is initialized.
    async with await psycopg.AsyncConnection.connect(
        CHECKPOINTER_DATABASE_URL, connect_timeout=2
    ) as conn:
        await conn.execute("SELECT 1")


async def _check_qdrant() -> None:
    await qdrant_store.get_client().get_collections()


async def _check_redis() -> None:
    await queue.get_client().ping()


async def _check_ml_service() -> None:
    # Its consumers already degrade rather than fail the turn when this is
    # down (qdrant_store.py::hybrid_search falls back to RRF-fused order,
    # moderation.py::screen falls back to pattern-only) — still worth
    # surfacing here rather than silently absorbed.
    async with httpx.AsyncClient(timeout=_CHECK_TIMEOUT_SECONDS) as client:
        resp = await client.get(f"{ML_SERVICE_URL}/health")
    resp.raise_for_status()


async def _bounded(name: str, check: Callable[[], None | Awaitable[None]]) -> bool:
    try:
        # Checked BEFORE calling `check`, not after: a sync function runs its
        # entire body the instant it's called, so deferring to asyncio.to_thread
        # only after calling it directly would already have blocked the loop.
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
    """Every dependency this app needs to complete a turn, probed
    concurrently (not sequentially — independent slow checks shouldn't
    queue behind each other)."""
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

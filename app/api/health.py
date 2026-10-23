"""Real dependency checks for `GET /health/ready` (app/api/routers/system.py) — distinct
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

from app.core import sql_store
from app.core.config import (
    CHECKPOINTER_DATABASE_URL,
    COLLECTION,
    ML_SERVICE_URL,
    SKILLS_COLLECTION,
)
from app.job_queue import queue
from app.retrieval import qdrant_store

logger = logging.getLogger(__name__)

_CHECK_TIMEOUT_SECONDS = 2.0


async def _check_appdata_postgres() -> None:
    async with sql_store.get_connection() as conn:
        await conn.execute("SELECT 1")


async def _check_checkpointer_postgres() -> None:
    # A separate, throwaway connection — not the graph's own AsyncPostgresSaver
    # (app/agent/runtime/core.py), which may not be open on THIS event loop yet
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
    # app/core/moderation.py::screen falls back to pattern-only) — still worth
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


_WARNINGS_TIMEOUT_SECONDS = 3.0


async def _embedding_identity_warnings() -> list[str]:
    warnings: list[str] = []
    for logical in (COLLECTION, SKILLS_COLLECTION):
        status = await qdrant_store.identity_status(logical)
        if status.state == "mismatch":
            warnings.append(
                f"embedding identity mismatch: {logical} holds {status.found}, this process is configured for {status.expected}; "
                f"retrieval from it is refused. Run `make reembed COLLECTION_NAME={logical}`."
            )
        elif status.state == "adopted":
            warnings.append(
                f"embedding identity adopted (unverified): {logical} predates identity tracking; `make reembed COLLECTION_NAME={logical}` makes it verifiable."
            )
    return warnings


async def _ml_service_warnings() -> list[str]:
    async with httpx.AsyncClient(timeout=_CHECK_TIMEOUT_SECONDS) as client:
        resp = await client.get(f"{ML_SERVICE_URL}/health/models")
    resp.raise_for_status()
    body = resp.json()
    warnings = [
        f"ML service model {name} is {info.get('state')}" + (f": {info['reason']}" if info.get("reason") else "")
        for name, info in body.get("models", {}).items()
        if info.get("state") != "ready"
    ]
    # A label set (specs/015) that is refused is otherwise a quiet line nobody reads, and the router then decides from its rules alone. `pending` is
    # not reported: a set is indexed within seconds of a model becoming ready, and a model that cannot index is already a model warning above.
    labelsets = body.get("labelsets")
    for name, info in (labelsets.items() if isinstance(labelsets, dict) else ()):
        if not isinstance(info, dict):
            continue
        if str(info.get("state", "")).startswith("refused"):
            warnings.append(f"ML service label set {name} is {info['state']}")
        models = info.get("models")
        for model, status in (models.items() if isinstance(models, dict) else ()):
            if str(status).startswith("refused"):
                warnings.append(f"ML service label set {name} on {model} is {status}")
    return warnings


async def readiness_warnings() -> list[str]:
    """Things a person should know that do not make the API unready. Each source is independent and bounded; one that cannot be read
    contributes nothing (the dependency check above already reports an unreachable Qdrant or ML service), it never fails the endpoint."""
    results = await asyncio.gather(
        *(
            asyncio.wait_for(source(), timeout=_WARNINGS_TIMEOUT_SECONDS)
            for source in (_embedding_identity_warnings, _ml_service_warnings)
        ),
        return_exceptions=True,
    )
    return [w for result in results if isinstance(result, list) for w in result]

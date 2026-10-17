"""Fixtures for tests/integration/ — real Postgres/Redis/Qdrant/crawl4ai,
no LLM (`@pytest.mark.integration`, `make test-integration`).

`crawl4ai_server` — `tests/containers.py::ensure_crawl4ai()`, used only by
test_web_crawler_live.py (moved here from `tests/live/` 2026-09-17, once
`ensure_crawl4ai()` made it self-provisioned like every other fixture in
this package — no LLM/graph dependency of that file's own ever justified
`tests/live/` specifically). Own copy rather than reusing
`tests/live/conftest.py`'s fixture of the same name — same "each package
wraps the shared `ensure_*()` helper itself" split `tests/deepeval/conftest.py`'s
own `crawl4ai_server` already takes relative to `tests/live/conftest.py`'s
(test_domain_crawl_tools_live.py stayed in `tests/live/`, so that package
still needs its own copy too) — `ensure_crawl4ai()`'s own cross-worker
`_acquire` cache means calling it from multiple packages' conftests within
the same pytest run still only starts one real container.
"""
import pytest

from tests.containers import ensure_crawl4ai, ensure_ml_service


@pytest.fixture(scope="session")
def crawl4ai_server() -> dict[str, str]:
    return ensure_crawl4ai()


@pytest.fixture(scope="session")
def ml_service() -> dict[str, str]:
    """The real ML service image (docker/ml-service.Dockerfile), built and started once per pytest run by the shared
    `ensure_ml_service()` (its cross-worker cache means every package asking for it still gets one container)."""
    return ensure_ml_service()


@pytest.fixture(scope="session")
def ml_service_url(ml_service: dict[str, str]) -> str:
    return ml_service["ml_service_url"]

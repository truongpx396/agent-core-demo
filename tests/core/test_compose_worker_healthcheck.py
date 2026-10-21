"""A worker container must not inherit the API's healthcheck.

The app image's `HEALTHCHECK` (Dockerfile) probes `http://localhost:8000/health/ready`. The agent
workers and the ingest worker run the same image with a different `command` and bind no port, and a
compose service inherits the image's healthcheck unless it says otherwise, so every worker reported
`unhealthy` for as long as it ran (`docker ps`: a failing streak of 152 after five hours, "Connection
refused" on every probe). That hides a service that really is unhealthy, and would restart-loop a
healthy one under anything that acts on container health. The Dockerfile's own comment said the
workers overrode it; only their `command` was overridden.

A TEXT test of the compose files, like `test_compose_layout.py`: no Docker needed.
"""
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILES = sorted((REPO / "deploy" / "compose").glob("docker-compose*.yml"))


def _runs_the_app_image(spec: dict) -> bool:
    image = str(spec.get("image") or "")
    return spec.get("build") == "." or image.startswith("${APP_IMAGE}")


def _non_api_roles(compose: dict) -> dict[str, dict]:
    """Services running the app's image with a `command` of their own: not the API, which is the image's
    default role (uvicorn) and the one the image's healthcheck is for."""
    return {
        name: spec
        for name, spec in (compose.get("services") or {}).items()
        if _runs_the_app_image(spec) and spec.get("command") and "uvicorn" not in " ".join(spec["command"])
    }


def test_the_workers_are_found_in_both_files_or_this_test_proves_nothing():
    found = {
        f.name: sorted(_non_api_roles(yaml.safe_load(f.read_text())))
        for f in COMPOSE_FILES
        if _non_api_roles(yaml.safe_load(f.read_text()))
    }
    assert found["docker-compose.yml"] == [
        "agent-worker",
        "agent-worker-ops",
        "agent-worker-sales",
        "agent-worker-support",
        "ingest-worker",
    ]
    assert found["docker-compose.prod.yml"] == ["agent-worker", "ingest-worker"]


@pytest.mark.parametrize("compose_file", COMPOSE_FILES, ids=lambda f: f.name)
def test_a_worker_does_not_inherit_the_apis_healthcheck(compose_file):
    for name, spec in _non_api_roles(yaml.safe_load(compose_file.read_text())).items():
        healthcheck = spec.get("healthcheck")
        assert healthcheck, (
            f"{compose_file.name}: {name} runs the app image with its own command and sets no healthcheck, so it "
            "inherits the API's probe of :8000, a port a worker never opens. Add `healthcheck: *worker-healthcheck`."
        )
        assert healthcheck.get("disable") is True or healthcheck.get("test"), (
            f"{compose_file.name}: {name}'s healthcheck is neither disabled nor a probe of its own"
        )


def test_the_image_really_does_carry_the_apis_healthcheck():
    """The premise: if the Dockerfile stops declaring one, the override above is dead weight and this says so."""
    dockerfile = (REPO / "Dockerfile").read_text()
    assert "HEALTHCHECK" in dockerfile and "localhost:8000/health/ready" in dockerfile

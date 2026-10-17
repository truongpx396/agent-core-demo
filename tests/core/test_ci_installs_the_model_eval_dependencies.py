"""The CI `test` job must install the packages the hermetic suite imports that the lock file does not carry (specs/014, analysis H1).

`tests/ml_service` imports `ml_service.metrics` (prometheus-client) and `tests/scripts/model_eval` reads parquet (pyarrow). Neither is
in `requirements-lock.txt`: the lock is a machine-generated freeze of requirements.txt, and these are a dev tool and an auxiliary
image's dependencies. The `test` job installs only the lock file plus what it names, so a pin that exists in requirements-dev.txt
alone never reaches CI and the suite passes locally and fails there. This is the same shape as tests/billing/test_ci_installs_the_package.py.

Text-only: reads the workflow and the requirements files.
"""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PINNED_FOR_THE_TEST_JOB = ("pyarrow", "prometheus-client")


def _pins(text: str) -> dict[str, str]:
    return {m.group(1).lower(): m.group(2) for m in re.finditer(r"^([A-Za-z][\w.-]*)==([\w.]+)", text, re.MULTILINE)}


def _test_job_install_text() -> str:
    workflow = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["test"]["steps"]
    return " ".join(" ".join(str(s.get("run", "")).split()) for s in steps if "pip install" in str(s.get("run", "")))


def test_the_dev_requirements_pin_what_the_test_job_installs():
    dev = _pins((REPO / "requirements-dev.txt").read_text())
    job = _pins(_test_job_install_text().replace(" ", "\n"))
    for name in PINNED_FOR_THE_TEST_JOB:
        assert name in dev, f"{name} is not pinned in requirements-dev.txt"
        assert job.get(name) == dev[name], f"the CI test job installs {name}=={job.get(name)} but requirements-dev.txt pins {dev[name]}"


def test_the_ml_service_image_pins_the_same_prometheus_client_as_the_test_job():
    image = _pins((REPO / "docker/ml-service/requirements.txt").read_text())
    dev = _pins((REPO / "requirements-dev.txt").read_text())
    assert image.get("prometheus-client") == dev.get("prometheus-client")


def test_neither_package_leaks_into_the_runtime_requirements_or_the_lock_file():
    for name in ("requirements.txt", "requirements-lock.txt"):
        text = (REPO / name).read_text().lower()
        assert "pyarrow" not in text and "prometheus-client" not in text, f"{name} must not carry a dev-only / aux-image dependency"

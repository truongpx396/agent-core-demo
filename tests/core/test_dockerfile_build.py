"""The shape of the two production images (ENGINEERING_PATTERNS.md pattern 76): the root `Dockerfile` (api and
both worker roles) and `docker/ml-service.Dockerfile`, built the same way, so one set of tests covers both.
Four stages each: `base` (Python image, pinned by digest), `uv` (installer, pinned by digest), `builder`
(installs dependencies into a virtualenv; thrown away) and `runtime` (the LAST stage, so what `docker build .`
and every compose `build:` produce; it copies the virtualenv in and ships no installer).

A TEXT test, like tests/core/test_dockerfile_runtime_files.py: no Docker needed. It can't prove an image works
(CI's `docker-build` job and the integration tier do); it fails in the default suite the moment one of these
easily lost properties is edited away:
- the stage that ships is the LAST one, and the builder, with uv in it, never is;
- the Python image and uv are pinned by digest, to the SAME digests in both files (a bump is one change made
  twice, and a test fails if only one is made), each a named `FROM` stage so Dependabot can propose the bump;
- installs use a cache mount, not `--no-cache-dir` (that made a lock change re-download every package);
- the dependency file is installed from a bind mount, so the step reruns exactly when its content changes;
- the shipped stage has no installer and takes its dependencies from the builder.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCKERFILES = {"app": REPO / "Dockerfile", "ml-service": REPO / "docker" / "ml-service.Dockerfile"}
# The file each image installs from, as the bind mount names it.
DEPENDENCY_FILE = {"app": "requirements-lock.txt", "ml-service": "docker/ml-service/requirements.txt"}
DIGESTED = r"@sha256:[0-9a-f]{64}"


def _instructions(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _stages(path: Path) -> dict[str, dict]:
    """Stage name -> {"from": the image or stage it starts FROM, "body": its instructions with comments dropped and `\\` continuations
    joined}, in file order."""
    stages: dict[str, dict] = {}
    for chunk in re.split(r"(?m)^(?=FROM\s)", path.read_text()):
        match = re.match(r"FROM\s+(\S+)\s+AS\s+(\S+)", chunk)
        if match:
            stages[match.group(2)] = {"from": match.group(1), "body": re.sub(r"\\\n\s*", " ", _instructions(chunk))}
    return stages


def _runs(body: str) -> list[str]:
    """The `RUN` instructions of a stage body, one per line (`_stages` joins continuations), so a check looks at ONE instruction and not at
    whatever text happens to follow it up to the next `RUN`."""
    return [line for line in body.splitlines() if line.startswith("RUN ")]


@pytest.fixture(params=list(DOCKERFILES))
def image(request):
    path = DOCKERFILES[request.param]
    return request.param, path, _stages(path)


def test_there_are_four_stages_and_the_one_that_ships_is_last(image):
    _, _, stages = image

    assert list(stages) == ["base", "uv", "builder", "runtime"]


def test_the_python_image_is_pinned_by_digest_in_one_place_and_both_stages_build_on_it(image):
    _, _, stages = image

    assert re.fullmatch(rf"python:3\.13-slim{DIGESTED}", stages["base"]["from"]), stages["base"]["from"]
    assert stages["builder"]["from"] == "base" and stages["runtime"]["from"] == "base", "one base: the virtualenv links to the interpreter it was built for"


def test_uv_is_pinned_by_digest_as_its_own_stage_and_only_the_builder_copies_it(image):
    _, _, stages = image

    assert re.fullmatch(rf"ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+{DIGESTED}", stages["uv"]["from"]), stages["uv"]["from"]
    assert "COPY --from=uv /uv /usr/local/bin/uv" in stages["builder"]["body"]
    runtime = stages["runtime"]["body"]
    assert "--from=uv" not in runtime and "astral-sh" not in runtime and "uv pip" not in runtime, "uv must not reach the shipped stage"


def test_both_dockerfiles_pin_the_same_python_image_and_the_same_uv():
    """One bump, made twice: a digest changed in only one file would put two images on different bases."""
    a, b = (_stages(path) for path in DOCKERFILES.values())

    assert a["base"]["from"] == b["base"]["from"]
    assert a["uv"]["from"] == b["uv"]["from"]


def test_the_install_uses_the_uv_cache_mount_and_a_bind_mount_of_the_dependency_file(image):
    name, _, stages = image
    install = next(run for run in _runs(stages["builder"]["body"]) if "uv pip install -r" in run)
    bind = re.search(r"--mount=type=bind,source=(\S+),target=(\S+)", install)

    assert "--mount=type=cache,target=/root/.cache/uv" in install
    assert bind and bind.group(1) == DEPENDENCY_FILE[name], "bind-mounted: the step reruns exactly when the file's content changes"
    assert f"uv pip install -r {bind.group(2)}" in install, "the install must read the file from where the bind mount puts it"


def test_nothing_turns_the_package_cache_off_because_that_is_what_made_every_miss_a_full_download(image):
    _, path, _ = image

    assert "--no-cache-dir" not in _instructions(path.read_text())


def test_the_builder_installs_into_the_virtualenv_the_runtime_copies_from_the_same_path(image):
    _, _, stages = image

    assert "VIRTUAL_ENV=/opt/venv" in stages["builder"]["body"] and "uv venv" in stages["builder"]["body"]
    assert "COPY --from=builder /opt/venv /opt/venv" in stages["runtime"]["body"]
    assert re.search(r"ENV PATH=/opt/venv/bin:\$PATH", stages["runtime"]["body"]), "the shipped stage must put the virtualenv first on PATH"


def test_bytecode_is_compiled_at_build_time_not_on_every_start_of_a_non_root_container(image):
    _, _, stages = image

    assert "UV_COMPILE_BYTECODE=1" in stages["builder"]["body"]


def test_the_shipped_stage_has_no_pip_installs_nothing_and_runs_as_the_app_user_with_its_probe(image):
    _, _, stages = image
    runtime = stages["runtime"]["body"]

    assert "python -m pip uninstall -y pip" in runtime, "the base image's pip (and its vendored certifi) must not ship"
    assert "pip install" not in runtime and "uv pip" not in runtime
    assert "USER appuser" in runtime
    assert "HEALTHCHECK" in runtime and "CMD [" in runtime


def test_the_billing_package_is_installed_without_its_dependencies_from_the_builder_only():
    stages = _stages(DOCKERFILES["app"])

    install = next(run for run in _runs(stages["builder"]["body"]) if "uv pip install" in run and "/tmp/billing" in run)

    assert "uv pip install --no-deps /tmp/billing" in install
    assert "--mount=type=cache,target=/root/.cache/uv" in install
    assert "packages/billing" not in stages["runtime"]["body"], "the package is installed in the virtualenv; its source must not ship"


def test_the_ml_service_keeps_what_its_healthcheck_and_its_model_cache_volume_need():
    """curl is the HEALTHCHECK's client; the cache directory must exist and be owned by the app user BEFORE a named volume is mounted over
    it (docker-compose.yml), or the volume is seeded root-owned and the first model download fails with 'Permission denied'."""
    runtime = _stages(DOCKERFILES["ml-service"])["runtime"]["body"]
    install = next(run for run in _runs(runtime) if "apt-get install" in run)

    assert re.search(r"\bcurl\b", install), "curl must be INSTALLED: the word also appears in the HEALTHCHECK, which fails without the binary"
    assert "curl -sf http://localhost:80/health" in runtime
    assert "mkdir -p /home/appuser/.cache" in runtime and "chown -R appuser:appuser /home/appuser" in runtime


# --- the test harness must build with BuildKit ----------------------------------------------------------------------------------
#
# The first push of pattern 76 failed `test-integration` and `test-live` with "the --mount option requires BuildKit": tests/containers.py built
# the ML service image through the `docker` SDK's `images.build`, whose builder is the legacy one. The Dockerfiles use BuildKit's cache mounts,
# so anything that builds them must be BuildKit.


def test_no_test_builds_an_image_through_the_sdks_legacy_builder():
    offenders = [
        str(path.relative_to(REPO))
        for path in sorted((REPO / "tests").rglob("*.py"))
        if path != Path(__file__) and re.search(r"\.images\.build\(", path.read_text())
    ]

    assert not offenders, f"these build with the legacy builder, which rejects `RUN --mount`: {offenders}"


def test_the_harness_builds_with_the_docker_cli_and_buildkit_and_reports_a_failure_with_its_output(monkeypatch):
    from tests import containers

    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Result", (), {"returncode": 1, "stdout": "step 3", "stderr": "boom from the builder"})()

    monkeypatch.setattr(containers.subprocess, "run", fake_run)
    monkeypatch.setattr(containers.shutil, "which", lambda name: f"/usr/bin/{name}")

    with pytest.raises(RuntimeError, match="boom from the builder"):
        containers._build_image("docker/ml-service.Dockerfile", "tag:test")

    (argv, kwargs), = calls
    assert argv[:2] == ["/usr/bin/docker", "build"] and "--file" in argv and "docker/ml-service.Dockerfile" in argv and "tag:test" in argv
    assert kwargs["env"]["DOCKER_BUILDKIT"] == "1" and kwargs["check"] is False

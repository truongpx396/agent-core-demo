"""The shape of the image build (ENGINEERING_PATTERNS.md pattern 76): two stages, uv pinned by digest, a cache mount, and nothing that
installs software in what ships.

A TEXT test of the Dockerfile, like tests/core/test_dockerfile_runtime_files.py: no Docker needed. It cannot prove the image works;
that is CI's `docker-build` job, which builds it and runs the result. What it adds is a failure in the default suite the moment one of
the properties below is edited away, each of which is easy to lose in a refactor and invisible until an incident or a slow week:

  * the stage that ships is the LAST one (`docker build .` and every `build: .` in compose build the last stage; reorder them and the
    builder, with uv in it, would be what runs in production);
  * uv is pinned by digest and exists only in the builder;
  * the installs use a cache mount and not `--no-cache-dir` (that flag is what made a lock change re-download all 189 packages);
  * the lock is installed from a bind mount of the file, so the step reruns exactly when the lock changes;
  * the shipped stage has no installer and takes its dependencies from the builder.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = (REPO / "Dockerfile").read_text()


def _instructions(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _stages() -> dict[str, str]:
    """Stage name -> its instructions (comments dropped, `\\` continuations joined), in file order."""
    stages: dict[str, str] = {}
    name = None
    for chunk in re.split(r"(?m)^(?=FROM\s)", DOCKERFILE):
        match = re.match(r"FROM\s+\S+\s+AS\s+(\S+)", chunk)
        if match:
            name = match.group(1)
            stages[name] = re.sub(r"\\\n\s*", " ", _instructions(chunk))
    return stages


def test_there_are_two_stages_and_the_one_that_ships_is_last():
    assert list(_stages()) == ["builder", "runtime"]


def test_the_stages_share_one_base_so_the_virtualenv_links_to_the_interpreter_it_was_built_for():
    bases = re.findall(r"(?m)^FROM\s+(\S+)\s+AS\s+\S+", DOCKERFILE)

    assert len(bases) == 2 and len(set(bases)) == 1, bases


def test_uv_is_pinned_by_digest_and_only_the_builder_has_it():
    stages = _stages()
    pin = re.search(r"COPY --from=ghcr\.io/astral-sh/uv:(\d+\.\d+\.\d+)@sha256:([0-9a-f]{64}) /uv /usr/local/bin/uv", stages["builder"])

    assert pin, "uv must be copied from its image as tag@sha256:<64 hex> (a moving tag would decide what runs in the build)"
    assert "uv" not in re.sub(r"astral-sh/uv", "", stages["runtime"]).replace("uvicorn", ""), "uv must not reach the shipped stage"


@pytest.mark.parametrize("step", ["install -r /tmp/requirements-lock.txt", "install --no-deps /tmp/billing"])
def test_every_install_uses_the_uv_cache_mount(step):
    builder = _stages()["builder"]
    line = next(part for part in builder.split("RUN ") if step in part)

    assert "--mount=type=cache,target=/root/.cache/uv" in line


def test_nothing_turns_the_package_cache_off_because_that_is_what_made_every_miss_a_full_download():
    assert "--no-cache-dir" not in _instructions(DOCKERFILE)


def test_the_lock_is_bind_mounted_so_the_step_reruns_exactly_when_its_content_changes():
    builder = _stages()["builder"]

    assert "--mount=type=bind,source=requirements-lock.txt,target=/tmp/requirements-lock.txt" in builder


def test_the_builder_installs_into_the_virtualenv_the_runtime_copies_from_the_same_path():
    stages = _stages()

    assert "VIRTUAL_ENV=/opt/venv" in stages["builder"] and "uv venv" in stages["builder"]
    assert "COPY --from=builder /opt/venv /opt/venv" in stages["runtime"]
    assert re.search(r"ENV PATH=/opt/venv/bin:\$PATH", stages["runtime"]), "the shipped stage must put the virtualenv first on PATH"


def test_bytecode_is_compiled_at_build_time_not_on_every_start_of_a_non_root_container():
    assert "UV_COMPILE_BYTECODE=1" in _stages()["builder"]


def test_the_shipped_stage_installs_nothing_and_still_runs_as_the_app_user_with_its_probe():
    runtime = _stages()["runtime"]

    assert "pip install" not in runtime and "uv pip" not in runtime
    assert "USER appuser" in runtime
    assert "HEALTHCHECK" in runtime and "CMD [" in runtime


def test_the_billing_package_is_installed_without_its_dependencies_from_the_builder_only():
    stages = _stages()

    assert "uv pip install --no-deps /tmp/billing" in stages["builder"]
    assert "packages/billing" not in stages["runtime"], "the package is installed in the virtualenv; its source must not ship"

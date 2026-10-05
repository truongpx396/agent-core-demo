"""Compose, Caddy and LiteLLM config live under `deploy/`, and every path they
reference must still resolve.

The compose files were moved out of the repo root, but they were written with
repo-root-relative paths (`./observability/...`, `./postgres-init`, `build: .`)
and read `.env` from the root. Compose resolves all of those against the PROJECT
DIRECTORY, which defaults to the directory of the first `-f` file, so the stack
only works when every caller passes `--project-directory .` — the Makefile via
its `COMPOSE` variable, `deploy.yml` on the droplets. Forget the flag and
nothing fails loudly: the dev project is silently renamed `compose` (orphaning
its volumes), `.env` stops loading, and the bind mounts point at directories
that Docker then CREATES empty.

This is a TEXT test of those contracts, with no Docker needed, so a drifted
mount path or a bare `docker compose` fails the default suite instead of a
deploy. It does not render the stack — `docker compose config` against the real
files is the check for that.
"""
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILES = sorted((REPO / "deploy" / "compose").glob("docker-compose*.yml"))


def _repo_paths(compose: dict) -> list[tuple[str, str]]:
    """(service, path) for every bind mount and build input that points into the
    repo, i.e. a relative host path. Named volumes have no leading `.`."""
    found: list[tuple[str, str]] = []
    for service, spec in (compose.get("services") or {}).items():
        for volume in spec.get("volumes") or []:
            source = volume.get("source") if isinstance(volume, dict) else volume.split(":")[0]
            if source and source.startswith("."):
                found.append((service, source))
        build = spec.get("build")
        if isinstance(build, str):
            found.append((service, build))
        elif isinstance(build, dict):
            context = build.get("context", ".")
            found.append((service, context))
            if "dockerfile" in build:
                found.append((service, str(Path(context) / build["dockerfile"])))
    return found


def test_compose_files_are_found_and_none_are_left_at_the_root():
    # The non-empty assert matters: an empty glob would let every parametrized
    # test below pass vacuously after a rename of the directory.
    assert len(COMPOSE_FILES) >= 5, f"expected the compose files under deploy/compose/, found {COMPOSE_FILES}"
    assert not list(REPO.glob("docker-compose*.yml")), (
        "a docker-compose*.yml is back at the repo root; compose files belong in deploy/compose/"
    )
    # Overlays such as the load-test file legitimately reference no repo paths, so
    # "the parser found something" is asserted over the whole set, not per file.
    parsed = [_repo_paths(yaml.safe_load(f.read_text())) for f in COMPOSE_FILES]
    assert sum(len(paths) for paths in parsed) >= 10, "the path parser found (almost) nothing; it is broken"


@pytest.mark.parametrize("compose_file", COMPOSE_FILES, ids=lambda p: p.name)
def test_every_host_path_resolves_from_the_repo_root(compose_file: Path):
    compose = yaml.safe_load(compose_file.read_text())
    missing = [(svc, p) for svc, p in _repo_paths(compose) if not (REPO / p).exists()]
    assert not missing, (
        f"{compose_file.name} mounts/builds paths that don't exist relative to the repo root "
        f"(the project directory): {missing}"
    )


def test_makefile_pins_the_project_directory_and_never_calls_bare_docker_compose():
    makefile = (REPO / "Makefile").read_text()
    for var in ("COMPOSE", "COMPOSE_OBS"):
        assert re.search(rf"^{var}\s*:=.*--project-directory \.", makefile, re.M), (
            f"{var} must pass --project-directory . (see this file's docstring)"
        )
    # Recipe lines only. DefectDojo's targets `cd` into its own checkout and drive
    # ITS compose project, so they start with `cd`/`@cd`, not `docker compose`.
    bare = [line for line in makefile.splitlines() if re.match(r"^\t@?docker compose\b", line)]
    assert not bare, f"recipes must go through $(COMPOSE)/$(COMPOSE_OBS), not bare docker compose: {bare}"


def test_deploy_workflow_pins_the_project_directory_and_ships_files_that_exist():
    text = (REPO / ".github" / "workflows" / "deploy.yml").read_text()
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    invocations = [line for line in code if re.search(r"docker compose .*-f ", line)]
    assert invocations, "deploy.yml no longer runs docker compose with -f; the parser is broken"
    unpinned = [line.strip() for line in invocations if "--project-directory ." not in line]
    assert not unpinned, f"droplet compose calls must pass --project-directory .: {unpinned}"

    shipped = set(re.findall(r"\bdeploy/[\w./-]*\w", "\n".join(code)))
    assert shipped, "deploy.yml references nothing under deploy/; the parser is broken"
    missing = sorted(p for p in shipped if not (REPO / p).exists())
    assert not missing, f"deploy.yml ships or runs files that don't exist: {missing}"

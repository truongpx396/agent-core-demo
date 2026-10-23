"""How the production images are published and kept fresh (ENGINEERING_PATTERNS.md pattern 76).

`.github/workflows/deploy.yml` builds the two images the droplet pulls (the app image and the ML service image) and pushes them to GHCR. This
file pins what that build says about WHAT it publishes (the named runtime stage, a software bill of materials, build provenance) and
`.github/dependabot.yml` pins how the digest-pinned base images stay fresh. The workflow itself only runs on `main` after CI, so none of this
could be run from a branch: it is the YAML read as text, plus the hermetic reasons behind each line.
"""
from pathlib import Path

import yaml

GITHUB = Path(__file__).resolve().parents[2] / ".github"


def _builds() -> list[dict]:
    workflow = yaml.safe_load((GITHUB / "workflows" / "deploy.yml").read_text())
    return [step["with"] for step in workflow["jobs"]["build-and-push"]["steps"] if "build-push-action" in str(step.get("uses", ""))]


def test_the_deploy_workflow_builds_exactly_the_two_images_the_droplet_pulls():
    assert [build["file"] for build in _builds()] == ["Dockerfile", "docker/ml-service.Dockerfile"]


def test_the_app_image_is_built_from_the_named_runtime_stage():
    """A reordered Dockerfile must fail this build, not publish the builder stage (which has uv in it)."""
    app = _builds()[0]

    assert app["target"] == "runtime"


def test_both_images_are_published_with_a_bill_of_materials_and_build_provenance():
    for build in _builds():
        assert build["sbom"] is True, build["file"]
        assert build["provenance"] == "mode=max", build["file"]


def test_both_builds_keep_the_gha_layer_cache_including_the_builder_stage():
    """`mode=max` exports every stage's layers; the default (`min`) exports only the final stage, which would throw away the dependency install."""
    for build in _builds():
        assert build["cache-from"] == "type=gha" and build["cache-to"] == "type=gha,mode=max", build["file"]


def _dependabot() -> list[dict]:
    return yaml.safe_load((GITHUB / "dependabot.yml").read_text())["updates"]


def test_dependabot_reads_both_dockerfiles_so_the_pinned_base_image_and_uv_get_fresh_digests():
    (docker,) = [entry for entry in _dependabot() if entry["package-ecosystem"] == "docker"]

    assert set(docker["directories"]) == {"/", "/docker"}
    assert docker["schedule"]["interval"] == "weekly"


def test_dependabot_waits_two_weeks_before_proposing_a_release_and_never_moves_the_python_minor():
    (docker,) = [entry for entry in _dependabot() if entry["package-ecosystem"] == "docker"]

    assert docker["cooldown"]["default-days"] >= 14
    (python,) = [rule for rule in docker["ignore"] if rule["dependency-name"] == "python"]
    assert set(python["update-types"]) == {"version-update:semver-major", "version-update:semver-minor"}


def test_dependabot_does_not_read_the_compose_files_where_it_would_propose_database_major_versions():
    assert "docker-compose" not in {entry["package-ecosystem"] for entry in _dependabot()}

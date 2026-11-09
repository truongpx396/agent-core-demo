"""The app image and the ML service image pin the same base image and installer, by digest, and Dependabot reads both directories.

`.github/dependabot.yml` says a test keeps the two Dockerfiles' pins equal; none did. Dependabot bumps each directory in its own PR, and a
pull request that touches one file leaves the two images on different `uv` or Python digests, which is the drift pinning by digest is meant
to stop. When #241 moved `docker/ml-service.Dockerfile` to `services/ml-service/Dockerfile`, a Dependabot PR opened before the move
(#221) still named the old path and conflicted; a Dockerfile in a directory the config does not list would never be proposed a bump at all.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = [ROOT / "Dockerfile", ROOT / "services" / "ml-service" / "Dockerfile"]
PIN = re.compile(r"^FROM (\S+) AS (base|uv)\s*$", re.MULTILINE)
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _pins(dockerfile: Path) -> dict[str, str]:
    return {stage: image for image, stage in PIN.findall(dockerfile.read_text(encoding="utf-8"))}


def test_the_app_and_the_ml_service_image_pin_the_same_base_and_installer():
    pins = [_pins(path) for path in DOCKERFILES]
    for path, found in zip(DOCKERFILES, pins, strict=True):
        assert set(found) == {"base", "uv"}, f"{path.relative_to(ROOT)} must have a `FROM <image> AS base` and a `FROM <image> AS uv` line: {found}"
    assert pins[0] == pins[1], f"the two Dockerfiles pin different images:\n{pins[0]}\n{pins[1]}"


def test_each_pin_names_a_digest_not_a_moving_tag():
    for path in DOCKERFILES:
        for stage, image in _pins(path).items():
            assert DIGEST.search(image), f"{path.relative_to(ROOT)}: the `{stage}` stage is `{image}`, not pinned by digest"


def test_dependabot_reads_the_directory_of_every_pinned_dockerfile():
    updates = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))["updates"]
    docker = next(entry for entry in updates if entry["package-ecosystem"] == "docker")
    listed = set(docker.get("directories", [docker.get("directory")]))
    needed = {"/" + path.parent.relative_to(ROOT).as_posix() if path.parent != ROOT else "/" for path in DOCKERFILES}
    assert needed <= listed, f"dependabot.yml's docker entry does not list {sorted(needed - listed)}: no bump would ever be proposed there"

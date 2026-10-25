"""The OpenSandbox pieces this repo runs must be on ONE release line.

Since release 1.1.0 OpenSandbox versions every artifact together ("one platform version covers server, component images, charts, CLI
and every published SDK, cut from one commit"), and supports a server and an SDK on the same line only: "server <-> CLI/SDK same
line supported; +-1 minor warns". This repo runs five of them, pinned in four places:

  * the SDK and the MCP bridge          requirements-lock.txt (and requirements.txt for the bridge, which brings the SDK)
  * the server                          docker/opensandbox-server.Dockerfile
  * the execd and egress sidecar images docker/opensandbox-server.toml

Until the bridge moved to 1.1.1 the server and both images stayed on 0.2.3 / v1.0.22 / v1.1.7 and nothing noticed: a hermetic suite
runs none of them together. This is a TEXT test of those pins, so the next bump cannot move one and forget the others. It cannot
prove the pieces work together; `make sandbox-up && make test-sandbox` does, by hand (GRAPH_PATTERNS.md pattern 50).
"""
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _pinned(text: str, package: str) -> str:
    match = re.search(rf"(?:^|\s){re.escape(package)}==([0-9][0-9A-Za-z.]*)\s*$", text, re.MULTILINE)
    assert match, f"{package}== is not pinned"
    return match.group(1)


def _line(version: str) -> tuple[int, int]:
    major, minor, *_ = (int(part) for part in version.split("."))
    return major, minor


def _image_version(image: str) -> str:
    match = re.search(r":release-([0-9][0-9.]*)(?:@|$)", image)
    assert match, f"{image!r} is not tagged release-<version> (the OpenSandbox umbrella tag)"
    return match.group(1)


def _config() -> dict:
    return tomllib.loads((REPO / "docker" / "opensandbox-server.toml").read_text())


def _versions() -> dict[str, str]:
    lock = (REPO / "requirements-lock.txt").read_text()
    config = _config()
    return {
        "opensandbox (SDK, requirements-lock.txt)": _pinned(lock, "opensandbox"),
        "opensandbox-mcp (bridge, requirements-lock.txt)": _pinned(lock, "opensandbox-mcp"),
        "opensandbox-mcp (bridge, requirements.txt)": _pinned((REPO / "requirements.txt").read_text(), "opensandbox-mcp"),
        "opensandbox-server (docker/opensandbox-server.Dockerfile)": _pinned(
            (REPO / "docker" / "opensandbox-server.Dockerfile").read_text(), "opensandbox-server"
        ),
        "execd image (docker/opensandbox-server.toml)": _image_version(config["runtime"]["execd_image"]),
        "egress image (docker/opensandbox-server.toml)": _image_version(config["egress"]["image"]),
    }


def test_the_server_sdk_bridge_and_both_images_are_on_one_release_line():
    versions = _versions()

    lines = {what: _line(version) for what, version in versions.items()}
    assert len(set(lines.values())) == 1, (
        "OpenSandbox supports a server and an SDK on the same release line only (\"+-1 minor warns\"); these differ:\n"
        + "\n".join(f"  {what}: {versions[what]}" for what in versions)
    )


def test_the_bridge_and_the_sdk_it_brings_are_the_same_release():
    versions = _versions()

    assert (
        versions["opensandbox-mcp (bridge, requirements-lock.txt)"] == versions["opensandbox-mcp (bridge, requirements.txt)"]
    ), "requirements-lock.txt is generated from requirements.txt: regenerate it"
    assert versions["opensandbox (SDK, requirements-lock.txt)"] == versions["opensandbox-mcp (bridge, requirements-lock.txt)"]


def test_the_sidecar_images_are_pinned_by_digest_not_only_by_a_tag_that_can_move():
    config = _config()

    for what, image in (("execd_image", config["runtime"]["execd_image"]), ("egress.image", config["egress"]["image"])):
        assert DIGEST.search(image), (
            f"{what} = {image!r} has no @sha256:<64 hex> digest; take it from the release's signed BOM "
            "(docs/releases/<version>.yaml in the OpenSandbox repo) together with the tag"
        )

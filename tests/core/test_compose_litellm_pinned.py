"""The LiteLLM image is pinned by digest, to the same digest in both compose files.

The gateway holds the provider keys and decides what a call costs, and the moving `main-stable` tag has changed behaviour under this app before
(the budget stop went from HTTP 400 to 429) and was the tag people pulled when a LiteLLM release on PyPI was compromised (March 2026). A digest
is immutable. The tag is kept next to it for the reader; Docker ignores it while a digest is present.

Static, reading the YAML. That the digest resolves to a multi-architecture index (linux/amd64 for CI and prod, linux/arm64 for a Mac) was read
from the registry with `docker manifest inspect` when it was chosen; a test cannot do that without the network.
"""
import re
from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose"
PINNED = re.compile(r"^ghcr\.io/berriai/litellm:main-stable@sha256:[0-9a-f]{64}$")


def _image(name: str) -> str:
    return yaml.safe_load((COMPOSE / name).read_text())["services"]["litellm"]["image"]


def test_both_compose_files_pin_the_gateway_by_digest():
    for name in ("docker-compose.yml", "docker-compose.prod.yml"):
        assert PINNED.match(_image(name)), f"{name}: {_image(name)!r} is not tag@sha256:<64 hex>"


def test_dev_and_prod_run_the_same_gateway_image():
    """A bump that touched one file would leave production on a build nobody ran locally (or the reverse)."""
    assert _image("docker-compose.yml") == _image("docker-compose.prod.yml")

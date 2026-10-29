"""The authlimit service's route table and its image (specs/018): the files the service ships, read without Docker.

The route table (`routes.json`) is the contract the chat API's real routes must all appear in (PR 2 tests that from the Python side); the
image is what runs. Static, like tests/core/test_authlimit_stack.py, which holds the compose, Caddy and Makefile half of the same service.
"""
import json
import re
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[2] / "services" / "authlimit"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}")


# --- the route table -------------------------------------------------------------------------------------------------------------------


def test_the_route_table_never_limits_cancel_or_reads_and_limits_an_unlisted_route():
    table = json.loads((SERVICE / "routes.json").read_text())
    by_class = {}
    for r in table["routes"]:
        by_class.setdefault(r["class"], []).append((r["method"], r["pattern"]))

    assert ("POST", "/chat/cancel") in by_class["cancel"], "stopping a runaway turn must never be throttled"
    assert table["fallback"] in table["limits"], "a route nobody listed is verified AND limited"
    for never_limited in ("cancel", "read", "public"):
        assert never_limited not in table["limits"], never_limited
    assert {p for _, p in by_class["turn"]} == {"/chat/stream/queued", "/chat/resume"}, "the same turn-creating routes the app's own limiter covered"
    assert {p for _, p in by_class["upload"]} == {"/ingest/upload"}


# --- the image -------------------------------------------------------------------------------------------------------------------------


def test_the_image_is_built_in_two_pinned_stages_and_runs_as_non_root_with_no_shell():
    text = (SERVICE / "Dockerfile").read_text()
    froms = re.findall(r"^FROM (\S+)", text, re.MULTILINE)

    assert len(froms) == 2
    for image in froms:
        assert DIGEST.search(image), f"{image} must be pinned by digest"
    assert "distroless" in froms[1], "no shell, no package manager in what ships"
    assert re.search(r"^USER nonroot", text, re.MULTILINE)
    assert not re.search(r"(?m)^RUN .*\b(apk|apt-get|apt|curl|wget)\b", text.split("AS runtime")[1])
    assert "CGO_ENABLED=0" in text, "a static binary needs no libc in the runtime image"
    assert (SERVICE / ".dockerignore").read_text().count("*.pem") == 1, "a key file must never enter the build context"


def test_the_image_says_how_to_check_it_without_a_shell():
    """Trivy's DS-0026 (and the bot's comment on the PR) asks for a HEALTHCHECK in the image itself, so a
    container run without compose is still checked.
    """
    runtime = (SERVICE / "Dockerfile").read_text().split("AS runtime")[1]
    healthcheck = re.search(r"(?m)^HEALTHCHECK .*CMD (\[.*\])\s*$", runtime)

    assert healthcheck, "the runtime stage needs a HEALTHCHECK in exec form (there is no shell to run a string form)"
    assert json.loads(healthcheck.group(1)) == ["/authlimit", "healthcheck"]

"""Runtime hardening of the images this repo builds, in both compose files (ENGINEERING_PATTERNS.md pattern 76).

The api, the agent workers, the ingest worker and the ML service run with a real PID 1 (`init`), without the ability to gain privileges
(`no-new-privileges`), without any Linux capability (`cap_drop: ALL`) and with a read-only root filesystem whose few runtime writes go to
size-capped tmpfs. The flags were run against the real image and read back with a real `docker compose config` when this was written; this
file keeps them from being lost to an edit, and keeps them from spreading to services whose write paths nobody has checked.

Static, reading the YAML (no Docker).
"""
from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose"
DEV, PROD = COMPOSE / "docker-compose.yml", COMPOSE / "docker-compose.prod.yml"
# The services running an image this repo builds, per file. Production only pulls them; the dev file builds them.
HARDENED = {
    DEV: ["api", "agent-worker", "agent-worker-support", "agent-worker-ops", "agent-worker-sales", "ingest-worker", "ml-service", "mcp-gateway"],
    PROD: ["api", "agent-worker", "ingest-worker", "ml-service"],
}
# Hardened on purpose but not an app-user Python image: the Go edge service (specs/018, services/authlimit) and its smoke-check stand-in
# upstream ship a static binary on a distroless non-root base with no shell, so they have no $HOME for a tmpfs to cover and a different set
# of write paths. They get their own check below instead of the Python roles' (which would demand /home/appuser).
GO_HARDENED = {
    DEV: ["authlimit", "authlimit-echo"],
    PROD: [],
}


def _services(path: Path) -> dict:
    return yaml.safe_load(path.read_text())["services"]


def test_every_image_this_repo_builds_runs_hardened_in_both_compose_files():
    for path, names in HARDENED.items():
        services = _services(path)
        for name in names:
            service = services[name]
            assert service.get("init") is True, f"{path.name} {name}: init"
            assert service.get("security_opt") == ["no-new-privileges:true"], f"{path.name} {name}: no-new-privileges"
            assert service.get("cap_drop") == ["ALL"], f"{path.name} {name}: cap_drop"
            assert service.get("read_only") is True, f"{path.name} {name}: read_only"


def test_the_flags_do_not_spread_to_services_nobody_has_checked_the_write_paths_of():
    """postgres, redis, qdrant, litellm, minio and the rest write all over their own filesystems; a read-only root on them is a different
    piece of work. The anchor is merged service by service, never applied to all."""
    for path, names in HARDENED.items():
        for name, service in _services(path).items():
            if name not in names and name not in GO_HARDENED[path]:
                assert not service.get("read_only") and not service.get("cap_drop"), f"{path.name} {name} was hardened without being listed here"


def test_the_go_edge_service_and_its_stand_in_keep_their_hardening():
    """They are listed apart from the Python roles (no $HOME, a distroless image), so nothing else would notice one losing a flag. Each runs
    as a numeric non-root user, which a distroless image needs spelled out, and writes nothing to its root filesystem."""
    for path, names in GO_HARDENED.items():
        services = _services(path)
        for name in names:
            service = services[name]
            assert service.get("init") is True, f"{path.name} {name}: init"
            assert service.get("security_opt") == ["no-new-privileges:true"], f"{path.name} {name}: no-new-privileges"
            assert service.get("cap_drop") == ["ALL"], f"{path.name} {name}: cap_drop"
            assert service.get("read_only") is True, f"{path.name} {name}: read_only"
            uid = str(service.get("user", "")).split(":")[0]
            assert uid.isdigit() or uid.startswith("${"), f"{path.name} {name}: user must be numeric (or the AUTHLIMIT_UID default), got {uid!r}"
            assert uid != "0", f"{path.name} {name}: not root"


def test_what_the_app_writes_at_runtime_goes_to_size_capped_ram_not_to_the_image():
    """Uploads spool to /tmp and libraries write dot-directories under $HOME. A tmpfs counts against the container's memory limit, so an
    uncapped one is a way to be OOM-killed by filling it."""
    for path, names in HARDENED.items():
        services = _services(path)
        for name in names:
            tmpfs = services[name]["tmpfs"]
            assert all(":size=" in entry for entry in tmpfs), f"{path.name} {name}: every tmpfs needs a size cap ({tmpfs})"
            if name != "api":
                assert any(entry.startswith("/tmp:") for entry in tmpfs), f"{path.name} {name}: /tmp"


def test_the_app_roles_get_the_whole_home_directory_as_tmpfs_owned_by_the_app_user():
    """Found by running the image read-only: crawl4ai creates ~/.crawl4ai when it is IMPORTED, so a tmpfs for ~/.cache alone left the api
    crashing at start. The whole home directory covers that and the next library's dot-directory; the uid/gid make it writable by the user the
    app runs as (a tmpfs is root-owned by default)."""
    for path, names in HARDENED.items():
        services = _services(path)
        for name in names:
            if name != "ml-service":
                assert "/home/appuser:size=64m,uid=1000,gid=1000" in services[name]["tmpfs"], f"{path.name} {name}: $HOME"


def test_the_ml_service_keeps_its_models_in_a_volume_and_no_tmpfs_covers_that_volume():
    for path in HARDENED:
        service = _services(path)["ml-service"]

        assert any(v.endswith(":/home/appuser/.cache") for v in service["volumes"]), f"{path.name}: the model cache is a named volume"
        assert not any(entry.startswith("/home/appuser") for entry in service["tmpfs"]), f"{path.name}: a tmpfs over the volume would hide it"


def test_in_dev_every_service_that_builds_the_root_dockerfile_names_the_stage_that_ships():
    """The last stage is what a plain build produces, and a test keeps it last; naming it as well means a reordered Dockerfile fails
    the build instead of quietly shipping the builder, which has uv in it."""
    services = _services(DEV)
    for name in ["api", "agent-worker", "agent-worker-support", "agent-worker-ops", "agent-worker-sales", "ingest-worker"]:
        assert services[name]["build"] == {"context": ".", "target": "runtime"}, name
    assert services["ml-service"]["build"]["target"] == "runtime"


def test_production_pulls_these_images_and_never_builds_them():
    for name in HARDENED[PROD]:
        assert "build" not in _services(PROD)[name], f"docker-compose.prod.yml {name}: the droplet pulls what CI built"


def test_the_role_that_receives_uploads_spools_them_to_disk_not_to_ram():
    """The multipart parser writes every uploaded file to /tmp before the handler runs, and the limits allow 5 x 25 MB per request
    (app/api/routers/ingest.py). A tmpfs holds that in RAM, counted against the container's memory limit (512m in production), and fills at
    the second concurrent large upload. So the api's /tmp is a disk-backed anonymous volume, still not the image's read-only layer, and only its
    $HOME is tmpfs. The ceiling is read from the settings, so a raised limit shows up here."""
    from app.core.config import Settings

    ceiling_mb = Settings.model_fields["max_upload_size_mb"].default * Settings.model_fields["max_upload_files_per_request"].default
    assert ceiling_mb >= 100, "premise: a single upload request can spool 100 MB or more"
    for path in HARDENED:
        api = _services(path)["api"]
        assert "/tmp" in api["volumes"], f"{path.name}: api /tmp must be disk-backed"
        assert not any(entry.startswith("/tmp") for entry in api["tmpfs"]), f"{path.name}: api /tmp must not be tmpfs"
        for worker in [n for n in HARDENED[path] if n not in ("api", "ml-service")]:
            assert "/tmp" not in (_services(path)[worker].get("volumes") or []), f"{path.name} {worker}: only the uploading role needs it"

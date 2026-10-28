"""The authlimit service in the gateway stack (specs/018): docker-compose.yml, the Caddyfile and the Makefile. The route table and the image
are in tests/core/test_authlimit_image.py.

Static, reading the YAML and the Caddyfile (no Docker, like tests/core/test_compose_gateway.py). Each test pins something the
design rests on and an edit could silently undo. The security-relevant ones say which property they protect.
"""
import json
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "compose" / "docker-compose.yml"
CADDYFILE = REPO / "deploy" / "caddy" / "Caddyfile.gateway"
SERVICE = REPO / "services" / "authlimit"


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _caddy() -> str:
    return CADDYFILE.read_text()


def _site(host: str) -> str:
    """The body of one site block of the Caddyfile, found by brace matching (the file nests braces inside sites)."""
    text = _caddy()
    start = text.index(f"https://{host}:8443 {{")
    depth, i = 0, text.index("{", start)
    for j in range(i, len(text)):
        depth += text[j] == "{"
        depth -= text[j] == "}"
        if depth == 0:
            return text[i + 1 : j]
    raise AssertionError(f"unterminated site {host}")


def _strip_comments(block: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in block.splitlines())


# --- compose ---------------------------------------------------------------------------------------------------------------------------


def test_authlimit_runs_two_replicas_by_default_in_the_gateway_profile_only():
    services = _services()

    assert services["authlimit"]["profiles"] == ["gateway"]
    assert services["authlimit"]["deploy"]["replicas"] == 2, "the maintainer's decision: two replicas by default (spec 018 Decision 1)"
    assert services["authlimit-echo"]["profiles"] == ["gateway"]


def test_the_authlimit_containers_are_hardened_like_the_other_images():
    services = _services()

    for name in ("authlimit", "authlimit-echo"):
        svc = services[name]
        assert svc["read_only"] is True, name
        assert svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name


def test_nothing_but_caddy_publishes_a_port_for_the_authlimit_stack():
    """The service answers a check, serves the public keys of the internal identity and reports readiness. None of that is for the host:
    only Caddy (loopback) is. A published port on the service would make the identity check reachable without the front door."""
    services = _services()

    assert "ports" not in services["authlimit"]
    assert "ports" not in services["authlimit-echo"]


def test_the_signing_keys_are_mounted_read_only_from_outside_the_repository():
    volumes = _services()["authlimit"]["volumes"]

    keys = [v for v in volumes if v.endswith(":/keys:ro")]
    assert len(keys) == 1, "the key directory is mounted exactly once, read-only"
    host_path = keys[0].split(":/keys")[0]
    assert ".cache/agent-core-demo-authlimit" in host_path, "outside the repository, like make mcp-gateway-secrets"
    assert not host_path.startswith("./"), "a path inside the repository would put a private key one `git add` from a commit"


def test_no_secret_is_written_into_the_compose_file_for_authlimit():
    env = _services()["authlimit"]["environment"]

    for key, value in env.items():
        assert not re.search(r"(?i)password|secret|private", key), f"{key}: the service takes no secret from the environment"
        assert "BEGIN" not in str(value)
    assert env["AUTHLIMIT_KEYS_DIR"] == "/keys"
    assert env["AUTHLIMIT_JWKS_URL"].startswith("http://casdoor:"), "keys are fetched in-network; only the issuer has to be public"
    assert env["AUTHLIMIT_REDIS_URL"] == "redis://redis:6379", "the existing Redis (its keys are prefixed, spec 018 A6)"


def test_readiness_not_just_liveness_gates_the_container_and_caddy_starts_after_it():
    services = _services()

    assert services["authlimit"]["healthcheck"]["test"] == ["CMD", "/authlimit", "healthcheck", "--ready"], (
        "ready means 'can decide a request' (a signing key, usable Casdoor keys, a trustworthy revocation list)"
    )
    caddy = services["caddy-gateway"]["depends_on"]
    assert caddy["authlimit"]["condition"] == "service_healthy"
    assert caddy["authlimit-echo"]["condition"] == "service_healthy"
    assert services["caddy-gateway"]["environment"]["AUTHLIMIT_UPSTREAM"] == "${AUTHLIMIT_UPSTREAM:-authlimit-echo:8000}"


def test_the_container_user_is_not_root_even_when_the_host_user_override_is_unset():
    user = _services()["authlimit"]["user"]

    assert user == "${AUTHLIMIT_UID:-65532}:${AUTHLIMIT_GID:-65532}"
    assert _services()["authlimit-echo"]["user"] == "65532:65532"


def test_both_services_build_the_one_image_from_the_services_directory_not_the_root_dockerfile():
    services = _services()

    for name in ("authlimit", "authlimit-echo"):
        assert services[name]["build"]["context"] == "./services/authlimit", name
        assert services[name]["image"] == "authlimit:dev", name


# --- the Caddyfile ---------------------------------------------------------------------------------------------------------------------


def test_the_chat_api_check_comes_before_the_authorization_header_is_removed_and_before_the_proxy():
    body = _strip_comments(_site("api.localhost"))
    last_handle = body[body.rindex("handle {") :]

    assert last_handle.index("forward_auth") < last_handle.index("request_header -Authorization") < last_handle.index("reverse_proxy"), (
        "check, then strip the external token, then proxy: stripping first would starve the check of the token it must verify"
    )
    assert "copy_headers X-Internal-Identity" in last_handle
    assert "uri /v1/check" in last_handle
    assert "request_header -X-Tenant-Id" in last_handle and "request_header -X-Principal-Id" in last_handle, (
        "the old way to name the caller must not reach the API through the door, so an API flipped to header mode behind it stays safe"
    )


def test_the_chat_api_never_forwards_the_external_token_to_the_upstream_on_any_checked_route():
    body = _strip_comments(_site("api.localhost"))

    # Every handle that proxies WITHOUT the check must be a public one; the one with the check must strip.
    checked = [h for h in body.split("handle ")[1:] if "forward_auth" in h]
    assert len(checked) == 1
    assert "request_header -Authorization" in checked[0]


def test_the_mcp_routes_ask_authlimit_in_limit_only_mode_and_keep_the_authorization_header():
    body = _strip_comments(_site("mcp.localhost"))
    mcp_handle = body[body.index("handle @mcp {") : body.index("@mcp_metadata")]

    assert "forward_auth" in mcp_handle and "uri /v1/check" in mcp_handle
    assert "copy_headers" not in mcp_handle, "no identity is minted for an MCP route: the gateway verifies the external token itself"
    assert "request_header -Authorization" not in mcp_handle, "the MCP gateway needs the token (the MCP specification requires it to validate it)"
    assert mcp_handle.index("forward_auth") < mcp_handle.index("reverse_proxy")


def test_the_mcp_discovery_documents_skip_the_check():
    body = _strip_comments(_site("mcp.localhost"))
    metadata = body[body.index("handle @mcp_metadata {") :]
    metadata = metadata[: metadata.index("handle {")]

    assert "forward_auth" not in metadata, "a client that has no token yet must be able to discover where to get one"


def test_the_services_internal_endpoints_are_not_routed_by_caddy():
    """/internal/jwks.json, /readyz and /healthz answer anyone who can reach the service; the network is their boundary. If Caddy routed
    them, the public keys' source would be reachable (and spoofable by a request that reaches it) from outside."""
    text = _strip_comments(_caddy())
    # `health_uri /readyz` is Caddy ASKING the service; a routed path would appear in a `path` matcher.
    for line in text.splitlines():
        if re.match(r"\s*@\w+\s+path\b", line):
            assert "internal" not in line and "readyz" not in line and "healthz" not in line, line
    assert "authlimit:8080" not in text.replace("port 8080", ""), "the service is reached by its pool, never named in a route"


def test_the_upstream_behind_the_chat_api_check_is_a_variable_not_a_hard_coded_name():
    body = _strip_comments(_site("api.localhost"))

    assert body.count("{$AUTHLIMIT_UPSTREAM:authlimit-echo:8000}") == 3, "the public routes, the preflight and the checked routes share one upstream"
    assert "api:8000" not in body, "PR 2 points the variable at the API; the file does not change"


def test_caddys_public_paths_are_exactly_paths_the_services_table_calls_public():
    """Public in one place and verified in the other resolves to verified, but a public path Caddy knows and the service does not is a
    route that is NOT public at the service and so is unchecked at neither only by luck. Keep the two lists the same."""
    table = json.loads((SERVICE / "routes.json").read_text())
    service_public = {r["pattern"] for r in table["routes"] if r["class"] == "public" and r["method"] in ("GET", "*")}
    match = re.search(r"@public_path\s*\{\s*path\s+(.+)", _strip_comments(_site("api.localhost")))
    caddy_public = set(match.group(1).split())

    assert caddy_public == service_public, f"Caddy {sorted(caddy_public)} vs the service's table {sorted(service_public)}"


def test_a_cors_preflight_is_public_in_both_places():
    table = json.loads((SERVICE / "routes.json").read_text())
    options = [r for r in table["routes"] if r["method"] == "OPTIONS"]

    assert options and options[0]["class"] == "public"
    assert "@preflight method OPTIONS" in _site("api.localhost")


def test_the_authlimit_pool_has_a_retry_window_and_active_health_checks():
    """Measured (specs/018 research R1): with one dead replica, 20 of 20 requests succeed with a retry window and 7-9 of 20 without."""
    pool = _caddy().split("(authlimit_pool) {")[1].split("\n}\n")[0]
    pool = _strip_comments(pool)

    assert "dynamic a" in pool and "name authlimit" in pool and "port 8080" in pool
    assert "lb_try_duration 5s" in pool and "lb_try_interval 250ms" in pool
    assert "health_uri /readyz" in pool
    assert "fail_duration" in pool and "max_fails 1" in pool and "dial_timeout 1s" in pool, (
        "found by the smoke check: without passive health and a short dial timeout, every request that picks a dead replica waits out the dial timeout"
    )


# --- the Makefile ----------------------------------------------------------------------------------------------------------------------


def test_the_gateway_stack_target_starts_authlimit_and_makes_sure_a_key_exists_first():
    makefile = (REPO / "Makefile").read_text()
    up = re.search(r"^mcp-gateway-up:([^\n]*)\n\t([^\n]*)", makefile, re.MULTILINE)

    assert up and "authlimit-keys-ensure" in up.group(1)
    assert "authlimit authlimit-echo caddy-gateway" in up.group(2)
    assert "AUTHLIMIT_KEYS_DIR=$(AUTHLIMIT_KEYS_DIR)" in makefile, "compose must be told where the host's key directory is"
    assert "AUTHLIMIT_DIR      := $(HOME)/.cache/agent-core-demo-authlimit" in makefile
    phony = makefile.split("\n", 1)[0].removeprefix(".PHONY:").split()
    for target in (
        "authlimit-test", "authlimit-build", "authlimit-keys", "authlimit-up", "authlimit-up-app", "authlimit-down", "authlimit-down-app",
        "authlimit-smoke", "authlimit-revoke",
    ):
        assert re.search(rf"^{target}:", makefile, re.MULTILINE), target
        assert target in phony, f"{target} must be in .PHONY (as a whole word: authlimit-up is a prefix of authlimit-up-app)"


def test_keys_are_created_as_the_host_user_by_the_binary_itself_never_by_a_shell_pipeline():
    makefile = (REPO / "Makefile").read_text()
    recipe = re.search(r"^authlimit-keys:[^\n]*\n(?:\t[^\n]*\n)+", makefile, re.MULTILINE).group(0)

    assert '--user "$$(id -u):$$(id -g)"' in recipe
    assert "openssl" not in recipe, "the key is generated by authlimit (Ed25519, mode 600), not assembled in shell"


def test_caddy_only_declares_a_plain_url_public_so_authlimits_stricter_reading_always_applies_to_the_rest():
    """Found by the smoke check on the real stack: a path Caddy declares public goes straight to the upstream and authlimit never sees it, so
    `/static/a%5c..%5cchat/resume` matched `/static/*` and was proxied with no token. The guard keeps such URLs out of the public handler."""
    body = _strip_comments(_site("api.localhost"))
    matcher = re.search(r"@public_path \{(.*?)\n\t\}", body, re.DOTALL).group(1)  # up to the block's own closing brace, not the placeholder's

    assert "not expression" in matcher and "{http.request.uri}" in matcher
    for needle in ("%", r"\\", r"\.\.", "//"):
        assert needle in matcher, f"the guard must refuse {needle!r} in a public URL"


# --- the real API behind the door (specs/018, task T061) --------------------------------------------------------------------------------


def _makefile_recipe(target: str) -> str:
    """The text of one Makefile target: its line and the tab-indented recipe lines under it."""
    lines = (REPO / "Makefile").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    body = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        body.append(line)
    return "\n".join(body)


def test_the_compose_api_is_header_mode_unless_the_app_stack_target_says_otherwise():
    """`--profile app` alone publishes the API on :8000 with nothing in front of it, so it must keep working there (header mode, announced
    loudly at startup); only the target that puts the door in front of it flips the mode."""
    env = _services()["api"]["environment"]

    assert env["AUTH_MODE"] == "${AUTH_MODE:-headers}"


def test_the_compose_api_reaches_the_doors_published_keys_by_the_services_in_network_name():
    from types import SimpleNamespace

    from app.core.internal_identity import check_settings

    default = _services()["api"]["environment"]["AUTHLIMIT_KEYS_URL"]
    address = re.fullmatch(r"\$\{AUTHLIMIT_KEYS_URL:-(.+)\}", default)

    assert address, default
    url = address.group(1)
    assert url == "http://authlimit:8080/internal/jwks.json", "the service name, its listen port, and the path the service publishes"
    check_settings(SimpleNamespace(auth_mode="identity", authlimit_keys_url=url))  # the startup check accepts it: plain http to a dotless name


def test_the_api_key_timings_are_passed_through_with_the_defaults_the_settings_have():
    """Passed through so the smoke can shorten them; and a default written twice must be the same twice."""
    from app.core.config import Settings

    env = _services()["api"]["environment"]

    assert env["AUTHLIMIT_KEYS_CACHE_SECONDS"] == f"${{AUTHLIMIT_KEYS_CACHE_SECONDS:-{Settings.model_fields['authlimit_keys_cache_seconds'].default}}}"
    assert env["AUTHLIMIT_KEYS_STALE_GRACE_SECONDS"] == f"${{AUTHLIMIT_KEYS_STALE_GRACE_SECONDS:-{Settings.model_fields['authlimit_keys_stale_grace_seconds'].default}}}"


def test_the_api_does_not_depend_on_the_door_so_the_app_profile_alone_still_starts():
    """A dependency on a service in the `gateway` profile would make `--profile app` fail to start without it."""
    assert "authlimit" not in _services()["api"]["depends_on"]


def test_the_workers_are_not_told_an_auth_mode_because_only_the_api_has_a_caller_to_check():
    assert "AUTH_MODE" not in _services()["agent-worker"]["environment"]


def test_the_app_stack_target_puts_the_real_api_behind_the_door_in_identity_mode():
    recipe = _makefile_recipe("authlimit-up-app")

    assert "AUTH_MODE=identity" in recipe
    assert "AUTHLIMIT_UPSTREAM=api:8000" in recipe
    assert "--profile app" in recipe and "--profile gateway" in recipe.replace("$(GATEWAY_COMPOSE)", "--profile gateway")
    for service in ("api", "agent-worker", "caddy-gateway", "authlimit", "casdoor"):
        assert f" {service}" in recipe, service
    assert "authlimit-keys-ensure" in recipe.splitlines()[0], "a signing key must exist before the door starts"


def test_the_stub_stays_the_default_upstream_because_the_smoke_reads_what_an_api_would_have_been_given():
    assert _services()["caddy-gateway"]["environment"]["AUTHLIMIT_UPSTREAM"] == "${AUTHLIMIT_UPSTREAM:-authlimit-echo:8000}"
    assert "AUTHLIMIT_UPSTREAM" not in _makefile_recipe("authlimit-up"), "the plain stack target keeps the stub"


def test_taking_the_app_stack_down_removes_the_api_it_started_so_header_mode_is_what_comes_back():
    recipe = _makefile_recipe("authlimit-down-app")

    assert "--profile app" in recipe and " api" in recipe and " agent-worker" in recipe and " caddy-gateway" in recipe

"""scripts/mcp_gateway_bootstrap.py — what it asks Casdoor to create (specs/017, extended by specs/018).

Hermetic: `Casdoor` is replaced by a recorder, so these pin WHAT the script asks for (the applications, their scopes, redirect URIs and token
lifetime), not Casdoor's behaviour; that is measured against the real image by `make mcp-gateway-smoke` and `make authlimit-smoke`.

The decision under test (spec 018, FR-022): one `--token-hours` setting, default seven days, for EVERY application the script creates, MCP
ones included. Verification is offline, so the lifetime is the window a stolen token works in; revocation (`make authlimit-revoke`) is the
bound, and a test that the default is 168 is a test that nobody quietly changes it back to one hour or forward to a year.
"""
import pytest

from scripts import mcp_gateway_bootstrap as bootstrap


class FakeCasdoor:
    """Records what is ensured; returns a client id derived from the application name."""

    instances: list["FakeCasdoor"] = []

    def __init__(self, base_url: str, admin_password: str) -> None:
        self.organizations: list[str] = []
        self.applications: list[dict] = []
        self.users: list[tuple[str, str]] = []
        FakeCasdoor.instances.append(self)

    def ensure_organization(self, name: str) -> None:
        self.organizations.append(name)

    def ensure_user(self, organization: str, name: str) -> str | None:
        self.users.append((organization, name))
        return "generated-password"

    def ensure_application(self, name, organization, scope, description, redirect_uris, token_hours, kind="MCP") -> str:
        self.applications.append(
            {"name": name, "organization": organization, "scope": scope, "redirect_uris": redirect_uris, "token_hours": token_hours, "kind": kind}
        )
        return f"client-{name}"


@pytest.fixture
def casdoor(monkeypatch):
    FakeCasdoor.instances = []
    monkeypatch.setattr(bootstrap, "Casdoor", FakeCasdoor)
    monkeypatch.setenv("CASDOOR_ADMIN_PASSWORD", "not-a-real-password")
    for var in ("TOKEN_HOURS", "AUTHLIMIT_AUDIENCE", "MCP_GATEWAY_PUBLIC_URL", "MCP_GATEWAY_OPS_TENANTS"):
        monkeypatch.delenv(var, raising=False)
    return lambda argv=(): (bootstrap.main(list(argv)), FakeCasdoor.instances[-1])[1]


def by_name(fake: FakeCasdoor) -> dict[str, dict]:
    return {a["name"]: a for a in fake.applications}


def test_every_application_lasts_seven_days_by_default(casdoor):
    fake = casdoor()

    apps = by_name(fake)
    assert set(apps) == {"mcp-ecorp", "api-ecorp", "mcp-ops"}
    assert {a["token_hours"] for a in apps.values()} == {168}, "the default is seven days for the MCP applications too (spec 018 FR-022)"
    assert bootstrap.DEFAULT_TOKEN_HOURS == 168


def test_the_token_hours_option_and_the_environment_change_it_for_every_application(casdoor, monkeypatch):
    assert {a["token_hours"] for a in casdoor(["--token-hours", "24"]).applications} == {24}

    monkeypatch.setenv("TOKEN_HOURS", "48")
    assert {a["token_hours"] for a in casdoor().applications} == {48}, "TOKEN_HOURS sets the default"
    assert {a["token_hours"] for a in casdoor(["--token-hours", "2"]).applications} == {2}, "and the option beats it"


@pytest.mark.parametrize("hours", ["0", "-1", "8761", "100000"])
def test_an_absurd_token_lifetime_is_refused_before_anything_is_created(casdoor, hours):
    with pytest.raises(SystemExit) as stop:
        casdoor(["--token-hours", hours])

    assert "between 1 and 8760" in str(stop.value)
    assert FakeCasdoor.instances == [], "nothing may be created when the arguments are wrong"


def test_the_chat_api_gets_one_public_client_per_tenant_with_its_own_scope(casdoor):
    fake = casdoor(["--tenant", "ecorp", "--tenant", "other-co"])

    apps = by_name(fake)
    assert {"api-ecorp", "api-other-co"} <= set(apps)
    api = apps["api-ecorp"]
    assert api["organization"] == "ecorp"
    assert api["scope"] == "api:chat"
    assert api["kind"] == "API"
    # The page signs in at its root URL; the smoke check at its own callback. Casdoor compares redirect URIs exactly.
    assert api["redirect_uris"] == ["https://api.localhost:8443/", bootstrap.API_SMOKE_CALLBACK]
    assert apps["api-other-co"]["organization"] == "other-co", "the application belongs to the tenant's organization: that is what puts `owner` in the token"


def test_the_operators_organization_gets_no_chat_api_application(casdoor):
    # The operations team uses the MCP operations server only; a chat client for them would be an unrequested way in.
    assert not [a for a in casdoor().applications if a["organization"] == "ops-team" and a["name"].startswith("api-")]


def test_the_api_audience_follows_the_environment(casdoor, monkeypatch):
    monkeypatch.setenv("AUTHLIMIT_AUDIENCE", "https://chat.example.com/")

    api = by_name(casdoor())["api-ecorp"]

    assert api["redirect_uris"][0] == "https://chat.example.com/", "the trailing slash of the resource identifier is dropped, the page URL ends with one"


def test_the_mcp_applications_keep_their_fixed_callback_and_scopes(casdoor):
    apps = by_name(casdoor(["--callback-port", "40000"]))

    assert apps["mcp-ecorp"]["redirect_uris"] == ["http://localhost:40000/callback"]
    assert apps["mcp-ecorp"]["scope"] == "ecorp:read"
    assert apps["mcp-ops"]["scope"] == "ops:read"


def test_the_output_names_each_clients_id_and_the_lifetime(casdoor, capsys):
    casdoor(["--token-hours", "12"])

    out = capsys.readouterr().out
    assert "client-api-ecorp" in out
    assert "Access tokens last 12 hours" in out
    assert "make authlimit-revoke" in out


def test_without_the_admin_password_nothing_is_attempted(monkeypatch):
    FakeCasdoor.instances = []
    monkeypatch.setattr(bootstrap, "Casdoor", FakeCasdoor)
    monkeypatch.delenv("CASDOOR_ADMIN_PASSWORD", raising=False)

    with pytest.raises(SystemExit):
        bootstrap.main([])

    assert FakeCasdoor.instances == []


# --- the record the REAL class sends --------------------------------------------------------------------------------------------------


class RecordingCasdoor(bootstrap.Casdoor):
    """The real `ensure_application`, with the two HTTP primitives replaced."""

    def __init__(self, existing: dict | None = None) -> None:  # noqa: D107 - the base __init__ logs in over HTTP; this must not
        self.existing = existing
        self.writes: list[tuple[str, dict]] = []

    def read(self, path: str, **params: str):
        return self.existing

    def write(self, path: str, body: dict, **params: str) -> None:
        self.writes.append((path, body))


def test_the_real_application_record_carries_the_token_lifetime_the_format_and_the_scope():
    casdoor = RecordingCasdoor()

    client_id = casdoor.ensure_application("api-ecorp", "ecorp", "api:chat", "chat API for ecorp", ["https://api.localhost:8443/"], 168, kind="API")

    path, body = casdoor.writes[0]
    assert path == "/api/add-application"
    assert body["expireInHours"] == 168
    assert body["refreshExpireInHours"] == bootstrap.REFRESH_HOURS
    assert body["tokenFormat"] == "JWT-Standard", "the default format carries ~85 claims, among them password-related fields"
    assert (body["category"], body["type"]) == ("Agent", "MCP"), "this is what turns custom scopes on (spec 017 research)"
    assert body["redirectUris"] == ["https://api.localhost:8443/"]
    assert body["scopes"][0]["name"] == "api:chat"
    assert body["displayName"] == "API: chat API for ecorp"
    assert body["clientId"] == client_id
    assert "authorization_code" in body["grantTypes"]


def test_a_rerun_keeps_the_client_id_and_the_secret_and_applies_the_new_lifetime():
    existing = {"clientId": "kept-id", "clientSecret": "kept-secret", "expireInHours": 1, "name": "api-ecorp", "owner": "admin"}
    casdoor = RecordingCasdoor(existing)

    client_id = casdoor.ensure_application("api-ecorp", "ecorp", "api:chat", "chat API for ecorp", ["https://api.localhost:8443/"], 168, kind="API")

    path, body = casdoor.writes[0]
    assert path == "/api/update-application"
    assert client_id == "kept-id", "a client already registered with the id must keep working"
    assert body["clientId"] == "kept-id" and body["clientSecret"] == "kept-secret"
    assert body["expireInHours"] == 168, "changing the setting and re-running updates the lifetime"

"""`make telegram-person` (scripts/tools/telegram_person.py): how an operator adds, disables and re-enables a Telegram person without the bot ever holding a
credential that can (specs/020). The authorization server is a fake; what is pinned is what the tool WRITES: the derived password, never printed, a
person that is not an administrator, and a disable that says how to void the token already issued."""
import pytest

from app.adapters.inbound.channels.telegram_people import person_password
from app.core.config import settings
from scripts.tools import telegram_person

SECRET = "k" * 40


class FakeCasdoor:
    instances: list["FakeCasdoor"] = []

    def __init__(self, base_url, admin_password):
        self.base_url, self.admin_password = base_url, admin_password
        self.users: dict[str, dict] = {}
        self.written: list[tuple[str, dict, dict]] = []
        self.organizations: list[str] = []
        FakeCasdoor.instances.append(self)

    def ensure_organization(self, name):
        self.organizations.append(name)

    def read(self, path, **params):
        assert path == "/api/get-user"
        return self.users.get(params["id"])

    def write(self, path, body, **params):
        self.written.append((path, body, params))
        if path == "/api/add-user":
            self.users[f"{body['owner']}/{body['name']}"] = {**body, "id": f"uuid-{body['name']}"}
        if path == "/api/update-user":
            self.users[params["id"]] = body


@pytest.fixture(autouse=True)
def operator_environment(monkeypatch):
    FakeCasdoor.instances = []
    FakeCasdoor.shared = None
    monkeypatch.setattr(telegram_person, "Casdoor", FakeCasdoor)
    monkeypatch.setattr(settings, "telegram_person_secret", SECRET)
    monkeypatch.setattr(settings, "telegram_tenant", "ecorp")
    monkeypatch.setattr(settings, "web_auth_issuer", "https://auth.test")
    monkeypatch.setenv("CASDOOR_ADMIN_PASSWORD", "admin-pw")


def run(*argv):
    telegram_person.main(list(argv))
    return FakeCasdoor.instances[-1]


def test_add_writes_a_person_with_the_derived_password_who_is_not_an_administrator(capsys):
    fake = run("add", "--telegram-id", "4242")

    (path, body, _), = fake.written
    assert path == "/api/add-user"
    assert (body["owner"], body["name"], body["isAdmin"], body["isForbidden"]) == ("ecorp", "telegram-4242", False, False)
    assert body["password"] == person_password(SECRET, "ecorp", 4242), "the same value the bot derives, so it can sign in"
    assert fake.base_url == "https://auth.test" and fake.admin_password == "admin-pw"
    out = capsys.readouterr().out
    assert "added ecorp/telegram-4242" in out
    assert body["password"] not in out, "the password is never printed"


def test_add_for_a_person_that_exists_changes_nothing(monkeypatch, capsys):
    class Existing(FakeCasdoor):
        def read(self, path, **params):
            return {"id": "uuid-x", "name": "telegram-4242"}

    monkeypatch.setattr(telegram_person, "Casdoor", Existing)

    fake = run("add", "--telegram-id", "4242")

    assert fake.written == []
    assert "already exists" in capsys.readouterr().out


def test_disable_forbids_the_person_and_prints_the_line_that_voids_a_token_already_issued(monkeypatch, capsys):
    class Existing(FakeCasdoor):
        def read(self, path, **params):
            return {"owner": "ecorp", "name": "telegram-4242", "id": "the-subject", "isForbidden": False}

    monkeypatch.setattr(telegram_person, "Casdoor", Existing)

    fake = run("disable", "--telegram-id", "4242")

    (path, body, params), = fake.written
    assert (path, body["isForbidden"], params["id"]) == ("/api/update-user", True, "ecorp/telegram-4242")
    assert "make authlimit-revoke TENANT=ecorp SUB=the-subject" in capsys.readouterr().out


def test_enable_lifts_it_and_has_nothing_to_revoke(monkeypatch, capsys):
    class Existing(FakeCasdoor):
        def read(self, path, **params):
            return {"owner": "ecorp", "name": "telegram-4242", "id": "the-subject", "isForbidden": True}

    monkeypatch.setattr(telegram_person, "Casdoor", Existing)

    fake = run("enable", "--telegram-id", "4242")

    assert fake.written[0][1]["isForbidden"] is False
    assert "authlimit-revoke" not in capsys.readouterr().out


def test_disabling_a_person_who_does_not_exist_says_so_and_writes_nothing():
    with pytest.raises(SystemExit, match="does not exist"):
        run("disable", "--telegram-id", "4242")

    assert FakeCasdoor.instances[-1].written == []


@pytest.mark.parametrize(("secret", "password", "needle"), [("short", "admin-pw", "TELEGRAM_PERSON_SECRET"), (SECRET, "", "CASDOOR_ADMIN_PASSWORD")])
def test_it_refuses_to_run_without_what_it_needs_and_touches_nothing(monkeypatch, secret, password, needle):
    monkeypatch.setattr(settings, "telegram_person_secret", secret)
    monkeypatch.setenv("CASDOOR_ADMIN_PASSWORD", password)

    with pytest.raises(SystemExit, match=needle):
        telegram_person.main(["add", "--telegram-id", "1"])

    assert FakeCasdoor.instances == []

"""Telegram people (app/channels/telegram_people.py): every Telegram user is a distinct person the front door can prove, meter and cut off (specs/020).

The properties that matter here are the ones whose failure is silent: that two users never share a person, that a restart gives the same
people, that a DISABLED person is never re-created or re-enabled by the bot, that two simultaneous first messages make ONE person, and that the
bot's default holds no credential able to create anyone. The authorization server is the fake of tests/channels/fake_casdoor.py.
"""
import asyncio

import pytest

from app.channels import oauth, telegram_people
from app.channels.oauth import SignInRefused
from app.channels.telegram_people import (
    PersonAuth,
    TelegramPeople,
    person_name,
    person_password,
)
from app.core import metrics
from app.core.config import settings
from tests.channels.fake_casdoor import AUDIENCE, ISSUER, SCOPE, FakeCasdoor
from tests.conftest import metric_value as _count

SECRET = "s" * 48
TENANT = "ecorp"


@pytest.fixture(autouse=True)
def the_local_stack(monkeypatch):
    monkeypatch.setattr(settings, "web_auth_issuer", ISSUER)
    monkeypatch.setattr(settings, "web_auth_audience", AUDIENCE)
    monkeypatch.setattr(settings, "web_auth_scope", SCOPE)
    monkeypatch.setattr(settings, "telegram_person_secret", SECRET)
    monkeypatch.setattr(settings, "telegram_tenant", TENANT)
    monkeypatch.setattr(settings, "telegram_auto_provision", False)
    monkeypatch.setattr(settings, "telegram_provision_client_id", "provisioner")
    monkeypatch.setattr(settings, "telegram_provision_client_secret", "provisioner-secret")


@pytest.fixture
def casdoor():
    return FakeCasdoor()


def operator_adds(casdoor, user_id: int, **kwargs) -> str:
    """What `make telegram-person ACTION=add` does."""
    return casdoor.add_person(TENANT, person_name(user_id), person_password(SECRET, TENANT, user_id), **kwargs)


class TestThePasswordIsDerivedNotStored:
    def test_the_same_user_always_gets_the_same_password(self):
        assert person_password(SECRET, TENANT, 7) == person_password(SECRET, TENANT, 7)

    def test_two_users_never_share_one(self):
        assert person_password(SECRET, TENANT, 7) != person_password(SECRET, TENANT, 8)

    def test_another_secret_or_another_tenant_is_another_password(self):
        assert person_password(SECRET, TENANT, 7) != person_password("t" * 48, TENANT, 7)
        assert person_password(SECRET, TENANT, 7) != person_password(SECRET, "other-co", 7)

    def test_it_carries_256_bits_in_url_safe_characters(self):
        password = person_password(SECRET, TENANT, 7)

        assert len(password) == 43
        assert set(password) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

    def test_the_tenant_and_id_cannot_be_shifted_into_each_other(self):
        """('ab', 1) and ('a', b1...) must not collide: the fields are NUL separated."""
        assert person_password(SECRET, "a1", 2) != person_password(SECRET, "a", 12)

    def test_the_person_is_named_by_the_telegram_id(self):
        assert person_name(123456789) == "telegram-123456789"


class TestCheckSettings:
    def test_identity_mode_refuses_to_start_without_a_real_secret(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "identity")
        monkeypatch.setattr(settings, "telegram_person_secret", "short")

        with pytest.raises(SystemExit, match="TELEGRAM_PERSON_SECRET"):
            telegram_people.check_settings()

    def test_header_mode_needs_no_secret(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "headers")
        monkeypatch.setattr(settings, "telegram_person_secret", "")

        telegram_people.check_settings()

    def test_auto_provision_without_a_credential_to_do_it_with_refuses_to_start(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "identity")
        monkeypatch.setattr(settings, "telegram_auto_provision", True)
        monkeypatch.setattr(settings, "telegram_provision_client_secret", "")

        with pytest.raises(SystemExit, match="TELEGRAM_PROVISION"):
            telegram_people.check_settings()


class TestSigningAPersonIn:
    async def test_an_operator_added_person_gets_a_token_naming_them(self, casdoor):
        person_id = operator_adds(casdoor, 7)
        people = TelegramPeople(casdoor.client())

        tokens = await people.sign_in(7)

        claims = oauth.jwt_claims(tokens.access_token)
        assert (claims["sub"], claims["owner"], claims["name"]) == (person_id, TENANT, "telegram-7")

    async def test_two_users_are_two_people(self, casdoor):
        one, two = operator_adds(casdoor, 1), operator_adds(casdoor, 2)
        people = TelegramPeople(casdoor.client())

        sub_one, sub_two = await people.person(1).sign_in(), await people.person(2).sign_in()

        assert (sub_one, sub_two) == (one, two) and one != two

    async def test_a_restart_gives_the_same_person(self, casdoor):
        """No per-person secret is stored, so a fresh process derives the same password and signs in as the same person."""
        person_id = operator_adds(casdoor, 7)

        before = await TelegramPeople(casdoor.client()).person(7).sign_in()
        after = await TelegramPeople(casdoor.client()).person(7).sign_in()

        assert before == after == person_id

    async def test_the_sign_in_is_kept_until_it_is_about_to_expire(self, casdoor):
        operator_adds(casdoor, 7)
        auth = TelegramPeople(casdoor.client()).person(7)

        first, second = await auth.headers(), await auth.headers()

        assert first == second and casdoor.logins == 1

    async def test_after_a_401_the_person_signs_in_again(self, casdoor):
        operator_adds(casdoor, 7)
        auth = TelegramPeople(casdoor.client()).person(7)
        await auth.headers()

        assert await auth.reauthenticate() is True
        assert casdoor.logins == 2

    async def test_the_same_person_object_is_reused_per_user(self, casdoor):
        people = TelegramPeople(casdoor.client())

        assert people.person(7) is people.person(7)
        assert isinstance(people.person(7), PersonAuth)


class TestWhoMayCreateAPerson:
    async def test_by_default_a_user_nobody_added_is_refused_and_the_bot_creates_nothing(self, casdoor):
        """The bot holds a password-derivation secret and NO credential that can create a person: this is the default (MEASURED: an application's
        credentials are an administrator's of the whole organization)."""
        people = TelegramPeople(casdoor.client())

        with pytest.raises(SignInRefused) as exc:
            await people.sign_in(7)

        assert exc.value.reason == "unknown_person"
        assert casdoor.add_user_calls == []
        assert ("POST", "/api/add-user") not in casdoor.requests

    async def test_with_auto_provision_a_first_message_creates_the_person_once_and_signs_them_in(self, casdoor, monkeypatch):
        monkeypatch.setattr(settings, "telegram_auto_provision", True)
        created = _count(metrics.channel_person_provisioned_total, outcome="created")
        people = TelegramPeople(casdoor.client())

        tokens = await people.sign_in(7)

        (call,) = casdoor.add_user_calls
        assert (call["owner"], call["name"], call["isAdmin"], call["isForbidden"]) == (TENANT, "telegram-7", False, False)
        assert call["password"] == person_password(SECRET, TENANT, 7)
        assert oauth.jwt_claims(tokens.access_token)["name"] == "telegram-7"
        assert _count(metrics.channel_person_provisioned_total, outcome="created") == created + 1

    async def test_ten_simultaneous_first_messages_from_one_user_make_one_person(self, casdoor, monkeypatch):
        monkeypatch.setattr(settings, "telegram_auto_provision", True)
        people = TelegramPeople(casdoor.client())

        results = await asyncio.gather(*(people.sign_in(7) for _ in range(10)))

        assert len(casdoor.add_user_calls) == 1
        assert len({oauth.jwt_claims(t.access_token)["sub"] for t in results}) == 1

    async def test_two_different_new_users_make_two_people(self, casdoor, monkeypatch):
        monkeypatch.setattr(settings, "telegram_auto_provision", True)
        people = TelegramPeople(casdoor.client())

        await asyncio.gather(people.sign_in(1), people.sign_in(2))

        assert sorted(c["name"] for c in casdoor.add_user_calls) == ["telegram-1", "telegram-2"]

    async def test_a_creation_that_fails_is_a_refusal_and_is_counted(self, casdoor, monkeypatch):
        monkeypatch.setattr(settings, "telegram_auto_provision", True)
        monkeypatch.setattr(settings, "telegram_provision_client_secret", "the wrong secret")
        failed = _count(metrics.channel_person_provisioned_total, outcome="failed")
        people = TelegramPeople(casdoor.client())

        with pytest.raises(SignInRefused) as exc:
            await people.sign_in(7)

        assert exc.value.reason == "credentials"
        assert ("ecorp", "telegram-7") not in casdoor.people
        assert _count(metrics.channel_person_provisioned_total, outcome="failed") == failed + 1


class TestDisabledIsFinal:
    @pytest.mark.parametrize("auto", [False, True], ids=["default", "auto-provision"])
    async def test_a_disabled_person_is_refused_and_never_re_created_or_re_enabled(self, casdoor, monkeypatch, auto):
        monkeypatch.setattr(settings, "telegram_auto_provision", auto)
        operator_adds(casdoor, 7, forbidden=True)
        people = TelegramPeople(casdoor.client())

        with pytest.raises(SignInRefused) as exc:
            await people.sign_in(7)

        assert exc.value.reason == "forbidden"
        assert casdoor.add_user_calls == []
        assert casdoor.people[(TENANT, "telegram-7")]["forbidden"] is True

    async def test_disabling_a_person_stops_the_bots_next_sign_in(self, casdoor):
        operator_adds(casdoor, 7)
        auth = TelegramPeople(casdoor.client()).person(7)
        await auth.sign_in()
        casdoor.people[(TENANT, "telegram-7")]["forbidden"] = True

        assert await auth.reauthenticate() is False


class TestAWrongPasswordIsNotRetried:
    async def test_it_is_tried_once_because_five_wrong_ones_lock_the_person(self, casdoor):
        casdoor.add_person(TENANT, "telegram-7", "a password the bot does not derive")
        people = TelegramPeople(casdoor.client())

        with pytest.raises(SignInRefused) as exc:
            await people.sign_in(7)

        assert exc.value.reason == "credentials"
        assert casdoor.logins == 1


class TestEveryOutcomeIsCounted:
    async def test_ok_and_each_refusal_are_counted_per_channel(self, casdoor):
        operator_adds(casdoor, 1)
        operator_adds(casdoor, 2, forbidden=True)
        counts = {o: _count(metrics.channel_signin_total, channel="telegram", outcome=o) for o in ("ok", "forbidden", "unknown_person")}
        people = TelegramPeople(casdoor.client())

        await people.sign_in(1)
        for user_id in (2, 3):
            with pytest.raises(SignInRefused):
                await people.sign_in(user_id)

        for outcome in counts:
            assert _count(metrics.channel_signin_total, channel="telegram", outcome=outcome) == counts[outcome] + 1

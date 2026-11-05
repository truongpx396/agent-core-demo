"""Telegram account linking (app/adapters/inbound/channels/telegram/link.py, specs/021 FR-008). The authorization server is replaced by two functions of
`app.adapters.inbound.channels.oauth` (the code exchange and the refresh), whose real behaviour is covered in test_oauth.py and, for Casdoor, in specs/020 research."""
import asyncio
import base64
import json
import time

import pytest
from cryptography.fernet import Fernet

from app.adapters.inbound.channels import oauth
from app.adapters.inbound.channels.oauth import SignInRefused, Tokens
from app.adapters.inbound.channels.telegram import link as telegram_link
from app.adapters.inbound.channels.telegram.link import (
    LinkedAuth,
    LinkFlow,
    LinkStore,
    ToConfirm,
)
from app.core import metrics
from app.core.config import settings
from tests.adapters.inbound.channels.telegram.fakes import FakeApi, FakeRedis
from tests.conftest import metric_value as _count

KEY = Fernet.generate_key().decode()


def jwt(**claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"h.{body}.s"


def tokens(*, sub="u-1", owner="acme", refresh="r-1", name="Ann Lee", lifetime=3600):
    return Tokens(jwt(sub=sub, owner=owner, name=name), refresh, time.time() + lifetime)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "telegram_tenant", "acme")
    monkeypatch.setattr(settings, "telegram_link_public_url", "https://bot.example.com")
    monkeypatch.setattr(settings, "telegram_link_key", KEY)
    monkeypatch.setattr(settings, "web_auth_issuer", "https://auth.example.com")
    monkeypatch.setattr(settings, "web_auth_audience", "https://api.example.com")
    monkeypatch.setattr(settings, "web_auth_scope", "api:chat")
    monkeypatch.setattr(settings, "telegram_link_state_ttl_seconds", 600)


def make_flow(redis=None):
    redis = redis or FakeRedis()
    api = FakeApi()
    return LinkFlow(LinkStore(redis, KEY), None, api), api, redis  # type: ignore[arg-type]  # the http client is only used by the patched oauth calls


def patch_exchange(monkeypatch, result):
    calls = []

    async def exchange(http, issuer, **kw):
        calls.append(kw)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(oauth, "exchange_code", exchange)
    return calls


class TestTheStore:
    async def test_a_state_works_once_and_only_once(self):
        store = LinkStore(FakeRedis(), KEY)
        state, challenge = await store.begin(42, 1, "Ann")

        first = await store.take_state(state)
        second = await store.take_state(state)

        assert first["user_id"] == 42 and first["chat_id"] == 1 and challenge
        assert second is None

    async def test_a_state_nobody_made_is_nothing(self):
        assert await LinkStore(FakeRedis(), KEY).take_state("guess") is None

    async def test_a_state_lives_only_as_long_as_configured(self):
        redis = FakeRedis()
        await LinkStore(redis, KEY).begin(42, 1, "Ann")

        assert set(redis.ttls.values()) == {600}

    async def test_what_is_kept_is_sealed_the_verifier_and_the_refresh_token_are_not_readable_in_redis(self):
        redis = FakeRedis()
        store = LinkStore(redis, KEY)
        await store.begin(42, 1, "Ann")
        await store.save(42, refresh_token="refresh-secret-123", sub="u-1", account="ann")

        stored = " ".join(redis.kv.values())

        assert "refresh-secret-123" not in stored and "verifier" not in stored and "u-1" not in stored

    async def test_a_record_altered_or_sealed_with_another_key_is_as_good_as_absent(self):
        redis = FakeRedis()
        store = LinkStore(redis, KEY)
        await store.save(42, refresh_token="r", sub="u", account="a")
        key = next(iter(redis.kv))

        assert await LinkStore(redis, Fernet.generate_key().decode()).load(42) is None
        redis.kv[key] = redis.kv[key][:-4] + "AAAA"
        assert await store.load(42) is None

    async def test_links_of_different_tenants_do_not_collide(self, monkeypatch):
        redis = FakeRedis()
        store = LinkStore(redis, KEY)
        await store.save(42, refresh_token="r", sub="u", account="a")

        monkeypatch.setattr(settings, "telegram_tenant", "other")

        assert await store.load(42) is None

    async def test_deleting_says_whether_there_was_a_link(self):
        store = LinkStore(FakeRedis(), KEY)
        await store.save(42, refresh_token="r", sub="u", account="a")

        assert [await store.delete(42), await store.delete(42)] == [True, False]


class TestLogin:
    async def test_the_person_gets_one_button_to_the_authorization_servers_own_page_bound_to_their_state(self):
        flow, api, redis = make_flow()

        await flow.login(1, 42, {"id": 42, "first_name": "Ann", "last_name": "Lee", "username": "ann"})

        (call,) = api.of("send")
        url = call[3]["inline_keyboard"][0][0]["url"]
        assert url.startswith("https://auth.example.com/login/oauth/authorize?")
        for expected in ("client_id=api-acme", "response_type=code", "code_challenge_method=S256", "resource=https%3A%2F%2Fapi.example.com",
                         "redirect_uri=https%3A%2F%2Fbot.example.com%2Ftelegram%2Flink%2Fcallback"):
            assert expected in url
        state = url.split("state=")[1].split("&")[0]
        record = await LinkStore(redis, KEY).take_state(state)
        assert (record["user_id"], record["chat_id"], record["name"]) == (42, 1, "Ann Lee (@ann)")

    async def test_the_password_is_never_asked_for_here(self):
        flow, api, _ = make_flow()

        await flow.login(1, 42, {"id": 42})

        assert "password" not in api.sent_texts()[0].lower()


class TestTheReturnFromTheLoginPage:
    async def begin(self, flow):
        await flow.login(1, 42, {"id": 42, "first_name": "Ann"})
        return flow._api.of("send")[-1][3]["inline_keyboard"][0][0]["url"].split("state=")[1].split("&")[0]

    async def test_a_good_login_asks_for_confirmation_and_binds_nothing_yet(self, monkeypatch):
        flow, _, redis = make_flow()
        state = await self.begin(flow)
        calls = patch_exchange(monkeypatch, tokens())

        ask = await flow.callback("the-code", state)

        assert isinstance(ask, ToConfirm) and (ask.telegram, ask.account) == ("Ann", "Ann Lee")
        assert calls[0]["code"] == "the-code" and calls[0]["verifier"] and calls[0]["redirect_uri"] == "https://bot.example.com/telegram/link/callback"
        assert await LinkStore(redis, KEY).load(42) is None  # the person has not said yes

    async def test_the_same_return_cannot_be_replayed(self, monkeypatch):
        flow, _, _ = make_flow()
        state = await self.begin(flow)
        patch_exchange(monkeypatch, tokens())
        await flow.callback("c", state)
        before = _count(metrics.channel_telegram_link_total, step="callback", outcome="refused")

        assert await flow.callback("c", state) is None
        assert _count(metrics.channel_telegram_link_total, step="callback", outcome="refused") == before + 1

    async def test_a_state_that_was_never_issued_is_refused_without_calling_the_authorization_server(self, monkeypatch):
        flow, _, _ = make_flow()
        calls = patch_exchange(monkeypatch, tokens())

        assert await flow.callback("c", "made-up") is None
        assert calls == []

    @pytest.mark.parametrize("reason", ["credentials", "unavailable"])
    async def test_a_code_the_authorization_server_would_not_exchange_binds_nothing(self, monkeypatch, reason):
        flow, _, _ = make_flow()
        state = await self.begin(flow)
        patch_exchange(monkeypatch, SignInRefused(reason))

        assert await flow.callback("c", state) is None

    async def test_an_account_of_another_tenant_is_not_this_bots_to_link(self, monkeypatch):
        """The tenant comes from the token, never from the callback's query (Principle I)."""
        flow, _, _ = make_flow()
        state = await self.begin(flow)
        patch_exchange(monkeypatch, tokens(owner="someone-elses-org"))

        assert await flow.callback("c", state) is None

    @pytest.mark.parametrize("bad", [tokens(refresh=None), Tokens(jwt(owner="acme"), "r", time.time() + 100)])
    async def test_a_login_that_cannot_be_kept_alive_or_names_nobody_is_refused(self, monkeypatch, bad):
        flow, _, _ = make_flow()
        state = await self.begin(flow)
        patch_exchange(monkeypatch, bad)

        assert await flow.callback("c", state) is None


class TestConfirming:
    async def confirm_token(self, flow, monkeypatch, **token_kw):
        await flow.login(1, 42, {"id": 42, "first_name": "Ann"})
        state = flow._api.of("send")[-1][3]["inline_keyboard"][0][0]["url"].split("state=")[1].split("&")[0]
        patch_exchange(monkeypatch, tokens(**token_kw))
        return (await flow.callback("c", state)).token

    async def test_yes_saves_the_link_and_tells_the_person_in_telegram(self, monkeypatch):
        flow, api, redis = make_flow()
        token = await self.confirm_token(flow, monkeypatch)
        before = _count(metrics.channel_telegram_link_total, step="confirm", outcome="ok")

        assert await flow.confirm(token) == "Ann Lee"

        record = await LinkStore(redis, KEY).load(42)
        assert (record["refresh_token"], record["sub"], record["account"]) == ("r-1", "u-1", "Ann Lee")
        assert "Connected to Ann Lee" in api.sent_texts()[-1] and api.of("send")[-1][1] == 1
        assert _count(metrics.channel_telegram_link_total, step="confirm", outcome="ok") == before + 1

    async def test_the_button_works_once(self, monkeypatch):
        flow, _, _ = make_flow()
        token = await self.confirm_token(flow, monkeypatch)
        await flow.confirm(token)

        assert await flow.confirm(token) is None

    async def test_a_token_nobody_issued_saves_nothing(self):
        flow, _, redis = make_flow()

        assert await flow.confirm("guess") is None
        assert await LinkStore(redis, KEY).load(42) is None

    async def test_unlinking_removes_the_link_and_says_so(self, monkeypatch):
        flow, api, redis = make_flow()
        await flow.confirm(await self.confirm_token(flow, monkeypatch))

        await flow.unlink(1, 42)
        await flow.unlink(1, 42)

        assert await LinkStore(redis, KEY).load(42) is None
        assert "Disconnected" in api.sent_texts()[-2] and "No account" in api.sent_texts()[-1]


class TestSigningInAsALinkedAccount:
    def auth(self, redis=None):
        redis = redis or FakeRedis()
        store = LinkStore(redis, KEY)
        return LinkedAuth(None, store, 42), store  # type: ignore[arg-type]  # the http client is only used by the patched oauth call

    def patch_refresh(self, monkeypatch, *results):
        calls = []
        queue = list(results)

        async def refresh(http, issuer, **kw):
            calls.append(kw)
            await asyncio.sleep(0.01)
            result = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(oauth, "refresh_tokens", refresh)
        return calls

    async def test_without_a_link_the_person_is_told_to_log_in(self):
        auth, _ = self.auth()

        with pytest.raises(SignInRefused) as refused:
            await auth.sign_in()

        assert refused.value.reason == "not_linked"

    async def test_the_account_is_the_one_in_the_fresh_token_not_the_one_that_was_stored(self, monkeypatch):
        """The stored `sub` is never trusted: what the front door sees is what the authorization server says today."""
        auth, store = self.auth()
        await store.save(42, refresh_token="r-1", sub="stored-sub", account="a")
        calls = self.patch_refresh(monkeypatch, tokens(sub="fresh-sub"))

        assert await auth.sign_in() == "fresh-sub"
        assert (await auth.headers())["Authorization"].startswith("Bearer ")
        assert calls[0]["refresh_token"] == "r-1" and calls[0]["client_id"] == "api-acme"
        assert calls[0]["resource"] == "https://api.example.com" and calls[0]["scope"] == "api:chat"

    async def test_a_token_still_fresh_is_reused_not_refreshed_again(self, monkeypatch):
        auth, store = self.auth()
        await store.save(42, refresh_token="r-1", sub="u", account="a")
        calls = self.patch_refresh(monkeypatch, tokens())

        await auth.sign_in()
        await auth.sign_in()

        assert len(calls) == 1

    async def test_two_chats_of_one_person_refresh_once_so_a_rotating_server_never_sees_the_old_token_twice(self, monkeypatch):
        auth, store = self.auth()
        await store.save(42, refresh_token="r-1", sub="u", account="a")
        calls = self.patch_refresh(monkeypatch, tokens(refresh="r-2"))

        await asyncio.gather(*(auth.sign_in() for _ in range(5)))

        assert len(calls) == 1

    async def test_a_refresh_token_that_rotated_is_the_one_kept(self, monkeypatch):
        auth, store = self.auth()
        await store.save(42, refresh_token="r-1", sub="u", account="a")
        self.patch_refresh(monkeypatch, tokens(refresh="r-2"))

        await auth.sign_in()

        assert (await store.load(42))["refresh_token"] == "r-2"

    async def test_a_refresh_the_server_refused_ends_the_link_and_asks_for_a_new_login(self, monkeypatch):
        """A person disabled at the authorization server has their refresh token revoked at once (specs/020, measured), so this is how they are cut off."""
        auth, store = self.auth()
        await store.save(42, refresh_token="r-1", sub="u", account="a")
        self.patch_refresh(monkeypatch, SignInRefused("credentials"))
        before = _count(metrics.channel_telegram_link_total, step="refresh", outcome="revoked")

        with pytest.raises(SignInRefused) as refused:
            await auth.sign_in()

        assert refused.value.reason == "not_linked"
        assert await store.load(42) is None
        assert _count(metrics.channel_telegram_link_total, step="refresh", outcome="revoked") == before + 1

    async def test_an_authorization_server_that_did_not_answer_keeps_the_link(self, monkeypatch):
        auth, store = self.auth()
        await store.save(42, refresh_token="r-1", sub="u", account="a")
        self.patch_refresh(monkeypatch, SignInRefused("unavailable"))

        with pytest.raises(SignInRefused) as refused:
            await auth.sign_in()

        assert refused.value.reason == "unavailable"
        assert await store.load(42) is not None

    async def test_after_a_401_the_next_sign_in_is_a_new_refresh(self, monkeypatch):
        auth, store = self.auth()
        await store.save(42, refresh_token="r-1", sub="u", account="a")
        calls = self.patch_refresh(monkeypatch, tokens())
        await auth.sign_in()

        assert await auth.reauthenticate() is True
        assert len(calls) == 2

    async def test_reauthenticating_without_a_link_says_it_is_not_worth_repeating(self):
        auth, _ = self.auth()

        assert await auth.reauthenticate() is False


class TestSettings:
    def test_a_complete_configuration_passes(self):
        telegram_link.check_settings()

    def test_local_development_may_use_plain_http_on_localhost(self, monkeypatch):
        monkeypatch.setattr(settings, "telegram_link_public_url", "http://localhost:8081")

        telegram_link.check_settings()

    @pytest.mark.parametrize("url", ["", "http://bot.example.com", "ftp://x", "bot.example.com"])
    def test_a_public_address_must_be_https(self, monkeypatch, url):
        monkeypatch.setattr(settings, "telegram_link_public_url", url)

        with pytest.raises(SystemExit, match="TELEGRAM_LINK_PUBLIC_URL"):
            telegram_link.check_settings()

    @pytest.mark.parametrize("key", ["", "not-a-fernet-key", "short"])
    def test_the_key_must_be_a_fernet_key(self, monkeypatch, key):
        monkeypatch.setattr(settings, "telegram_link_key", key)

        with pytest.raises(SystemExit, match="TELEGRAM_LINK_KEY"):
            telegram_link.check_settings()

    def test_the_authorization_server_must_be_named(self, monkeypatch):
        monkeypatch.setattr(settings, "web_auth_issuer", "")

        with pytest.raises(SystemExit, match="WEB_AUTH_ISSUER"):
            telegram_link.check_settings()

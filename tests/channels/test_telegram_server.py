"""The bot's HTTP listener (app/adapters/inbound/channels/telegram_server.py, specs/021 FR-007, FR-008): the webhook is authenticated by the secret Telegram was given, puts
the update on the inbox before it answers 200, and the link pages escape what they show and are not cacheable or frameable."""
import httpx
import pytest

from app.adapters.inbound.channels import telegram_server
from app.adapters.inbound.channels.telegram_inbox import Inbox
from app.adapters.inbound.channels.telegram_link import ToConfirm
from app.core import metrics
from app.core.config import settings
from tests.channels.telegram_fakes import FakeRedis, message
from tests.conftest import metric_value as _count

SECRET = "s" * 40
HOOK = {"X-Telegram-Bot-Api-Secret-Token": SECRET}


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(settings, "telegram_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "telegram_webhook_url", "https://bot.example.com/telegram/webhook")


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot")


class _Linker:
    def __init__(self, ask=None, account="ann"):
        self.ask, self.account, self.seen = ask, account, []

    async def callback(self, code, state):
        self.seen.append(("callback", code, state))
        return self.ask

    async def confirm(self, token):
        self.seen.append(("confirm", token))
        return self.account


def webhook_app(redis=None):
    redis = redis or FakeRedis()
    return telegram_server.build_app(Inbox(redis, "ecorp"), None, webhook=True), redis


class TestTheWebhook:
    async def test_a_request_with_the_secret_is_put_on_the_inbox_and_answered_200(self):
        app, redis = webhook_app()
        before = _count(metrics.channel_telegram_webhook_total, outcome="accepted")

        async with client(app) as http:
            reply = await http.post("/telegram/webhook", json={"update_id": 5, "message": message("hi")}, headers=HOOK)

        assert reply.status_code == 200
        assert len(redis.lists["telegram:inbox:ecorp"]) == 1
        assert _count(metrics.channel_telegram_webhook_total, outcome="accepted") == before + 1

    @pytest.mark.parametrize("headers", [{}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"}, {"X-Telegram-Bot-Api-Secret-Token": SECRET + "x"}, {"X-Telegram-Bot-Api-Secret-Token": ""}])
    async def test_a_request_without_the_right_secret_is_refused_and_nothing_is_queued(self, headers):
        """Anyone can POST to a public address; without this, anyone could speak to the bot as any Telegram user."""
        app, redis = webhook_app()
        before = _count(metrics.channel_telegram_webhook_total, outcome="forbidden")

        async with client(app) as http:
            reply = await http.post("/telegram/webhook", json={"update_id": 5, "message": message("hi")}, headers=headers)

        assert reply.status_code == 403 and reply.content == b""
        assert not redis.lists
        assert _count(metrics.channel_telegram_webhook_total, outcome="forbidden") == before + 1

    @pytest.mark.parametrize("body", [b"not json", b"[]", b'{"message": {}}', b'{"update_id": "5"}', b"x" * 1_000_001])
    async def test_a_body_that_is_not_an_update_is_a_400_and_is_not_queued(self, body):
        app, redis = webhook_app()

        async with client(app) as http:
            reply = await http.post("/telegram/webhook", content=body, headers=HOOK)

        assert reply.status_code == 400 and not redis.lists

    async def test_when_the_inbox_cannot_take_it_the_answer_is_503_so_telegram_sends_it_again(self):
        class _Down:
            async def push(self, update):
                raise ConnectionError("redis is down")

        app = telegram_server.build_app(_Down(), None, webhook=True)  # type: ignore[arg-type]  # a stand-in for the inbox
        before = _count(metrics.channel_telegram_webhook_total, outcome="unavailable")

        async with client(app) as http:
            reply = await http.post("/telegram/webhook", json={"update_id": 5}, headers=HOOK)

        assert reply.status_code == 503
        assert _count(metrics.channel_telegram_webhook_total, outcome="unavailable") == before + 1

    async def test_a_polling_bot_has_no_webhook_route_at_all(self):
        app = telegram_server.build_app(Inbox(FakeRedis(), "ecorp"), None, webhook=False)

        async with client(app) as http:
            assert (await http.post("/telegram/webhook", json={"update_id": 5}, headers=HOOK)).status_code == 404

    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
    async def test_it_publishes_no_api_description(self, path):
        app, _ = webhook_app()

        async with client(app) as http:
            assert (await http.get(path)).status_code == 404


class TestTheLinkPages:
    def linked_app(self, linker):
        return telegram_server.build_app(Inbox(FakeRedis(), "ecorp"), linker, webhook=False)  # type: ignore[arg-type]  # a stand-in for the linker

    async def test_the_confirmation_names_both_sides_and_escapes_what_it_shows(self):
        ask = ToConfirm("tok<en>", "<script>alert(1)</script> (@x)", 'ann"><b>')
        app = self.linked_app(_Linker(ask))

        async with client(app) as http:
            reply = await http.get("/telegram/link/callback", params={"code": "c", "state": "s"})

        assert reply.status_code == 200
        assert "<script>" not in reply.text and "&lt;script&gt;" in reply.text
        assert 'ann"><b>' not in reply.text
        assert "tok&lt;en&gt;" in reply.text and "someone may be trying to use your account" in reply.text

    async def test_the_pages_cannot_be_cached_framed_or_leak_the_address_in_a_referer(self):
        app = self.linked_app(_Linker(ToConfirm("t", "Ann", "ann")))

        async with client(app) as http:
            reply = await http.get("/telegram/link/callback", params={"code": "c", "state": "s"})

        assert reply.headers["cache-control"] == "no-store"
        assert reply.headers["referrer-policy"] == "no-referrer"
        assert reply.headers["x-frame-options"] == "DENY"
        assert "default-src 'none'" in reply.headers["content-security-policy"]

    @pytest.mark.parametrize("params", [{}, {"code": "c"}, {"state": "s"}, {"error": "access_denied", "code": "c", "state": "s"}])
    async def test_a_return_without_a_code_and_state_or_with_an_error_binds_nothing(self, params):
        linker = _Linker(ToConfirm("t", "Ann", "ann"))

        async with client(self.linked_app(linker)) as http:
            reply = await http.get("/telegram/link/callback", params=params)

        assert reply.status_code == 400 and linker.seen == []

    async def test_a_login_the_flow_refused_gives_a_plain_refusal_page(self):
        async with client(self.linked_app(_Linker(None))) as http:
            reply = await http.get("/telegram/link/callback", params={"code": "c", "state": "s"})

        assert reply.status_code == 400 and "/login" in reply.text

    async def test_pressing_the_button_confirms_with_the_token_in_the_form(self):
        linker = _Linker()

        async with client(self.linked_app(linker)) as http:
            reply = await http.post("/telegram/link/confirm", data={"token": "tok1"})

        assert reply.status_code == 200 and "ann" in reply.text
        assert linker.seen == [("confirm", "tok1")]

    async def test_a_confirmation_that_was_used_or_expired_is_refused(self):
        async with client(self.linked_app(_Linker(account=None))) as http:
            reply = await http.post("/telegram/link/confirm", data={"token": "gone"})

        assert reply.status_code == 400

    async def test_a_bot_without_linking_has_no_link_routes(self):
        app, _ = webhook_app()

        async with client(app) as http:
            assert (await http.get("/telegram/link/callback", params={"code": "c", "state": "s"})).status_code == 404
            assert (await http.post("/telegram/link/confirm", data={"token": "t"})).status_code == 404


class TestStartUpChecks:
    def test_a_good_webhook_configuration_passes(self):
        telegram_server.check_webhook_settings()

    @pytest.mark.parametrize("url", ["", "http://bot.example.com/telegram/webhook", "https://bot.example.com/other"])
    def test_the_address_must_be_public_https_and_end_where_the_route_is(self, monkeypatch, url):
        monkeypatch.setattr(settings, "telegram_webhook_url", url)

        with pytest.raises(SystemExit, match="TELEGRAM_WEBHOOK_URL"):
            telegram_server.check_webhook_settings()

    @pytest.mark.parametrize("secret", ["", "short", "s" * 31, "has space" + "s" * 30, "s" * 257, "dollar$" + "s" * 30])
    def test_the_secret_must_be_long_and_in_the_alphabet_telegram_allows(self, monkeypatch, secret):
        monkeypatch.setattr(settings, "telegram_webhook_secret", secret)

        with pytest.raises(SystemExit, match="TELEGRAM_WEBHOOK_SECRET"):
            telegram_server.check_webhook_settings()

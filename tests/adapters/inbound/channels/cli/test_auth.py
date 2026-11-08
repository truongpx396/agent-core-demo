"""The CLI's sign-in (app/adapters/inbound/channels/cli/auth.py): one sign-in kept in a file only its owner can read, renewed before it expires, and a real loopback
listener that catches the redirect. The authorization server is the fake of tests/adapters/inbound/channels/fake_casdoor.py; the listener and the file are real."""
import asyncio
import os
import socket
import stat
import time
import urllib.parse

import httpx
import pytest

from app.adapters.inbound.channels import oauth
from app.adapters.inbound.channels.cli import auth as cli_auth
from app.adapters.inbound.channels.cli.auth import CliAuth, SessionStore
from app.adapters.inbound.channels.oauth import SignInRefusedError, Tokens
from app.core.config import settings
from tests.adapters.inbound.channels.fake_casdoor import (
    AUDIENCE,
    ISSUER,
    SCOPE,
    FakeCasdoor,
)

CLIENT_ID = "api-ecorp"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def casdoor():
    fake = FakeCasdoor()
    fake.add_person("ecorp", "alice", "pw")
    return fake


@pytest.fixture(autouse=True)
def settings_for_the_local_stack(monkeypatch):
    monkeypatch.setattr(settings, "web_auth_issuer", ISSUER)
    monkeypatch.setattr(settings, "web_auth_audience", AUDIENCE)
    monkeypatch.setattr(settings, "web_auth_scope", SCOPE)
    monkeypatch.setattr(settings, "channel_cli_callback_port", free_port())


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / "config" / "graph-agent-core" / "session.json")


class _Browser:
    """Plays the person and their browser: signs in at the authorization server, then follows the redirect to the CLI's listener."""

    def __init__(self, casdoor, *, state_override=None, sign_in_as=("ecorp", "alice")):
        self.casdoor = casdoor
        self.opened: list[str] = []
        self.state_override = state_override
        self.sign_in_as = sign_in_as
        self.tasks: list[asyncio.Task] = []

    def __call__(self, url: str) -> None:
        self.opened.append(url)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        code = self.casdoor.issue_code(*self.sign_in_as, challenge=query["code_challenge"], resource=query["resource"])
        callback = f"{query['redirect_uri']}?code={code}&state={self.state_override or query['state']}"

        async def follow():
            async with httpx.AsyncClient() as client:
                await client.get(callback)

        self.tasks.append(asyncio.get_running_loop().create_task(follow()))


def make_auth(casdoor, store, browser, said=None):
    http = casdoor.client()
    return CliAuth(http, tenant="ecorp", store=store, open_browser=browser, say=(said.append if said is not None else (lambda text: None))), http


class TestSessionStore:
    def test_the_file_is_private_to_its_owner_and_so_is_the_directory(self, store):
        store.save(Tokens("a", "r", time.time() + 100), issuer=ISSUER, client_id=CLIENT_ID)

        assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700

    def test_it_round_trips_and_leaves_no_temporary_file_behind(self, store):
        tokens = Tokens("a", "r", time.time() + 100)
        store.save(tokens, issuer=ISSUER, client_id=CLIENT_ID)

        assert store.load(issuer=ISSUER, client_id=CLIENT_ID) == tokens
        assert [p.name for p in store.path.parent.iterdir()] == [store.path.name]

    def test_a_session_for_another_authorization_server_or_client_is_not_used(self, store):
        store.save(Tokens("a", "r", time.time() + 100), issuer=ISSUER, client_id=CLIENT_ID)

        assert store.load(issuer="https://elsewhere.test", client_id=CLIENT_ID) is None
        assert store.load(issuer=ISSUER, client_id="api-other-co") is None

    @pytest.mark.parametrize("content", ["", "not json", "{}", '{"issuer": "x"}', "[1, 2]"])
    def test_an_unreadable_session_is_no_session_not_a_crash(self, store, content):
        store.path.parent.mkdir(parents=True)
        store.path.write_text(content)

        assert store.load(issuer=ISSUER, client_id=CLIENT_ID) is None

    def test_clear_forgets_it_and_says_whether_there_was_one(self, store):
        assert store.clear() is False
        store.save(Tokens("a", None, time.time() + 100), issuer=ISSUER, client_id=CLIENT_ID)

        assert store.clear() is True
        assert store.load(issuer=ISSUER, client_id=CLIENT_ID) is None


class TestWhileTheSessionIsGood:
    async def test_a_fresh_saved_session_is_used_with_no_network_and_no_browser(self, casdoor, store):
        store.save(Tokens("saved-token", "r", time.time() + 3600), issuer=ISSUER, client_id=CLIENT_ID)
        browser = _Browser(casdoor)
        auth, http = make_auth(casdoor, store, browser)

        headers = await auth.headers()
        await http.aclose()

        assert headers == {"Authorization": "Bearer saved-token"}
        assert casdoor.requests == [] and browser.opened == []


class TestSigningInWithABrowser:
    async def test_the_redirect_is_caught_by_the_listener_exchanged_and_kept(self, casdoor, store):
        browser = _Browser(casdoor)
        said: list[str] = []
        auth, http = make_auth(casdoor, store, browser, said)

        headers = await auth.headers()
        await asyncio.gather(*browser.tasks)
        again = await auth.headers()
        await http.aclose()

        token = headers["Authorization"].removeprefix("Bearer ")
        claims = oauth.jwt_claims(token)
        assert (claims["owner"], claims["name"], claims["aud"]) == ("ecorp", "alice", [AUDIENCE])
        assert len(browser.opened) == 1, "the second request used the session, not another browser"
        assert again == headers
        assert store.load(issuer=ISSUER, client_id=CLIENT_ID) is not None
        assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
        assert any(browser.opened[0] in line for line in said), "the address is shown in case the browser does not open"
        assert not any(token in line for line in said), "a token is never printed"

    async def test_the_url_asks_for_the_tenants_client_the_apis_audience_and_pkce(self, casdoor, store):
        browser = _Browser(casdoor)
        auth, http = make_auth(casdoor, store, browser)

        await auth.headers()
        await http.aclose()

        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(browser.opened[0]).query))
        assert query["client_id"] == CLIENT_ID
        assert query["resource"] == AUDIENCE and query["scope"] == SCOPE
        assert query["code_challenge_method"] == "S256"
        assert query["redirect_uri"] == f"http://localhost:{settings.channel_cli_callback_port}/callback"

    async def test_a_redirect_with_the_wrong_state_is_refused_and_nothing_is_kept(self, casdoor, store):
        """The state is what ties the redirect to THIS sign-in; a code from someone else's would otherwise sign the terminal in as them."""
        browser = _Browser(casdoor, state_override="not-the-state")
        auth, http = make_auth(casdoor, store, browser)

        with pytest.raises(SignInRefusedError) as exc:
            await auth.headers()
        await http.aclose()

        assert exc.value.reason == "credentials"
        assert store.load(issuer=ISSUER, client_id=CLIENT_ID) is None
        assert casdoor.token_forms == [], "a code that failed the state check is never exchanged"

    async def test_a_sign_in_nobody_completes_gives_up(self, casdoor, store, monkeypatch):
        monkeypatch.setattr(cli_auth, "SIGN_IN_TIMEOUT_SECONDS", 0.2)
        auth, http = make_auth(casdoor, store, lambda url: None)

        with pytest.raises(SignInRefusedError) as exc:
            await auth.headers()
        await http.aclose()

        assert exc.value.reason == "credentials"

    async def test_a_callback_port_that_is_in_use_says_so_and_is_unavailable(self, casdoor, store):
        said: list[str] = []
        auth, http = make_auth(casdoor, store, _Browser(casdoor), said)

        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", settings.channel_cli_callback_port))
            blocker.listen()
            with pytest.raises(SignInRefusedError) as exc:
                await auth.headers()
        await http.aclose()

        assert exc.value.reason == "unavailable"
        assert any("in use" in line and str(settings.channel_cli_callback_port) in line for line in said)


class TestRenewing:
    async def _signed_in(self, casdoor, store):
        browser = _Browser(casdoor)
        auth, http = make_auth(casdoor, store, browser)
        await auth.headers()
        await asyncio.gather(*browser.tasks)
        return auth, http, browser

    async def test_a_session_about_to_expire_is_renewed_with_its_refresh_token_without_a_browser(self, casdoor, store):
        auth, http, browser = await self._signed_in(casdoor, store)
        saved = store.load(issuer=ISSUER, client_id=CLIENT_ID)
        assert saved is not None
        store.save(Tokens(saved.access_token, saved.refresh_token, time.time() + 5), issuer=ISSUER, client_id=CLIENT_ID)
        again, http2 = make_auth(casdoor, store, browser)  # a new terminal run: the session comes from the file

        await again.headers()
        await http.aclose()
        await http2.aclose()

        assert len(browser.opened) == 1, "renewed by refresh, not by asking the person again"
        assert casdoor.token_forms[-1]["grant_type"] == "refresh_token"
        renewed = store.load(issuer=ISSUER, client_id=CLIENT_ID)
        assert renewed is not None and renewed.fresh()

    async def test_a_refresh_that_is_refused_falls_back_to_signing_in_again(self, casdoor, store):
        auth, http, browser = await self._signed_in(casdoor, store)
        casdoor.refresh_tokens.clear()  # revoked
        saved = store.load(issuer=ISSUER, client_id=CLIENT_ID)
        assert saved is not None
        store.save(Tokens(saved.access_token, saved.refresh_token, time.time() + 5), issuer=ISSUER, client_id=CLIENT_ID)
        again, http2 = make_auth(casdoor, store, browser)

        await again.headers()
        await asyncio.gather(*browser.tasks)
        await http.aclose()
        await http2.aclose()

        assert len(browser.opened) == 2

    async def test_a_refresh_that_could_not_be_tried_is_reported_not_turned_into_a_new_sign_in(self, casdoor, store):
        auth, http, browser = await self._signed_in(casdoor, store)
        saved = store.load(issuer=ISSUER, client_id=CLIENT_ID)
        assert saved is not None
        store.save(Tokens(saved.access_token, saved.refresh_token, time.time() + 5), issuer=ISSUER, client_id=CLIENT_ID)
        again, http2 = make_auth(casdoor, store, browser)
        casdoor.down = True

        with pytest.raises(SignInRefusedError) as exc:
            await again.headers()
        await http.aclose()
        await http2.aclose()

        assert exc.value.reason == "unavailable"
        assert len(browser.opened) == 1

    async def test_after_a_401_the_token_is_renewed_and_the_caller_may_repeat(self, casdoor, store):
        auth, http, browser = await self._signed_in(casdoor, store)

        assert await auth.reauthenticate() is True
        await http.aclose()

        assert casdoor.token_forms[-1]["grant_type"] == "refresh_token"

    async def test_when_nothing_can_be_renewed_the_caller_is_told_not_to_repeat(self, casdoor, store):
        auth, http, browser = await self._signed_in(casdoor, store)
        casdoor.down = True

        assert await auth.reauthenticate() is False
        await http.aclose()

    async def test_signing_out_forgets_the_session(self, casdoor, store):
        auth, http, browser = await self._signed_in(casdoor, store)
        await http.aclose()

        assert auth.sign_out() is True
        assert store.load(issuer=ISSUER, client_id=CLIENT_ID) is None

"""A channel's sign-in at the authorization server (app/adapters/inbound/channels/oauth.py), against a fake that behaves as Casdoor 4.24.0 was measured to
(tests/adapters/inbound/channels/fake_casdoor.py; specs/020 research R1). Each case is a way the real thing differs from what one would assume."""
import base64
import hashlib
import urllib.parse

import httpx
import pytest

from app.adapters.inbound.channels import oauth
from app.adapters.inbound.channels.oauth import SignInRefused, Tokens
from tests.adapters.inbound.channels.fake_casdoor import (
    AUDIENCE,
    ISSUER,
    SCOPE,
    FakeCasdoor,
    make_jwt,
)

REDIRECT = "http://localhost:33420/callback"
SIGN_IN = {"client_id": "api-ecorp", "organization": "ecorp", "redirect_uri": REDIRECT, "scope": SCOPE, "resource": AUDIENCE}


@pytest.fixture
def casdoor():
    return FakeCasdoor()


async def sign_in(casdoor, **overrides):
    async with casdoor.client() as http:
        return await oauth.password_sign_in(http, ISSUER, **{**SIGN_IN, "username": "telegram-7", "password": "pw", **overrides})


class TestPkce:
    def test_the_challenge_is_the_s256_of_the_verifier(self):
        verifier, challenge = oauth.pkce_pair()

        assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        assert len(verifier) >= 43  # RFC 7636's floor

    def test_two_pairs_are_not_the_same(self):
        assert oauth.pkce_pair() != oauth.pkce_pair()


class TestJwtClaims:
    def test_the_payload_is_read_without_checking_the_token(self):
        assert oauth.jwt_claims(make_jwt({"sub": "abc", "owner": "ecorp"}))["sub"] == "abc"

    @pytest.mark.parametrize("junk", ["", "nodots", "a.b", "a.%%%.c"])
    def test_anything_that_is_not_a_token_is_empty_not_an_error(self, junk):
        assert oauth.jwt_claims(junk) == {}


class TestAuthorizeUrl:
    def test_it_asks_for_the_apis_audience_with_pkce_and_a_state(self):
        url = oauth.authorize_url(
            ISSUER + "/", client_id="api-ecorp", redirect_uri=REDIRECT, scope=SCOPE, resource=AUDIENCE, state="s1", challenge="c1"
        )

        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{ISSUER}/login/oauth/authorize"
        assert query == {
            "client_id": "api-ecorp",
            "response_type": "code",
            "redirect_uri": REDIRECT,
            "scope": SCOPE,
            "state": "s1",
            "code_challenge": "c1",
            "code_challenge_method": "S256",
            "resource": AUDIENCE,
        }


class TestPasswordSignIn:
    async def test_a_person_gets_a_token_for_the_apis_audience_not_the_clients(self, casdoor):
        """MEASURED: without `resource` on the exchange the audience is the client id, which the front door refuses."""
        person_id = casdoor.add_person("ecorp", "telegram-7", "pw")

        tokens = await sign_in(casdoor)

        claims = oauth.jwt_claims(tokens.access_token)
        assert claims["aud"] == [AUDIENCE]
        assert (claims["sub"], claims["owner"], claims["name"]) == (person_id, "ecorp", "telegram-7")
        assert tokens.refresh_token and tokens.fresh()
        assert casdoor.token_forms[0]["resource"] == AUDIENCE
        assert casdoor.token_forms[0]["code_verifier"]

    @pytest.mark.parametrize(
        ("setup", "reason"),
        [
            ("missing", "unknown_person"),
            ("disabled", "forbidden"),
            ("wrong_password", "credentials"),
        ],
    )
    async def test_each_refusal_is_told_apart(self, casdoor, setup, reason):
        """A missing person may be created (when the operator allowed it); a disabled one must never be; a wrong password must not be retried."""
        if setup == "disabled":
            casdoor.add_person("ecorp", "telegram-7", "pw", forbidden=True)
        if setup == "wrong_password":
            casdoor.add_person("ecorp", "telegram-7", "something else")

        with pytest.raises(SignInRefused) as exc:
            await sign_in(casdoor)

        assert exc.value.reason == reason
        assert casdoor.token_forms == []  # no code, no exchange

    async def test_an_authorization_server_that_does_not_answer_is_unavailable(self, casdoor):
        casdoor.down = True

        with pytest.raises(SignInRefused) as exc:
            await sign_in(casdoor)

        assert exc.value.reason == "unavailable"

    async def test_an_answer_that_is_not_json_is_unavailable_not_a_crash(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(502, text="<html>bad gateway</html>"))) as http:
            with pytest.raises(SignInRefused) as exc:
                await oauth.password_sign_in(http, ISSUER, **SIGN_IN, username="u", password="p")

        assert exc.value.reason == "unavailable"

    async def test_a_reply_with_no_code_is_refused(self):
        def handler(request):
            return httpx.Response(200, json={"status": "ok", "msg": "", "data": None})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(SignInRefused) as exc:
                await oauth.password_sign_in(http, ISSUER, **SIGN_IN, username="u", password="p")

        assert exc.value.reason == "credentials"


class TestRefresh:
    async def test_a_refresh_repeats_the_resource_of_the_original_grant(self, casdoor):
        """MEASURED: a refresh without it (or with another) is refused as a mismatch."""
        casdoor.add_person("ecorp", "telegram-7", "pw")
        first = await sign_in(casdoor)

        async with casdoor.client() as http:
            second = await oauth.refresh_tokens(
                http, ISSUER, client_id="api-ecorp", refresh_token=first.refresh_token or "", scope=SCOPE, resource=AUDIENCE
            )

        assert oauth.jwt_claims(second.access_token)["aud"] == [AUDIENCE]
        assert casdoor.token_forms[-1]["resource"] == AUDIENCE

    async def test_disabling_a_person_ends_their_refresh_tokens_at_once(self, casdoor):
        casdoor.add_person("ecorp", "telegram-7", "pw")
        first = await sign_in(casdoor)
        casdoor.people[("ecorp", "telegram-7")]["forbidden"] = True

        async with casdoor.client() as http:
            with pytest.raises(SignInRefused) as exc:
                await oauth.refresh_tokens(
                    http, ISSUER, client_id="api-ecorp", refresh_token=first.refresh_token or "", scope=SCOPE, resource=AUDIENCE
                )

        assert exc.value.reason == "credentials"


class TestTokens:
    def test_a_token_is_not_fresh_inside_the_margin_before_it_expires(self):
        import time

        assert Tokens("a", None, time.time() + 3600).fresh()
        assert not Tokens("a", None, time.time() + 30).fresh()
        assert not Tokens("a", None, time.time() - 1).fresh()

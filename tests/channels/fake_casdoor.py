"""A Casdoor that behaves as 4.24.0 was MEASURED to (specs/020 research R1), for the channel sign-in tests. Only the endpoints the channels call exist.

What it reproduces on purpose, because each was a way to get this wrong:
- the login endpoint answers HTTP 200 with {"status": "error", "msg": ...} for a wrong password, a missing person and a disabled one, in
  Casdoor's own words;
- a code exchange that does not repeat `resource` yields a token whose audience is the CLIENT ID (which the front door refuses);
- a refresh must repeat the original grant's `resource`, and a disabled person's refresh token is revoked;
- PKCE is checked, a code is single-use, and an application's client credentials (HTTP Basic) can add a person.
"""
import base64
import hashlib
import json
import secrets
import time
import urllib.parse

import httpx

ISSUER = "https://auth.test"
AUDIENCE = "https://api.test"
SCOPE = "api:chat"
LIFETIME = 604800


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def make_jwt(claims: dict) -> str:
    return f"{_b64({'alg': 'none'})}.{_b64(claims)}.signature"


class FakeCasdoor:
    def __init__(self, *, provision_client=("provisioner", "provisioner-secret")) -> None:
        self.people: dict[tuple[str, str], dict] = {}  # (organization, name) -> {"password", "forbidden", "id"}
        self.codes: dict[str, dict] = {}
        self.refresh_tokens: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []  # (method, path)
        self.token_forms: list[dict] = []
        self.add_user_calls: list[dict] = []
        self.logins = 0
        self.provision_client = provision_client
        self.down = False

    # -- the world ----------------------------------------------------------------------------------------------------------------------
    def add_person(self, organization: str, name: str, password: str, *, forbidden: bool = False) -> str:
        person_id = f"id-{name}"
        self.people[(organization, name)] = {"password": password, "forbidden": forbidden, "id": person_id}
        return person_id

    def issue_code(self, organization: str, name: str, *, challenge: str, resource: str) -> str:
        """What the login page does after a person signs in at the browser."""
        code = secrets.token_hex(8)
        self.codes[code] = {"org": organization, "name": name, "challenge": challenge, "resource": resource}
        return code

    # -- the transport ------------------------------------------------------------------------------------------------------------------
    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if self.down:
            raise httpx.ConnectError("casdoor is down")
        if request.url.path == "/api/login":
            return self._login(request)
        if request.url.path == "/api/login/oauth/access_token":
            return self._token(request)
        if request.url.path == "/api/add-user":
            return self._add_user(request)
        return httpx.Response(404)

    def _login(self, request: httpx.Request) -> httpx.Response:
        self.logins += 1
        query = dict(urllib.parse.parse_qsl(request.url.query.decode()))
        body = json.loads(request.content)
        person = self.people.get((body["organization"], body["username"]))
        if person is None:
            return self._error(f"The user: {body['organization']}/{body['username']} doesn't exist")
        if person["forbidden"]:
            return self._error("The user is forbidden to sign in, please contact the administrator")
        if person["password"] != body["password"]:
            return self._error("invalid username or password: password or code is incorrect, you have 4 remaining chances")
        assert query.get("code_challenge_method") == "S256", "the channels always use PKCE"
        code = self.issue_code(body["organization"], body["username"], challenge=query["code_challenge"], resource=query.get("resource", ""))
        return httpx.Response(200, json={"status": "ok", "msg": "", "data": code})

    @staticmethod
    def _error(message: str) -> httpx.Response:
        return httpx.Response(200, json={"status": "error", "msg": message, "data": None})

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        self.token_forms.append(form)
        if form["grant_type"] == "authorization_code":
            grant = self.codes.pop(form.get("code", ""), None)  # single use
            if grant is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            verifier = form.get("code_verifier", "")
            if base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() != grant["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            # MEASURED: the audience follows `resource` on the EXCHANGE; without it the token is for the client id.
            audience = form.get("resource") or form["client_id"]
            return self._issue(grant["org"], grant["name"], audience, form["client_id"], grant["resource"])
        if form["grant_type"] == "refresh_token":
            grant = self.refresh_tokens.get(form.get("refresh_token", ""))
            if grant is None or self.people[(grant["org"], grant["name"])]["forbidden"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "refresh token is invalid or revoked"})
            if form.get("resource", "") != grant["resource"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "resource parameter does not match the original grant"})
            return self._issue(grant["org"], grant["name"], form.get("resource") or form["client_id"], form["client_id"], grant["resource"])
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _issue(self, org: str, name: str, audience: str, client_id: str, resource: str) -> httpx.Response:
        now = int(time.time())
        person = self.people[(org, name)]
        access = make_jwt(
            {"iss": ISSUER, "aud": [audience], "sub": person["id"], "owner": org, "name": name, "scope": SCOPE, "iat": now, "exp": now + LIFETIME}
        )
        refresh = secrets.token_hex(12)
        self.refresh_tokens[refresh] = {"org": org, "name": name, "resource": resource}
        return httpx.Response(
            200, json={"access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": LIFETIME, "scope": SCOPE}
        )

    def _add_user(self, request: httpx.Request) -> httpx.Response:
        expected = httpx.BasicAuth(*self.provision_client)
        sent = request.headers.get("authorization", "")
        if sent != next(expected.auth_flow(httpx.Request("GET", "https://x"))).headers["authorization"]:
            return self._error("Unauthorized operation")
        body = json.loads(request.content)
        self.add_user_calls.append(body)
        if (body["owner"], body["name"]) in self.people:
            return self._error("the user already exists")
        self.add_person(body["owner"], body["name"], body["password"], forbidden=body.get("isForbidden", False))
        return httpx.Response(200, json={"status": "ok", "msg": "", "data": "Affected"})

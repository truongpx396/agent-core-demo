"""The MCP gateway (app/mcp/gateway.py, specs/017) end to end, in memory: the real SDK middleware, the real Streamable HTTP transport and
the real published tools, with only the identity provider replaced (tests/mcp/gateway_support.py).

What these pin is the boundary: who gets in, who is told what, and that the tenant a tool reads is the TOKEN's, never an argument and
never the launcher's environment. Each rejection test is a forgery or a mistake that has a name; the measurements behind them are in
specs/017-mcp-gateway/research.md.
"""
import asyncio
import contextlib
import logging
import time

import httpx
import httpx2  # the SDK's own HTTP client library; the client given to `streamable_http_client` must be one of its
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.core import metrics
from app.mcp import gateway, ops_server
from app.mcp import server as ecorp_server
from tests.conftest import metric_value
from tests.mcp import gateway_support as gs

JSON_RPC = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class Gateway:
    def __init__(self, app, authorization_server: gs.AuthorizationServer) -> None:
        self.app = app
        self.auth = authorization_server

    def http(self, token: str | None = None, *, host: str | None = None) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if host:
            headers["Host"] = host
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=gs.PUBLIC, headers=headers)

    async def post(self, path: str, token: str | None = None, *, host: str | None = None) -> httpx.Response:
        async with self.http(token, host=host) as client:
            return await client.post(path, json=JSON_RPC, headers=HEADERS)

    async def get(self, path: str) -> httpx.Response:
        async with self.http() as client:
            return await client.get(path)

    async def call(self, path: str, token: str, tool: str, arguments: dict | None = None) -> str:
        http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), base_url=gs.PUBLIC, headers={"Authorization": f"Bearer {token}"}
        )
        async with http, Client(streamable_http_client(f"{gs.PUBLIC}{path}", http_client=http)) as client:
            result = await client.call_tool(tool, arguments or {})
            return result.content[0].text


@contextlib.asynccontextmanager
async def running(*, down: bool = False, **config_overrides):
    """A gateway with its lifespan running. Entered in the TEST body, not a fixture: the SDK's session manager is an anyio task group,
    which must be entered and exited by one task, and pytest runs a fixture's setup and teardown as different tasks."""
    authorization_server = gs.AuthorizationServer()
    authorization_server.down = down
    keys_client = authorization_server.client()
    app = gateway.create_app(gs.config(**config_overrides), http_client=keys_client)
    try:
        async with app.router.lifespan_context(app):
            yield Gateway(app, authorization_server)
    finally:
        await keys_client.aclose()


@pytest.fixture
def ecorp_rows(monkeypatch):
    """The employee store, replaced by a function that records the ctx it was asked under."""
    seen: list[dict] = []

    async def fake_query_employees_impl(department, name_contains, ctx):
        seen.append({"tenant": ctx["tenant"], "principal": ctx["principal"], "department": department, "name": name_contains})
        return f"rows for {ctx['tenant']} as {ctx['principal']}"

    monkeypatch.setattr(ecorp_server, "query_employees_impl", fake_query_employees_impl)
    return seen


def token(gw: Gateway, **overrides) -> str:
    return gw.auth.signer.sign(gs.claims(**overrides))


def outcomes(server: str) -> dict[str, float]:
    return {o: metric_value(metrics.agent_mcp_gateway_requests_total, server=server, outcome=o)
            for o in ("ok", "invalid_token", "jwks_unavailable", "forbidden_tenant")}


# -- the door ------------------------------------------------------------------------------------------------------------


async def test_a_request_without_a_token_is_401_and_says_where_a_client_logs_in():
    async with running() as gw:
        response = await gw.post("/ecorp/mcp")

        assert response.status_code == 401
        challenge = response.headers["www-authenticate"]
        assert challenge.startswith("Bearer ")
        assert f'resource_metadata="{gs.PUBLIC}/.well-known/oauth-protected-resource/ecorp/mcp"' in challenge


@pytest.mark.parametrize(
    "path, scope",
    [("/ecorp/mcp", "ecorp:read"), ("/ops/mcp", "ops:read")],
)
async def test_each_server_publishes_its_own_resource_metadata_at_the_host_root(path, scope):
    async with running() as gw:
        response = await gw.get(f"/.well-known/oauth-protected-resource{path}")

        assert response.status_code == 200
        document = response.json()
        assert document["resource"] == f"{gs.PUBLIC}{path}"
        assert document["authorization_servers"] == [gs.ISSUER]  # exactly: a trailing slash would differ from the issuer in every token
        assert document["scopes_supported"] == [scope]


async def test_a_valid_token_lists_the_tools():
    async with running() as gw:
        response = await gw.post("/ecorp/mcp", token(gw))

        assert response.status_code == 200
        assert [t["name"] for t in response.json()["result"]["tools"]] == ["query_employees"]


async def test_the_tenant_and_principal_a_tool_acts_for_are_the_tokens(ecorp_rows):
    async with running() as gw:
        answer = await gw.call("/ecorp/mcp", token(gw, owner="acme", sub="user-7"), "query_employees", {"department": "Sales"})

        assert answer == "rows for acme as user-7"
        assert ecorp_rows == [{"tenant": "acme", "principal": "user-7", "department": ecorp_server.Department("Sales"), "name": None}]


async def test_a_tenant_named_in_the_arguments_changes_nothing(ecorp_rows):
    async with running() as gw:
        # Every argument the tool has, plus the two it must not: none of them can move the tenant the rows are read for.
        await gw.call(
            "/ecorp/mcp",
            token(gw, owner="acme"),
            "query_employees",
            {"tenant": "rival", "principal": "someone-else", "department": "Sales", "name_contains": "rival"},
        )

        assert [(r["tenant"], r["principal"]) for r in ecorp_rows] == [("acme", "user-1")]


async def test_the_launchers_environment_is_never_consulted_by_the_gateway(ecorp_rows, launched_as):
    async with running() as gw:
        launched_as(tenant="launcher-tenant", principal="launcher-principal")

        await gw.call("/ecorp/mcp", token(gw, owner="acme"), "query_employees")

        assert [r["tenant"] for r in ecorp_rows] == ["acme"]


async def test_a_valid_token_with_no_tenant_claim_is_refused_by_the_tool_not_run_unscoped(ecorp_rows, launched_as):
    async with running() as gw:
        launched_as(tenant="launcher-tenant", principal="launcher-principal")  # must not be the fallback

        answer = await gw.call("/ecorp/mcp", token(gw, owner=None), "query_employees")

        assert answer == gateway.GATEWAY_REFUSAL
        assert ecorp_rows == []


async def test_a_tenant_that_is_not_text_is_refused_not_coerced_into_something_that_matches_a_row(ecorp_rows):
    async with running() as gw:
        answer = await gw.call("/ecorp/mcp", token(gw, owner=["acme"]), "query_employees")

        assert answer == gateway.GATEWAY_REFUSAL
        assert ecorp_rows == []


async def test_callers_of_different_tenants_at_the_same_time_each_get_their_own(monkeypatch):
    async with running() as gw:
        async def slow_impl(_department, _name, ctx):
            await asyncio.sleep(0.01 * (5 - int(ctx["tenant"][-1])))  # the first to arrive is the last to finish
            return f"{ctx['tenant']}:{ctx['principal']}"

        monkeypatch.setattr(ecorp_server, "query_employees_impl", slow_impl)

        answers = await asyncio.gather(
            *[gw.call("/ecorp/mcp", token(gw, owner=f"tenant{i}", sub=f"user{i}"), "query_employees") for i in range(5)]
        )

        assert answers == [f"tenant{i}:user{i}" for i in range(5)]


# -- tokens that must not get in ----------------------------------------------------------------------------------------


def _forgeries(gw: Gateway) -> dict[str, str]:
    other_key = gs.Signer("k1")  # same kid, different key: the signature must be what fails
    return {
        "expired": token(gw, exp=int(time.time()) - 600),
        "no expiry": token(gw, exp=None),
        "no subject": token(gw, sub=None),
        "another issuer": token(gw, iss="https://evil.example.test"),
        # What Casdoor mints when the client sent no `resource`: the client id is the audience. The whole point of the check.
        "audience is the client id": token(gw, aud=["client-1"]),
        "audience is the OTHER server": token(gw, aud=[gs.OPS]),
        "audience is a prefix of this one": token(gw, aud=[gs.ECORP + "/extra"]),
        "signed by a key the issuer never published": other_key.sign(gs.claims()),
        "a kid the issuer never published": gw.auth.signer.sign(gs.claims(), kid="k-unknown"),
        "alg none": gs.unsigned(gs.claims()),
        "HS256 with the public key as the secret": gs.hs256_signed_with_the_public_key(gs.claims(), gw.auth.signer),
        "subject is not text": token(gw, sub=["user-1"]),
        "not a token": "not.a.jwt",
        "garbage": "x",
    }


async def test_every_forged_or_mistaken_token_is_401():
    async with running() as gw:
        for why, bad in _forgeries(gw).items():
            response = await gw.post("/ecorp/mcp", bad)

            assert response.status_code == 401, why
            assert 'resource_metadata="' in response.headers["www-authenticate"], why


async def test_a_rejection_is_counted_and_never_logs_the_token(caplog):
    async with running() as gw:
        before = outcomes("ecorp")
        forged = _forgeries(gw)

        with caplog.at_level(logging.DEBUG):
            for bad in forged.values():
                await gw.post("/ecorp/mcp", bad)

        assert outcomes("ecorp")["invalid_token"] - before["invalid_token"] == len(forged)
        everything = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        for bad in forged.values():
            if len(bad) > 30:  # a real-looking token; "x" is a substring of any log line
                assert bad not in everything


async def test_a_token_without_the_scope_is_403_insufficient_scope_and_says_which():
    async with running() as gw:
        response = await gw.post("/ecorp/mcp", token(gw, scope="openid profile"))

        assert response.status_code == 403
        assert 'error="insufficient_scope"' in response.headers["www-authenticate"]
        assert "ecorp:read" in response.headers["www-authenticate"]


async def test_the_scope_may_be_one_of_several():
    async with running() as gw:
        response = await gw.post("/ecorp/mcp", token(gw, scope="openid ecorp:read profile"))

        assert response.status_code == 200


async def test_a_token_minted_for_ecorp_does_not_open_ops_even_with_the_ops_scope():
    async with running() as gw:
        response = await gw.post("/ops/mcp", token(gw, owner="ops-team", scope="ops:read", aud=[gs.ECORP]))

        assert response.status_code == 401


async def test_the_audience_and_the_scope_are_both_needed_a_right_audience_with_the_wrong_scope_fails():
    """The authorization server accepted a `resource` for one server with a scope for another (measured): neither check alone is the boundary."""
    async with running() as gw:
        response = await gw.post("/ops/mcp", token(gw, owner="ops-team", scope="ecorp:read", aud=[gs.OPS]))

        assert response.status_code == 403


async def test_a_request_for_another_host_is_refused_before_any_tool_runs():
    async with running() as gw:
        response = await gw.post("/ecorp/mcp", token(gw), host="evil.example")

        assert response.status_code == 421


async def test_an_unknown_path_is_404():
    async with running() as gw:
        assert (await gw.get("/ecorp/other")).status_code == 404
        assert (await gw.get("/nothing")).status_code == 404


# -- the operations server is the deployment's, not a tenant's ----------------------------------------------------------


@pytest.fixture
def ops_data(monkeypatch):
    async def fake_metrics():
        return "metrics: all quiet"

    monkeypatch.setattr(ops_server, "fetch_metrics_summary_impl", fake_metrics)


def _ops_token(gw: Gateway, **overrides) -> str:
    return token(gw, **{"aud": [gs.OPS], "scope": "ops:read", "owner": "ops-team", **overrides})


async def test_an_operator_tenant_reads_the_operations_server(ops_data):
    async with running() as gw:
        assert await gw.call("/ops/mcp", _ops_token(gw), "fetch_metrics_summary") == "metrics: all quiet"


async def test_a_tenant_that_is_not_an_operator_is_403_even_with_a_perfect_token(ops_data):
    async with running() as gw:
        before = outcomes("ops")

        response = await gw.post("/ops/mcp", _ops_token(gw, owner="acme"))

        assert response.status_code == 403
        assert response.json()["error"] == "forbidden"
        assert outcomes("ops")["forbidden_tenant"] - before["forbidden_tenant"] == 1


async def test_with_no_operator_tenants_configured_the_operations_server_refuses_everyone(ops_data):
    async with running(ops_tenants=frozenset()) as closed:
        response = await closed.post("/ops/mcp", _ops_token(closed))

    assert response.status_code == 403


async def test_a_request_with_no_token_to_ops_is_still_401_not_403():
    """The gate only judges callers who proved who they are; an anonymous one is told where to log in."""
    async with running() as gw:
        assert (await gw.post("/ops/mcp")).status_code == 401


# -- operations -----------------------------------------------------------------------------------------------------------


async def test_every_call_leaves_an_audit_line_naming_who_but_never_the_token_or_the_arguments(ecorp_rows, caplog):
    async with running() as gw:
        bearer = token(gw, owner="acme", sub="user-7")

        with caplog.at_level(logging.INFO):
            await gw.call("/ecorp/mcp", bearer, "query_employees", {"name_contains": "a-secret-looking-filter"})

        (line,) = [r for r in caplog.records if r.getMessage() == "mcp_tool_call"]
        assert (line.tenant, line.principal, line.tool, line.outcome) == ("acme", "user-7", "query_employees", "ok")
        everything = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        assert bearer not in everything
        assert "a-secret-looking-filter" not in everything


async def test_health_needs_no_token():
    async with running() as gw:
        assert (await gw.get("/healthz")).status_code == 200


async def test_a_replica_that_can_reach_the_signing_keys_is_ready():
    async with running() as gw:
        assert (await gw.get("/readyz")).status_code == 200


async def test_a_replica_that_cannot_reach_the_signing_keys_is_not_ready_so_the_load_balancer_skips_it():
    async with running(down=True) as down:
        response = await down.get("/readyz")

    assert response.status_code == 503


async def test_the_gateway_serves_the_same_tools_as_the_stdio_servers():
    """One definition of the tools, two transports: a tool added to one and not the other would be reachable by the wrong audience."""
    async with running() as gw:
        ecorp = (await gw.post("/ecorp/mcp", token(gw))).json()["result"]["tools"]
        ops = (await gw.post("/ops/mcp", _ops_token(gw))).json()["result"]["tools"]

        for served, stdio in ((ecorp, ecorp_server.mcp), (ops, ops_server.mcp)):
            expected = {t.name: t.input_schema for t in await stdio.list_tools()}
            assert {t["name"]: t["inputSchema"] for t in served} == expected

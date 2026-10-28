"""The built-in web page's sign-in (specs/018, task T062): what the server tells the page, the script it loads, and what that script does.

Three layers, cheapest first:
  * the server side, in Python: the <meta> tag the page is rendered with (and that its value cannot break out of the attribute), the script's route
    (public, and not part of the API document), and that header mode is told nothing about an authorization server;
  * guards on the text of the page and the script for the properties that must not be quietly dropped (no third-party script, the token only in
    sessionStorage and only in an Authorization header). These are greps: they catch a deletion, not a bug;
  * the script RUN, under Node when it is on the machine, against a small DOM stub: the PKCE challenge really is the SHA-256 of the stored
    verifier, a `state` that does not come back unchanged ends the sign-in without any request, the code is cleaned from the address bar BEFORE it is
    exchanged, a 401 from this origin (and only this origin) forgets the token, and header mode is left exactly as it was. Skipped, never failed,
    without Node. It uses no package and no network; the repo's one Node toolchain (promptfoo/) is not involved.

The real proof that a person can sign in with a real browser against a real Casdoor is `scripts/authlimit_web_check.py`, a manual tier like the
other end-to-end checks of this stack.
"""
import html
import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import main as api
from app.api import web_auth
from app.api.routers.system import ui
from app.core.config import settings

REPO = Path(__file__).resolve().parents[2]
PAGE = (REPO / "app" / "api" / "static" / "index.html").read_text()
SCRIPT = (REPO / "app" / "api" / "static" / "auth.js").read_text()
ROUTES = json.loads((REPO / "services" / "authlimit" / "routes.json").read_text())["routes"]


def _meta(page: str) -> dict:
    (content,) = re.findall(r'<meta name="agent-auth" content="([^"]*)">', page)
    return json.loads(html.unescape(content))


# --- the server side ---------------------------------------------------------------------------------------------------------------------


def test_header_mode_tells_the_page_the_mode_and_nothing_about_an_authorization_server():
    assert _meta(ui()) == {"mode": "headers"}


def test_identity_mode_tells_the_page_where_to_sign_in_what_to_ask_for_and_how_to_find_the_tenants_client(identity_mode, monkeypatch):
    monkeypatch.setattr(settings, "web_auth_issuer", "https://auth.example.test/")
    monkeypatch.setattr(settings, "web_auth_audience", "https://api.example.test/")

    assert _meta(ui()) == {
        "mode": "identity",
        "issuer": "https://auth.example.test",
        "audience": "https://api.example.test",
        "scope": "api:chat",
        "client_id_prefix": "api-",
    }, "trailing slashes are removed: the audience must equal the front door's byte for byte"


def test_a_setting_cannot_break_out_of_the_attribute_it_is_written_into(identity_mode, monkeypatch):
    monkeypatch.setattr(settings, "web_auth_issuer", 'https://x.test"><script>alert(1)</script><meta x="')

    page = ui()

    assert "<script>alert(1)" not in page
    assert _meta(page)["issuer"].startswith("https://x.test\"><script>"), "the value survives intact, as data"


def test_the_placeholder_is_always_replaced():
    assert web_auth.PLACEHOLDER in PAGE and web_auth.PLACEHOLDER not in ui()


def test_the_script_is_served_to_anyone_as_javascript_and_is_not_part_of_the_api_document():
    client = TestClient(api.app)

    response = client.get("/static/auth.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-cache"
    assert response.text == SCRIPT
    assert "/static/auth.js" not in api.app.openapi()["paths"]


def test_the_front_door_lets_the_script_through_unchecked_because_a_person_must_load_it_before_signing_in():
    rule = next(r for r in ROUTES if r["method"] in ("*", "GET") and r["pattern"] in ("/static/*", "/static/auth.js"))

    assert rule["class"] == "public"


# --- guards on the text ------------------------------------------------------------------------------------------------------------------


def test_the_page_loads_exactly_one_external_script_and_it_is_this_one_before_any_inline_script():
    external = re.findall(r"<script[^>]*\bsrc=[\"']([^\"']+)[\"']", PAGE)

    assert external == ["/static/auth.js"], "no third-party script"
    assert PAGE.index('src="/static/auth.js"') < PAGE.index("<script>"), "window.agentAuth must exist before the page's own script runs"


def test_the_page_builds_every_request_through_the_one_function_that_asks_the_sign_in_to_decorate_it():
    body = PAGE.split("function headers() {", 1)[1].split("\n}\n", 1)[0]

    assert "window.agentAuth.decorate(" in body
    assert PAGE.count("fetch(") >= 8 and "headers: headers()" in PAGE and "headers: multipartHeaders()" in PAGE
    assert not re.search(r"fetch\([^)]*\{[^}]*headers:\s*\{", PAGE), "no request builds its own headers and skips the sign-in"


def test_the_script_calls_no_address_but_its_own_origin_and_the_one_it_was_configured_with():
    assert not re.search(r"https?://", SCRIPT), "no address is written into the script: the authorization server comes from the page's <meta>"
    assert "cfg.issuer" in SCRIPT


def test_the_token_is_kept_in_session_storage_only_and_the_only_thing_in_local_storage_is_the_organization():
    local_uses = [line.strip() for line in SCRIPT.splitlines() if "localStorage" in line and not line.strip().startswith("//")]

    assert local_uses and all("ORG_KEY" in line for line in local_uses), local_uses
    assert "sessionStorage" in SCRIPT and "document.cookie" not in SCRIPT


def test_the_token_is_sent_only_in_an_authorization_header_and_never_placed_in_a_url():
    code = "\n".join(line for line in SCRIPT.splitlines() if not line.strip().startswith("//"))

    assert code.count('"Bearer "') == 1 and code.count('headers["Authorization"]') == 1
    assert not re.search(r"access_token\s*[=:]\s*[^,\n]*(location|href|search|URLSearchParams)", code)
    assert "access_token=" not in code and "?access_token" not in code


# --- the script, run -----------------------------------------------------------------------------------------------------------------------

NODE = shutil.which("node")

HARNESS = textwrap.dedent(
    r"""
    // Runs /static/auth.js in a context with a DOM stub and prints one JSON object per scenario. No network: every request is a recorded fake.
    const fs = require("fs");
    const vm = require("vm");
    const { webcrypto, createHash } = require("crypto");
    const source = fs.readFileSync(process.env.AUTH_JS, "utf8");
    const ORIGIN = "https://api.example.test";
    const ISSUER = "https://auth.example.test";
    const IDENTITY = { mode: "identity", issuer: ISSUER, audience: ORIGIN, scope: "api:chat", client_id_prefix: "api-" };
    const b64u = (buf) => Buffer.from(buf).toString("base64url");
    const jwt = (claims) => ["e30", b64u(JSON.stringify(claims)), "sig"].join(".");
    const NOW = Math.floor(Date.now() / 1000);

    function makeEnv({ config, search = "", session = {}, local = {}, fetchImpl }) {
      const events = [];
      const store = (init) => {
        const data = { ...init };
        return { getItem: (k) => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v); }, removeItem: (k) => { delete data[k]; }, data };
      };
      const sessionStorage = store(session);
      const localStorage = store(local);
      const listeners = {};
      const element = () => ({ hidden: true, textContent: "", value: "", listeners: {}, addEventListener(t, f) { this.listeners[t] = f; }, closest: () => null });
      const elements = {};
      const document = {
        readyState: "complete",
        querySelector: (sel) => (sel === 'meta[name="agent-auth"]' ? { content: config } : null),
        getElementById: (id) => (elements[id] ||= element()),
        addEventListener: (t, f) => { (listeners[t] ||= []).push(f); },
        dispatchEvent: (e) => { events.push(e.type); (listeners[e.type] || []).forEach((f) => f(e)); },
      };
      const location = {
        origin: ORIGIN, pathname: "/", search, href: ORIGIN + "/" + search,
        assign(url) { events.push("assign"); this.assigned = url; },
      };
      const history = { replaceState(_s, _t, url) { events.push("replaceState"); location.search = ""; location.cleaned = url; } };
      const fetchCalls = [];
      const window = {
        fetch: async (input, init) => { fetchCalls.push({ url: String(input), init }); events.push("fetch"); return fetchImpl(String(input), init); },
      };
      window.window = window;
      const context = vm.createContext({
        window, document, location, history, sessionStorage, localStorage, crypto: webcrypto, TextEncoder, TextDecoder, URL, URLSearchParams,
        btoa: (s) => Buffer.from(s, "binary").toString("base64"), atob: (s) => Buffer.from(s, "base64").toString("binary"),
        CustomEvent, console,
        // In a browser the global `fetch` IS window.fetch, including the wrapper the script installs over it later.
        fetch: (...args) => window.fetch(...args),
      });
      vm.runInContext(source, context);
      return { window, document, location, sessionStorage, localStorage, fetchCalls, events, elements };
    }
    const json = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body });
    const out = {};

    (async () => {
      // 1. header mode: the page is exactly what it was
      {
        const env = makeEnv({ config: '{"mode":"headers"}', fetchImpl: () => json(200, {}) });
        const original = env.window.fetch;
        const h = env.window.agentAuth.decorate({ "X-Tenant-Id": "t", "X-Principal-Id": "p", "X-Domain": "ecorp" });
        out.header_mode = { mode: env.window.agentAuth.mode, headers: h, fetchUntouched: env.window.fetch === original, calls: env.fetchCalls.length };
      }
      // 1b. a page served raw (the placeholder still in the tag) is header mode too
      {
        const env = makeEnv({ config: "__AGENT_AUTH__", fetchImpl: () => json(200, {}) });
        out.raw_page_mode = env.window.agentAuth.mode;
      }
      // 2. identity mode, not signed in: the pickers' headers are dropped, no Authorization is invented
      {
        const env = makeEnv({ config: JSON.stringify(IDENTITY), fetchImpl: () => json(200, {}) });
        await env.window.agentAuth.ready;
        out.no_token = env.window.agentAuth.decorate({ "X-Tenant-Id": "victim", "X-Principal-Id": "boss", "X-Domain": "ecorp" });
      }
      // 3. identity mode, signed in: Bearer on the request; an expired token is dropped and never sent
      {
        const good = jwt({ sub: "u1", name: "alice", owner: "ecorp", exp: NOW + 600 });
        const stale = jwt({ sub: "u1", exp: NOW - 600 });
        const live = makeEnv({ config: JSON.stringify(IDENTITY), session: { "agent-core-demo:access-token": good }, fetchImpl: () => json(200, {}) });
        await live.window.agentAuth.ready;
        const dead = makeEnv({ config: JSON.stringify(IDENTITY), session: { "agent-core-demo:access-token": stale }, fetchImpl: () => json(200, {}) });
        await dead.window.agentAuth.ready;
        out.with_token = { headers: live.window.agentAuth.decorate({ "X-Tenant-Id": "x", "X-Domain": "ecorp" }), good };
        out.expired = { headers: dead.window.agentAuth.decorate({ "X-Domain": "ecorp" }), kept: dead.sessionStorage.getItem("agent-core-demo:access-token") };
      }
      // 4. starting a sign-in: S256 challenge of the stored verifier, a state, the resource, the tenant's client id, nothing secret in the URL
      {
        const env = makeEnv({ config: JSON.stringify(IDENTITY), fetchImpl: () => json(200, {}) });
        await env.window.agentAuth.ready;
        await env.window.agentAuth.signIn("  ecorp ");
        const url = new URL(env.location.assigned);
        const flow = JSON.parse(env.sessionStorage.getItem("agent-core-demo:pkce-flow"));
        out.sign_in = {
          origin: url.origin, path: url.pathname, params: Object.fromEntries(url.searchParams), flow,
          expectedChallenge: createHash("sha256").update(flow.verifier).digest("base64url"),
          localKeys: Object.keys(env.localStorage.data), fetches: env.fetchCalls.length,
        };
        let refused = null;
        try { await env.window.agentAuth.signIn("   "); } catch (e) { refused = e.message; }
        out.sign_in_without_organization = { refused, assignedAgain: env.events.filter((e) => e === "assign").length };
      }
      // 5. the way back with the right state: the address is cleaned BEFORE the exchange; the verifier goes with the code; the token is kept
      {
        const first = makeEnv({ config: JSON.stringify(IDENTITY), fetchImpl: () => json(200, {}) });
        await first.window.agentAuth.signIn("ecorp");
        const flowJson = first.sessionStorage.getItem("agent-core-demo:pkce-flow");
        const flow = JSON.parse(flowJson);
        const token = jwt({ sub: "u1", owner: "ecorp", exp: NOW + 600 });
        const env = makeEnv({
          config: JSON.stringify(IDENTITY), search: "?code=CODE123&state=" + flow.state, session: { "agent-core-demo:pkce-flow": flowJson },
          fetchImpl: () => json(200, { access_token: token }),
        });
        await env.window.agentAuth.ready;
        const call = env.fetchCalls[0];
        out.return_ok = {
          calls: env.fetchCalls.length, url: call && call.url, method: call && call.init.method, body: call && Object.fromEntries(new URLSearchParams(call.init.body)),
          order: env.events.filter((e) => e === "replaceState" || e === "fetch"), cleaned: env.location.cleaned, search: env.location.search,
          stored: env.sessionStorage.getItem("agent-core-demo:access-token") === token, flowLeft: env.sessionStorage.getItem("agent-core-demo:pkce-flow"),
          verifier: flow.verifier,
        };
      }
      // 6. the way back with a state that is not ours, with no flow at all, and with an error: no request, no token
      for (const [name, search, session] of [
        ["wrong_state", "?code=C&state=NOT-OURS", { "agent-core-demo:pkce-flow": JSON.stringify({ state: "ours", verifier: "v", org: "ecorp" }) }],
        ["no_flow", "?code=C&state=anything", {}],
        ["refused", "?error=access_denied&state=s", {}],
      ]) {
        const env = makeEnv({ config: JSON.stringify(IDENTITY), search, session, fetchImpl: () => json(200, { access_token: "should-never-be-stored" }) });
        await env.window.agentAuth.ready;
        out[name] = { calls: env.fetchCalls.length, token: env.sessionStorage.getItem("agent-core-demo:access-token"), cleaned: env.location.cleaned,
                      flowLeft: env.sessionStorage.getItem("agent-core-demo:pkce-flow") };
      }
      // 7. a 401 from this origin forgets the token; one from the authorization server does not
      {
        const token = jwt({ sub: "u1", exp: NOW + 600 });
        const env = makeEnv({ config: JSON.stringify(IDENTITY), session: { "agent-core-demo:access-token": token }, fetchImpl: () => json(401, {}) });
        await env.window.agentAuth.ready;
        await env.window.fetch(ISSUER + "/api/login", { method: "POST" });
        const afterForeign = env.sessionStorage.getItem("agent-core-demo:access-token");
        await env.window.fetch("/chat/sessions", {});
        out.on_401 = { afterForeign: afterForeign === token, afterOwn: env.sessionStorage.getItem("agent-core-demo:access-token"), event: env.events.includes("agent-auth-changed") };
      }
      process.stdout.write(JSON.stringify(out));
    })().catch((err) => { console.error(err); process.exit(1); });
    """
)


@pytest.fixture(scope="module")
def run():
    if not NODE:
        pytest.skip("node is not installed: the script is checked by the greps above and by scripts/authlimit_web_check.py")
    env = {**os.environ, "AUTH_JS": str(REPO / "app" / "api" / "static" / "auth.js")}
    result = subprocess.run([NODE, "-e", HARNESS], capture_output=True, text=True, timeout=60, check=False, env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_header_mode_is_left_exactly_as_it_was(run):
    assert run["header_mode"] == {
        "mode": "headers",
        "headers": {"X-Tenant-Id": "t", "X-Principal-Id": "p", "X-Domain": "ecorp"},
        "fetchUntouched": True,
        "calls": 0,
    }
    assert run["raw_page_mode"] == "headers", "a page served without the server's rendering must not guess identity mode"


def test_identity_mode_without_a_token_sends_neither_the_pickers_identity_nor_an_invented_token(run):
    assert run["no_token"] == {"X-Domain": "ecorp"}


def test_identity_mode_with_a_token_sends_it_as_a_bearer_and_drops_the_pickers_identity(run):
    assert run["with_token"]["headers"] == {"X-Domain": "ecorp", "Authorization": "Bearer " + run["with_token"]["good"]}


def test_an_expired_token_is_never_sent_and_is_forgotten(run):
    assert run["expired"] == {"headers": {"X-Domain": "ecorp"}, "kept": None}


def test_signing_in_sends_the_person_to_the_authorization_server_with_a_pkce_challenge_of_a_verifier_only_this_tab_holds(run):
    s = run["sign_in"]

    assert (s["origin"], s["path"]) == ("https://auth.example.test", "/login/oauth/authorize")
    p = s["params"]
    assert p["client_id"] == "api-ecorp", "the organization is trimmed and becomes the tenant's client"
    assert (p["response_type"], p["code_challenge_method"], p["scope"]) == ("code", "S256", "api:chat")
    assert p["redirect_uri"] == "https://api.example.test/"
    assert p["resource"] == "https://api.example.test", "RFC 8707: without it the token is for the client id and the front door refuses it"
    assert p["code_challenge"] == s["expectedChallenge"], "the challenge is the SHA-256 of the verifier kept in this tab"
    assert p["state"] == s["flow"]["state"] and len(p["state"]) >= 16
    assert len(s["flow"]["verifier"]) >= 43, "RFC 7636: at least 43 characters"
    assert s["flow"]["verifier"] not in json.dumps(p), "the verifier itself never leaves the tab until the code exchange"
    assert s["localKeys"] == ["agent-core-demo:organization"], "the only thing kept in local storage is which organization was typed"
    assert s["fetches"] == 0


def test_an_empty_organization_is_refused_before_anything_leaves_the_page(run):
    assert run["sign_in_without_organization"] == {"refused": "Enter your organization.", "assignedAgain": 1}


def test_the_way_back_cleans_the_address_first_then_exchanges_the_code_with_the_verifier_and_keeps_the_token(run):
    r = run["return_ok"]

    assert r["calls"] == 1 and r["method"] == "POST" and r["url"] == "https://auth.example.test/api/login/oauth/access_token"
    assert r["order"] == ["replaceState", "fetch"], "the single-use code must not stay in the address bar or history while a request is in flight"
    assert (r["cleaned"], r["search"]) == ("/", "")
    assert r["body"] == {
        "grant_type": "authorization_code", "client_id": "api-ecorp", "code": "CODE123", "redirect_uri": "https://api.example.test/",
        "code_verifier": r["verifier"], "resource": "https://api.example.test",
    }
    assert r["stored"] is True and r["flowLeft"] is None, "the token is kept; the one-time flow is not"


@pytest.mark.parametrize("scenario", ["wrong_state", "no_flow", "refused"])
def test_a_return_that_is_not_ours_makes_no_request_and_keeps_no_token(run, scenario):
    r = run[scenario]

    assert r["calls"] == 0 and r["token"] is None
    assert r["cleaned"] == "/", "the address is cleaned either way"
    assert r["flowLeft"] is None, "a sign-in attempt is single-use"


def test_a_401_from_this_origin_forgets_the_token_and_one_from_anywhere_else_does_not(run):
    r = run["on_401"]

    assert r == {"afterForeign": True, "afterOwn": None, "event": True}

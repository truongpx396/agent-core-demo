"""Can a person sign in to the built-in web page and chat, in a real browser, through the front door? `make
authlimit-web-check` (specs/018, T062/T068).

`tests/api/test_web_ui_auth.py` runs the sign-in script against a DOM stub; this runs the real page in
Chromium (Playwright) against the real stack: the real Casdoor login form, the code exchange across origins
(CORS), the front door, the API in identity mode and a worker. For each of two tenants it creates a throwaway
user and then:
- loads the page signed out: the sign-in box shows, the Tenant and Principal pickers are hidden;
- signs in through Casdoor's form and lands back signed in as that person, with no code or token in the
  address bar or history, the token in sessionStorage only and nothing but the organization in localStorage;
- asks "list the employees" and gets ONLY that tenant's rows, although the page also tries to send the OTHER
  tenant in X-Tenant-Id (the pickers' values are dropped in identity mode and the door removes them anyway);
- reloads and is still signed in; signs out and is back at the box with the conversation gone; a 401 (revoked
  token) puts it back at the box with "no longer valid".

Needs the app stack behind the door, bootstrapped (`make authlimit-up-app`, `make mcp-gateway-bootstrap
ARGS="--tenant ecorp --tenant other-co"`, `make mcp-gateway-ca`), a worker on a model double that can call
`query_employees` (loadtest/fake_llm_server.py via `make fake-llm`, the worker's OPENAI_API_BASE pointed at
it), a second tenant with employees, and Playwright with Chromium (`playwright install chromium`). A manual
tier like `make authlimit-smoke`, never CI; exit 0 only if every check passed.
"""
import argparse
import asyncio
import os
import sys

from playwright.async_api import Page, async_playwright

from scripts.authlimit_smoke import FAILED, Door, check, info

TENANTS = {
    # tenant -> (a name that must appear in its answer, a name that must NOT)
    "ecorp": ("Priya Nair", "Zed Zephyr"),
    "other-co": ("Zed Zephyr", "Priya Nair"),
}
TIMEOUT_MS = 30_000


async def sign_in(page: Page, organization: str, username: str, password: str) -> None:
    await page.fill("#authOrg", organization)
    await page.click("#authSignIn")
    await page.wait_for_url("**/login/oauth/authorize**", timeout=TIMEOUT_MS)
    await page.fill("#username", username)
    await page.fill("#password", password)
    await page.click("button[type=submit]")
    await page.wait_for_selector("#authSignOut:not([hidden])", timeout=TIMEOUT_MS)


async def one_tenant(browser, door: Door, tenant: str, api: str) -> None:
    print(f"\n== {tenant}: a real browser, the real login form, the real door")
    organization, username = await door.user(tenant)
    password = door.passwords[(organization, username)]
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    await page.goto(api + "/")
    await page.wait_for_selector("#authBox:not([hidden])", timeout=TIMEOUT_MS)

    check("signed out: the sign-in box shows and says so", "Sign in" in (await page.inner_text("#authNote")), await page.inner_text("#authNote"))
    pickers_hidden = await page.evaluate("['tenant','principal'].every(id => document.getElementById(id).closest('.identity-field').hidden)")
    check("the Tenant and Principal pickers are hidden: who you are comes from the token", pickers_hidden)

    await sign_in(page, organization, username, password)
    who = await page.inner_text("#authWho")
    check("back on the page, signed in as that person and tenant", username in who and tenant in who, who)
    check("the address bar holds no code, state or token", page.url.rstrip("/") == api.rstrip("/") and "code=" not in page.url and "token" not in page.url, page.url)
    storage = await page.evaluate(
        "(() => { const t = sessionStorage.getItem('agent-core-demo:access-token');"
        " return {session: Object.keys(sessionStorage), tokenInLocal: Object.keys(localStorage).some(k => localStorage.getItem(k).includes(t) || k.includes('token')),"
        " organization: localStorage.getItem('agent-core-demo:organization'), cookie: document.cookie}; })()"
    )
    # localStorage also holds the page's own picker lists (tenants, principals; that is old behaviour and not a credential). What matters is the token.
    check("the token is in sessionStorage and in no localStorage entry; the organization is remembered; no cookie", "agent-core-demo:access-token" in storage["session"]
          and storage["tokenInLocal"] is False and storage["organization"] == tenant and storage["cookie"] == "", str(storage))
    history_length_ok = await page.evaluate("history.length") <= 4
    check("the code was replaced in the history, not left as an entry the Back button would replay", history_length_ok)

    # Make the page TRY to be the other tenant: the pickers' values are what a header-mode page would send. In identity mode they must go nowhere.
    other = next(t for t in TENANTS if t != tenant)
    await page.evaluate(
        "(other) => { for (const id of ['tenant','principal']) { const el = document.getElementById(id);"
        " el.innerHTML = ''; const o = document.createElement('option'); o.value = other; o.textContent = other; el.appendChild(o); el.value = other; } }",
        other,
    )
    sent: list[dict] = []
    page.on("request", lambda req: sent.append(req.headers) if req.url.startswith(api + "/chat/stream/queued") else None)
    await page.fill("#input", "list the employees")
    await page.click("#send")
    want, forbidden = TENANTS[tenant]
    await page.wait_for_function("document.querySelectorAll('#messages .msg.assistant').length > 0", timeout=TIMEOUT_MS)
    await page.wait_for_function(
        f"[...document.querySelectorAll('#messages .msg.assistant')].some(m => m.textContent.includes({want!r}))", timeout=TIMEOUT_MS
    )
    answer = " ".join(await page.eval_on_selector_all("#messages .msg.assistant", "els => els.map(e => e.textContent)"))
    check(f"the answer, from the real tool under the token's tenant, lists {tenant}'s own people ({want})", want in answer, answer[:200])
    check(f"and none of {other}'s ({forbidden})", forbidden not in answer, answer[:200])
    # The request the browser really sent: the browser's own view, after the page's code ran (not what the page meant to send).
    headers = sent[0] if sent else {}
    check("the request carried the Bearer token and neither identity header", "authorization" in headers and "x-tenant-id" not in headers and "x-principal-id" not in headers, str(sorted(headers)))

    await page.reload()
    await page.wait_for_selector("#authSignOut:not([hidden])", timeout=TIMEOUT_MS)
    check("a reload keeps the login (it lives in this tab's sessionStorage)", True)

    await page.click("#authSignOut")
    await page.wait_for_selector("#authSignIn:not([hidden])", timeout=TIMEOUT_MS)
    check("signing out lands on the sign-in box with the conversation gone", await page.locator("#messages .msg").count() == 0)
    check("and the token is gone from the tab", "agent-core-demo:access-token" not in await page.evaluate("Object.keys(sessionStorage)"))

    # A token the door refuses (revoked, tampered, expired) must put the page back at the box, never loop. Plant a well-formed but unsigned one.
    await page.evaluate(
        "sessionStorage.setItem('agent-core-demo:access-token', 'e30.' + btoa(JSON.stringify({sub:'x', exp: 4102444800})).replace(/=/g,'') + '.sig')"
    )
    await page.reload()
    # The page first looks signed in (the token parses and has not expired), then its first request is refused with a 401 and it signs itself out:
    # wait for where it ends up, not for the moment in between.
    await page.wait_for_function("document.getElementById('authNote').textContent.includes('no longer valid')", timeout=TIMEOUT_MS)
    note = await page.inner_text("#authNote")
    check("a token the door refuses sends the page back to the sign-in box, saying so, without redirecting",
          "no longer valid" in note and "authorize" not in page.url and not await page.is_hidden("#authSignIn"), f"{note!r} {page.url}")

    check("no script error on the page", not errors, "; ".join(errors))
    await context.close()


async def run(args: argparse.Namespace) -> None:
    password = os.environ.get("CASDOOR_ADMIN_PASSWORD", "")
    if not password:
        sys.exit("CASDOOR_ADMIN_PASSWORD is not set (make authlimit-web-check passes it)")
    import ssl

    door = Door(argparse.Namespace(api=args.api, mcp=args.mcp, auth=args.auth, compose="", keys_dir=""), ssl.create_default_context(cafile=args.ca), password)
    try:
        await door.start()
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                for tenant in TENANTS:
                    try:
                        await one_tenant(browser, door, tenant, args.api.rstrip("/"))
                    except Exception as exc:  # noqa: BLE001 - a manual check: report the step that failed and go on to the next tenant
                        check(f"{tenant}: the run completed", False, f"{type(exc).__name__}: {str(exc)[:300]}")
            finally:
                await browser.close()
    finally:
        await door.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sign in to the built-in web page in a real browser, through the front door (specs/018).")
    parser.add_argument("--ca", required=True, help="Caddy's local root certificate (make mcp-gateway-ca), used for the admin calls; the browser ignores certificate errors")
    parser.add_argument("--api", default="https://api.localhost:8443")
    parser.add_argument("--mcp", default="https://mcp.localhost:8443")
    parser.add_argument("--auth", default="https://auth.localhost:8443")
    asyncio.run(run(parser.parse_args()))
    print()
    if FAILED:
        print(f"{len(FAILED)} check(s) FAILED:")
        for label in FAILED:
            print(f"  - {label}")
        sys.exit(1)
    info("all checks passed")


if __name__ == "__main__":
    main()

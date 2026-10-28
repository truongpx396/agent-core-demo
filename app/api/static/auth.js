// Sign-in for the built-in web page (specs/018): authorization code with PKCE against the authorization server, as a PUBLIC client.
//
// What this file is for. When the API runs in `identity` mode (AUTH_MODE, app/api/deps.py) the front door in front of it wants a Bearer access
// token on every request and ignores the page's tenant and principal pickers. This file gets the token (a redirect to the authorization server and
// a code exchange back at this page's own address), keeps it where the page can use it, puts it on every request the page makes, and asks the
// person to sign in again when the API says the token is no good. In `headers` mode (the API run alone, the tests) it does nothing at all and the
// page behaves exactly as it always did.
//
// How it knows the mode: the server writes it into a <meta name="agent-auth"> tag when it renders the page (app/api/web_auth.py), which is
// smaller than a second request the page would have to wait for before it could send its first one. The tag carries no secret: the authorization
// server's address, the API's audience, the scope, and the prefix a tenant's client id has.
//
// Properties this file keeps, each pinned by a test (tests/api/test_web_ui_auth.py) and by a run in a real browser (scripts/authlimit_web_check.py):
//   * no third-party script, no library: the Web Crypto API and fetch only;
//   * the token lives in sessionStorage (this tab, gone when it closes), never in localStorage, a cookie or a URL, and is sent ONLY to this origin;
//   * the authorization code is exchanged with a PKCE verifier (S256) and a `state` that must come back unchanged, and is removed from the address
//     bar as soon as it is read;
//   * a 401 from the API signs the person out and asks again; it never redirects by itself, so a misconfiguration cannot become a redirect loop.
(function () {
  "use strict";

  const TOKEN_KEY = "agent-core-demo:access-token";
  const FLOW_KEY = "agent-core-demo:pkce-flow";
  const ORG_KEY = "agent-core-demo:organization";

  function readConfig() {
    const tag = document.querySelector('meta[name="agent-auth"]');
    try {
      const cfg = JSON.parse(tag ? tag.content : "");
      return cfg && cfg.mode === "identity" ? cfg : { mode: "headers" };
    } catch (_err) {
      // The page served raw (no server in the way) or a tag that is not JSON: behave as header mode, the page's original behaviour.
      return { mode: "headers" };
    }
  }

  const cfg = readConfig();
  const identityMode = cfg.mode === "identity";

  // sessionStorage can throw (blocked storage, private windows); the page then simply cannot keep a login, and says so when it is needed.
  function store(key, value) {
    try { value === null ? sessionStorage.removeItem(key) : sessionStorage.setItem(key, value); } catch (_err) { /* nothing to do */ }
  }
  function load(key) {
    try { return sessionStorage.getItem(key); } catch (_err) { return null; }
  }

  function base64url(bytes) {
    let text = "";
    bytes.forEach((b) => { text += String.fromCharCode(b); });
    return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function randomToken(byteCount) {
    return base64url(crypto.getRandomValues(new Uint8Array(byteCount)));
  }
  async function sha256Challenge(verifier) {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
    return base64url(new Uint8Array(digest));
  }

  // The page's own address is the redirect target: the authorization server compares it exactly with what the application registered.
  function redirectUri() {
    return location.origin + "/";
  }

  // The claims of the token, for DISPLAY only ("signed in as ..."). Nothing is decided from them here: the front door verifies the signature.
  function claimsOf(token) {
    try {
      const body = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      const bytes = Uint8Array.from(atob(body + "=".repeat((4 - (body.length % 4)) % 4)), (c) => c.charCodeAt(0));
      return JSON.parse(new TextDecoder().decode(bytes));
    } catch (_err) {
      return {};
    }
  }

  function expired(token) {
    const exp = claimsOf(token).exp;
    return typeof exp === "number" && exp * 1000 <= Date.now();
  }

  function token() {
    const t = load(TOKEN_KEY);
    if (t && expired(t)) { store(TOKEN_KEY, null); return null; }
    return t;
  }

  async function signIn(organization) {
    const org = (organization || "").trim();
    if (!org) { throw new Error("Enter your organization."); }
    const verifier = randomToken(48);
    const state = randomToken(16);
    store(FLOW_KEY, JSON.stringify({ state, verifier, org }));
    try { localStorage.setItem(ORG_KEY, org); } catch (_err) { /* a convenience only */ }
    const query = new URLSearchParams({
      client_id: cfg.client_id_prefix + org,
      response_type: "code",
      redirect_uri: redirectUri(),
      scope: cfg.scope,
      state,
      code_challenge: await sha256Challenge(verifier),
      code_challenge_method: "S256",
      // RFC 8707: the audience of the token. Without it the authorization server mints a token for the client id, which the front door refuses.
      resource: cfg.audience,
    });
    location.assign(cfg.issuer + "/login/oauth/authorize?" + query.toString());
  }

  // On load: if the address carries `?code=&state=`, this is the return from the authorization server. Exchange it, store the token, clean the URL.
  async function finishSignIn() {
    const params = new URLSearchParams(location.search);
    const code = params.get("code");
    const returnedState = params.get("state");
    const error = params.get("error");
    if (!code && !error) { return null; }
    history.replaceState(null, "", location.pathname);  // the code is single-use, but it has no business staying in the address bar or history
    let flow = null;
    try { flow = JSON.parse(load(FLOW_KEY) || "null"); } catch (_err) { flow = null; }
    store(FLOW_KEY, null);
    if (error) { return "The authorization server refused the sign-in (" + error + ")."; }
    if (!flow || !returnedState || flow.state !== returnedState) {
      return "That sign-in did not start in this tab, so it was ignored. Sign in again.";
    }
    const response = await fetch(cfg.issuer + "/api/login/oauth/access_token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        client_id: cfg.client_id_prefix + flow.org,
        code,
        redirect_uri: redirectUri(),
        code_verifier: flow.verifier,
        // Again: the authorization server mints the token's audience from the request that EXCHANGES the code (measured on Casdoor 4.24; the
        // authorize request's `resource` alone is not enough).
        resource: cfg.audience,
      }).toString(),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok || !body.access_token) { return "The sign-in could not be completed. Try again."; }
    store(TOKEN_KEY, body.access_token);
    return null;
  }

  function signOut() {
    store(TOKEN_KEY, null);
    store(FLOW_KEY, null);
  }

  // The page's `headers()` calls this on every request it builds. In identity mode the pickers' values are dropped (the API would ignore them, and
  // the front door removes them anyway) and the token is added; in header mode the headers are returned untouched.
  function decorate(headers) {
    if (!identityMode) { return headers; }
    delete headers["X-Tenant-Id"];
    delete headers["X-Principal-Id"];
    const t = token();
    if (t) { headers["Authorization"] = "Bearer " + t; }
    return headers;
  }

  // A 401 from this origin means the token is missing, expired, revoked or refused: forget it and let the page ask again. Only same-origin
  // responses count; the authorization server's own 401s (a wrong password) are its business.
  // Installed in identity mode only: in header mode `window.fetch` is left exactly as the browser made it.
  if (identityMode) {
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async function (input, init) {
      const response = await nativeFetch(input, init);
      if (response.status === 401) {
        const url = new URL(typeof input === "string" ? input : input.url, location.href);
        if (url.origin === location.origin) {
          const hadToken = !!load(TOKEN_KEY);
          signOut();
          document.dispatchEvent(new CustomEvent("agent-auth-changed", { detail: { expired: hadToken } }));
        }
      }
      return response;
    };
  }

  // --- the page's sign-in box ------------------------------------------------------------------------------------------------------------
  function setUpBox(message) {
    const box = document.getElementById("authBox");
    if (!box || !identityMode) { return; }
    box.hidden = false;
    for (const id of ["tenant", "principal"]) {
      const picker = document.getElementById(id);
      if (picker && picker.closest(".identity-field")) { picker.closest(".identity-field").hidden = true; }
    }
    const who = document.getElementById("authWho");
    const org = document.getElementById("authOrg");
    const inButton = document.getElementById("authSignIn");
    const outButton = document.getElementById("authSignOut");
    const note = document.getElementById("authNote");
    const t = token();
    const claims = t ? claimsOf(t) : {};
    who.textContent = t ? (claims.name || claims.sub || "signed in") + (claims.owner ? " · " + claims.owner : "") : "";
    who.hidden = !t;
    org.hidden = inButton.hidden = !!t;
    outButton.hidden = !t;
    note.textContent = message || (t ? "" : "Sign in to chat.");
    if (!org.value) {
      let last = "";
      try { last = localStorage.getItem(ORG_KEY) || ""; } catch (_err) { last = ""; }
      org.value = last || "ecorp";
    }
  }

  function wireBox() {
    const box = document.getElementById("authBox");
    if (!box || !identityMode) { return; }
    document.getElementById("authSignIn").addEventListener("click", () => {
      signIn(document.getElementById("authOrg").value).catch((err) => setUpBox(err.message));
    });
    document.getElementById("authOrg").addEventListener("keydown", (event) => {
      if (event.key === "Enter") { document.getElementById("authSignIn").click(); }
    });
    document.getElementById("authSignOut").addEventListener("click", () => {
      signOut();
      // Reload, not just redraw: the conversation on screen was the signed-out person's, and the next person on this browser must not see it.
      location.assign(redirectUri());
    });
    // A 401 on a first visit is just "not signed in yet"; a 401 after a login means that login stopped working (expired, revoked).
    document.addEventListener("agent-auth-changed", (event) => {
      setUpBox(event.detail && event.detail.expired ? "Your sign-in is no longer valid. Sign in again." : null);
    });
  }

  // `ready` resolves once the return from the authorization server (if this load is one) has been handled, so the page's first request already
  // carries the token. The page awaits it before its first fetch.
  const ready = (async function () {
    let message = null;
    if (identityMode) {
      try { message = await finishSignIn(); } catch (_err) { message = "The sign-in could not be completed. Try again."; }
    }
    const start = () => { wireBox(); setUpBox(message); };
    if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", start); } else { start(); }
  })();

  window.agentAuth = { mode: cfg.mode, ready, decorate, signIn, signOut, token };
})();

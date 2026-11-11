# Bruno collection

The HTTP API as a [Bruno](https://www.usebruno.com) collection: plain-text `.bru` files that diff and review like code.
Open Bruno, choose **Open Collection**, pick this folder, then an environment.

| Environment | Reaches | Who is calling |
|---|---|---|
| `local` | `http://localhost:8000`, the API alone (`make serve`) | `AUTH_MODE=headers`, the `.env.example` default: the `tenant` and `principal` variables are sent as `X-Tenant-Id` and `X-Principal-Id` |
| `door` | `https://api.localhost:8443`, the stack behind the front door (`make authlimit-up-app`) | `AUTH_MODE=identity`: only the secret variable `accessToken` counts; the two headers are ignored |

`domain` (`X-Domain`) picks the domain in both, and a worker serving it must be running (`make agent-worker`; `make ingest-worker` for uploads).
Without one, a turn's stream ends with an `error` event once the first-response deadline passes.

## A token for `door`

Sign in once with the CLI against the door (`CHANNEL_API_URL=https://api.localhost:8443 make chat`), then read the token it saved
(`CHANNEL_CLI_SESSION_FILE`, default below) and paste it into the environment's `accessToken`. It expires; sign in again for a new one.

```bash
jq -r .access_token ~/.config/graph-agent-core/channel-session.json
```

The door's certificate is signed by Caddy's local CA: `make mcp-gateway-ca` exports it and prints where, and Bruno has to trust it.

## From a terminal

```bash
npm install -g @usebruno/cli    # tested with 4.3.0
cd bruno
bru run -r --env local
bru run -r --env door --env-var accessToken="$TOKEN" --cacert path/to/caddy-root.crt
```

There is no make target: `promptfoo/` is the repo's one Node project, and this would be a second.

## What is in it

Folders run in this order, and later requests use what earlier ones set (`threadId`, `approvalThreadId` and `jobId` are run-time
variables, new for each run).

- **System**: liveness and readiness.
- **Chat**: send a message (SSE), list this caller's sessions, read one's messages.
- **Approval**: ask with `require_approval`, look at the pending approval, resume (approve or reject), cancel.
- **Usage**: the tenant's spend, the caller's limits and the credit wallet.
- **Ingest**: upload `files/sample.pdf` (PDF or DOCX only) and follow its job. This adds a page to the tenant's corpus.

Not here: `GET /` (the web page), `POST /billing/webhooks/{provider}` (the payment provider signs it; it is not in the OpenAPI document),
the MCP gateway and the ML service.

To see tenant isolation, send a message, change `tenant` or `principal` in `local`, and read the same thread's messages: a 404, as for an unknown id.

`tests/adapters/inbound/api/test_bruno_collection_matches_api.py` fails when a route has no request here, a request names a route, a body
field or a header the API lacks, or a `{{variable}}` is defined nowhere. Add a request with the endpoint.

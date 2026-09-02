"""Locust load test against the FastAPI service (POST /chat and friends).

Needs the real app running first: `make up` + `make serve` (or `make
up-app`), and ideally `make ingest` so `SingleTenantUser`'s questions have
real docs to retrieve. Point Locust at it with `--host http://localhost:8000`
(the Makefile targets below already do this):

    make loadtest             # interactive UI at http://localhost:8089
    make loadtest-headless    # fixed users/duration, CSV + HTML report

Two distinct user classes on purpose, not one. This app's rate limiter
(app/api/rate_limit.py) is keyed per `X-Tenant-Id`, not per IP and not
globally (`RATE_LIMIT_PER_MINUTE`, default 30/min, app/core/config.py) — a
load test that reused one tenant id for every simulated user would mostly
just measure that limiter, not the agent loop it's meant to be sizing:

- `ManyTenantsUser` mints its own synthetic tenant id per simulated user, so
  concurrency scales past the per-tenant ceiling. This is the one that
  actually load-tests the agent loop itself — moderation, the semantic
  cache, the LLM call, the Postgres checkpointer — under real concurrency.
- `SingleTenantUser` deliberately shares ONE tenant (`acme`, `DEFAULT_TENANT`
  in app/core/config.py — the tenant `make ingest` seeds docs under, see
  GRAPH_PATTERNS.md pattern 17) so its questions get real hybrid-search
  hits. Past a modest combined request rate this class WILL start seeing
  429s — that's the rate limiter doing its job, not a bug here; it's the
  realistic shape of one real customer's traffic against its own ceiling,
  not a synthetic-tenant free-for-all.
"""
import random
import uuid

from locust import HttpUser, between, task

RETRIEVAL_QUESTIONS = [
    "What is a LangGraph checkpointer?",
    "What are Acme Corp support hours?",
    "Who works in Engineering at Acme?",
]

ACME_TENANT = "acme"  # DEFAULT_TENANT, app/core/config.py — where `make ingest` seeds docs


class ManyTenantsUser(HttpUser):
    """Unconstrained agent-loop throughput: every simulated user gets its
    own tenant id, so the per-tenant rate limit never becomes the thing
    actually being measured."""

    weight = 3
    wait_time = between(1, 3)

    def on_start(self) -> None:
        self.tenant_id = f"loadtest-{uuid.uuid4().hex[:8]}"
        self.principal_id = f"user-{uuid.uuid4().hex[:8]}"
        # Reused across this user's own requests for the whole run, like a
        # real conversation thread — exercises the checkpointer's memory
        # path under concurrency instead of starting a fresh thread every call.
        self.thread_id = str(uuid.uuid4())

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Tenant-Id": self.tenant_id, "X-Principal-Id": self.principal_id}

    @task(3)
    def calculator(self) -> None:
        a, b = random.randint(1, 99), random.randint(1, 99)
        self.client.post(
            "/chat",
            json={"message": f"what is {a} * {b}?", "thread_id": self.thread_id},
            headers=self._headers,
            name="/chat [calculator]",
        )

    @task(1)
    def generic_question(self) -> None:
        # A synthetic tenant has no ingested docs, so this exercises the
        # retrieval-miss path through the graph, not a real citation hit —
        # SingleTenantUser below covers the real-retrieval case.
        self.client.post(
            "/chat",
            json={"message": random.choice(RETRIEVAL_QUESTIONS), "thread_id": self.thread_id},
            headers=self._headers,
            name="/chat [generic, no docs for a synthetic tenant]",
        )

    @task(2)
    def usage(self) -> None:
        # GET /usage isn't in RATE_LIMITED_PATHS (app/api/rate_limit.py) —
        # a cheap, unthrottled read mixed in alongside the turn-creating calls.
        self.client.get("/usage", headers=self._headers, name="/usage")


class SingleTenantUser(HttpUser):
    """One real customer's traffic: fixed tenant, real retrieval hits, and
    the shared 30-req/min ceiling as a real constraint. Expect 429s once
    combined load across every SingleTenantUser instance crosses it."""

    weight = 1
    wait_time = between(1, 3)

    def on_start(self) -> None:
        self.principal_id = f"user-{uuid.uuid4().hex[:8]}"
        self.thread_id = str(uuid.uuid4())

    @task
    def ask_real_question(self) -> None:
        self.client.post(
            "/chat",
            json={"message": random.choice(RETRIEVAL_QUESTIONS), "thread_id": self.thread_id},
            headers={"X-Tenant-Id": ACME_TENANT, "X-Principal-Id": self.principal_id},
            name="/chat [single-tenant, real retrieval]",
        )

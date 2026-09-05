"""Locust load test against the queued chat path (POST /chat/stream/queued
-> Redis -> app/turns/agent_worker.py) — the only HTTP chat path this app
serves. Meant to run against loadtest/fake_llm_server.py, not native Ollama.

Why a separate LLM double at all: native Ollama on this project's own dev
stack serializes to exactly one in-flight generation (`-np 1`, verified
directly), so a load test against it can't tell whether
app/turns/agent_worker.py's own concurrency (`_MAX_CONCURRENCY`, the pooled
checkpointer) does anything — every result would be dominated by Ollama's
own ceiling. Point the app at the fake server instead to isolate that:

    OPENAI_API_BASE=http://localhost:9009/v1 make serve
    OPENAI_API_BASE=http://localhost:9009/v1 make agent-worker
    make fake-llm            # in a third terminal
    make loadtest-queued     # this file, interactive UI at :8089

One user class: this file exists purely to push concurrency through the
queued path, so it only needs the "own synthetic tenant per user" shape
(past the per-tenant rate limiter, see app/api/rate_limit.py) that actually
stresses agent_worker.py's own concurrency, not a rate-limiter-focused
scenario (a different concern entirely).
"""
import random
import uuid

from locust import HttpUser, between, task

RETRIEVAL_QUESTIONS = [
    "What is a LangGraph checkpointer?",
    "What are Acme Corp support hours?",
    "Who works in Engineering at Acme?",
]


class QueuedTurnUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self) -> None:
        self.tenant_id = f"loadtest-{uuid.uuid4().hex[:8]}"
        self.principal_id = f"user-{uuid.uuid4().hex[:8]}"
        # Reused across this user's own requests, like a real conversation
        # thread — exercises the checkpointer's memory path under
        # concurrency instead of starting a fresh thread every call.
        self.thread_id = str(uuid.uuid4())

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Tenant-Id": self.tenant_id, "X-Principal-Id": self.principal_id}

    @task(3)
    def calculator(self) -> None:
        # loadtest/fake_llm_server.py's _calculator simulator matches this
        # exact "N op N" shape and emits a real calculator tool_call for it.
        a, b = random.randint(1, 99), random.randint(1, 99)
        self.client.post(
            "/chat/stream/queued",
            json={"message": f"what is {a} * {b}?", "thread_id": self.thread_id, "images": []},
            headers=self._headers,
            name="/chat/stream/queued [calculator]",
        )

    @task(1)
    def generic_question(self) -> None:
        self.client.post(
            "/chat/stream/queued",
            json={
                "message": random.choice(RETRIEVAL_QUESTIONS),
                "thread_id": self.thread_id,
                "images": [],
            },
            headers=self._headers,
            name="/chat/stream/queued [generic]",
        )

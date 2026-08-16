"""FastAPI service exposing the LangGraph agent over HTTP.

Endpoints:
- GET  /health       -> liveness check
- POST /chat         -> non-streaming, returns full answer (Pydantic in/out)
- POST /chat/stream  -> production SSE streaming via astream_events v2

Reuses the exact same agent runtime as the CLI, so conversation memory
(keyed by thread_id) and Langfuse tracing work identically here.

Run with: `make serve`  (then open http://localhost:8000/docs)
"""
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.agent import answer, astream_events_turn, init_graph_async
from app.schemas import ChatRequest, ChatResponse, HealthResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Opens the durable checkpointer on uvicorn's own event loop, once, at
    # startup — not lazily on first request. This matters beyond warming
    # the connection: /chat (a plain `def`, so FastAPI runs it in its own
    # threadpool — a different thread than uvicorn's loop) and /chat/stream
    # (`async def`, runs directly on uvicorn's loop) must share ONE
    # checkpointer bound to THIS loop, or the async path hits "bound to a
    # different event loop" — see app/agent.py's module docstring for the
    # full reasoning (verified empirically before writing that doc).
    await init_graph_async()
    yield


app = FastAPI(title="Core AI Stack Demo", version="1.0.0", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.get("/metrics")
def metrics_endpoint() -> Response:
    """Prometheus scrape target — see app/metrics.py for what's recorded."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    text = answer(req.message, req.thread_id)
    return ChatResponse(thread_id=req.thread_id, answer=text)


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Production SSE endpoint — streams typed events as they happen.

    Each line is a standard Server-Sent Event:
      data: {"type": "token",      "content": "Hello"}
      data: {"type": "tool_start", "tool": "search_docs", "args": {...}}
      data: {"type": "tool_end",   "tool": "search_docs"}
      data: {"type": "done"}

    The client can consume this with EventSource or any SSE library.
    Reuse `thread_id` across calls to keep conversation memory.
    """
    async def generate():
        async for event in astream_events_turn(req.message, req.thread_id):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering if behind a proxy
        },
    )

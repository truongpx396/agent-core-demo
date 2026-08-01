"""FastAPI service exposing the LangGraph agent over HTTP.

Endpoints:
- GET  /health -> liveness check
- POST /chat   -> run one agent turn (Pydantic-validated request/response)

Reuses the exact same agent runtime as the CLI, so conversation memory
(keyed by thread_id) and Langfuse tracing work identically here.

Run with: `make serve`  (then open http://localhost:8000/docs)
"""
from fastapi import FastAPI

from app.agent import answer
from app.schemas import ChatRequest, ChatResponse, HealthResponse

app = FastAPI(title="Core AI Stack Demo", version="1.0.0")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    text = answer(req.message, req.thread_id)
    return ChatResponse(thread_id=req.thread_id, answer=text)

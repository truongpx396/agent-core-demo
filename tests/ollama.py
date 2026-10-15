"""`GraphDeps` whose real chat client talks to a local Ollama container.

The live and deepeval tiers run the REAL model. They used to point the graph at it by
monkeypatching `CHAT_MODEL`/`OPENAI_API_BASE` onto a graph module; now the endpoint is an argument:
`GraphDeps.llm_factory` builds the tool-bound `ChatOpenAI` from it, exactly the way production
does, and `GraphDeps.model_alias` prices it. Anything else the test wants to override
(`search_docs=...`, ...) is passed through.

`endpoint` is the dict the `ollama_endpoint` / `deepeval_ollama` fixtures return.
"""
import functools

from app.agent.graph.build import GraphDeps
from app.agent.graph.utils import make_llm


def ollama_deps(endpoint: dict[str, str], **overrides) -> GraphDeps:
    return GraphDeps(
        model_alias=endpoint["model"],
        llm_factory=functools.partial(
            make_llm, model=endpoint["model"], base_url=endpoint["openai_api_base"]
        ),
        **overrides,
    )

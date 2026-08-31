"""LLM-judged RAG quality metrics against the real graph, via deepeval
(https://github.com/confident-ai/deepeval) — `@pytest.mark.deepeval`,
`make deepeval`. See GRAPH_PATTERNS.md pattern 48 for the full writeup.

This is a genuinely different kind of check from everything else in this
suite, not a smaller copy of one:
- promptfoo/support.yaml's assertions are deterministic (`icontains`/
  `not-icontains`) — no judgment call at all, just keyword presence.
- scripts/eval.py's `ungrounded_claims_count` is a structural heuristic —
  it counts citation markers actually used in the answer text against
  sentence count, never reads whether a cited CLAIM is actually true
  relative to the context it cites.
- test_prompt_injection_via_retrieval.py is a binary leak/no-leak keyword
  check on one adversarial input.
deepeval's FaithfulnessMetric/AnswerRelevancyMetric are semantic, LLM-judged
scores: does the answer's own claims actually follow from the retrieved
context (not just cite something), and does the answer actually address
the question asked (not just contain the right keywords)? No other tool
here asks that question.

DISCLOSED FINDING, verified directly by actually running this against the
same small `qwen2.5:1.5b` tests/live/ otherwise uses for CI speed (not
assumed from deepeval's own docs): the judge is unreliable at that size, in
the SAME way promptfoo's redteam grading was independently found to be
unreliable at a similarly small model (GRAPH_PATTERNS.md pattern 48's own
promptfoo-redteam finding). Concretely — a hand-verified GOOD answer
(faithful and relevant) scored `FaithfulnessMetric` 0.0 with a `reason`
field that read "There are no contradictions... both statements align
perfectly," a direct contradiction between the numeric score and the
judge's own stated verdict; `AnswerRelevancyMetric` scored a good, on-topic
answer and a deliberately bad, off-topic one THE SAME (0.5 for both),
failing to discriminate the one thing it exists to measure. This is why
this file, like `make promptfoo-redteam`/`make garak`/`make eval`, is
deliberately manual and never a CI gate — a low score from a small local
judge is not trustworthy evidence of a real regression, and a passing
score isn't trustworthy evidence of quality either. `DEEPEVAL_MODEL`
deliberately defaults to `qwen2.5:3b` (this app's own production
`CHAT_MODEL`, already what `make eval`/`make pull-models` use) rather than
`TEST_LLM_MODEL`'s CI-speed `1.5b` — a larger local judge is the one lever
actually available here to make the signal more trustworthy; still read
the `reason` fields by hand rather than trusting `assert_test`'s pass/fail
alone.
"""
import uuid

import pytest
from langchain_core.messages import HumanMessage

from app.agent import graph as graph_module
from app.agent.graph import GraphDeps, build_graph
from tests.conftest import TEST_CTX

pytestmark = pytest.mark.deepeval

_CONTEXT = (
    "[1] Acme Corp support hours are 9am to 5pm Monday through Friday, "
    "closed on weekends and public holidays.\n\n"
    "[2] Once a refund is approved by a human agent, it takes 3-5 business "
    "days to process."
)
_CITATIONS = [
    {
        "marker": "[1]",
        "doc_id": "support-hours-doc",
        "title": "support-hours",
        "text": "Acme Corp support hours are 9am to 5pm Monday through Friday.",
        "score": 0.9,
    },
    {
        "marker": "[2]",
        "doc_id": "refund-timing-doc",
        "title": "refund-timing",
        "text": "Refunds take 3-5 business days to process once approved.",
        "score": 0.85,
    },
]


def _real_search(query: str, ctx=None) -> tuple[str, list[dict]]:
    return _CONTEXT, _CITATIONS


@pytest.fixture(autouse=True)
def real_ollama_chat_model(monkeypatch, deepeval_ollama):
    monkeypatch.setattr(graph_module, "CHAT_MODEL", deepeval_ollama["model"])
    monkeypatch.setattr(graph_module, "OPENAI_API_BASE", deepeval_ollama["openai_api_base"])


def test_grounded_answer_is_faithful_and_relevant_by_a_real_llm_judge(deepeval_ollama):
    from deepeval import assert_test
    from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric
    from deepeval.models import OllamaModel
    from deepeval.test_case import LLMTestCase

    graph = build_graph(GraphDeps(search_docs=_real_search))
    config = {"configurable": {"thread_id": str(uuid.uuid4()), "ctx": TEST_CTX}}
    question = "What are Acme Corp's support hours, and how long do refunds take once approved?"

    result = graph.invoke(
        {"messages": [HumanMessage(content=question)]},
        config=config,
    )
    answer = result["messages"][-1].content

    judge = OllamaModel(
        model=deepeval_ollama["model"],
        base_url=deepeval_ollama["openai_api_base"].removesuffix("/v1"),
        temperature=0,
    )
    test_case = LLMTestCase(
        input=question,
        actual_output=answer,
        retrieval_context=[_CONTEXT],
    )
    assert_test(
        test_case,
        [
            FaithfulnessMetric(model=judge, threshold=0.5, include_reason=True),
            AnswerRelevancyMetric(model=judge, threshold=0.5, include_reason=True),
        ],
    )

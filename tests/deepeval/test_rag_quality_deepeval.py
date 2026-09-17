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
failing to discriminate the one thing it exists to measure.

JUDGE MODEL (2026-09-16): the judge is now `deepeval_judge`
(tests/deepeval/conftest.py, `DEEPEVAL_JUDGE_MODEL`, default Groq's
`openai/gpt-oss-120b`) — a SEPARATE knob from `deepeval_ollama`/
`DEEPEVAL_MODEL`, which still drives the TARGET (`graph_module.CHAT_MODEL`
below) and stays local. Bumping the target's own model to `qwen2.5:3b`
already helped some (see `DEEPEVAL_MODEL`'s own comment in conftest.py) but
still needed the manual/read-the-reason-by-hand caveat above; moving the
JUDGE specifically to a real 70B model is the same lever promptfoo's
`redteam.provider` already pulled (Gemini 3.1 Flash-Lite) for the identical
reason — a stronger grader is worth a deliberate, disclosed exception to
this suite's otherwise-local posture. `compound` (Groq's agentic
tool-using system) was deliberately NOT used here — it autonomously
invokes web search/code execution mid-request, a bad fit for a judge that
needs one predictable structured verdict, not an agentic loop.

CI WIRING (2026-09-16): `LLMTestCase(..., flaky=True)` below is deepeval's
own first-class mechanism for exactly this unreliability — confirmed
directly in the installed `deepeval==4.2.0` source
(`deepeval/evaluate/evaluate.py`'s `assert_test`): when a flaky test case's
metrics fail, it `warnings.warn(...)` instead of raising `AssertionError`,
so the pytest test itself still PASSES (visible in pytest's warning
summary) rather than turning CI red on judge noise. This is deliberately
NOT the same as `make promptfoo-redteam`/`make garak`, which stay fully
manual (GRAPH_PATTERNS.md pattern 48) — a genuine crash (a broken
`build_graph()`, an unreachable Ollama, an import error) still fails this
job for real, since `flaky` only swallows a failed METRIC, not an
exception. Still read the `reason` fields in the CI job's own log by hand —
`flaky=True` makes a bad score non-blocking, not meaningful on its own.

SEEDING (2026-09-16): every test below calls `seed_thread` before its
first `ainvoke` — see tests/seeding.py's own docstring for a real,
disclosed finding that affected every `build_graph().ainvoke()` caller in
this repo, this file included: without it, the target agent had ZERO
tool-routing guidance from `SYSTEM_PROMPT`, only each tool's own
individual docstring.

Two cases, not one, as of the same day: the original grounded case above
asks "is a claim the model DID make faithful to its context" — a second
case, `test_ungrounded_question_is_answered_without_hallucination`, asks
the complementary question neither it nor anything else in this suite
asked before: does the model correctly decline/hedge when asked something
the retrieved context doesn't cover at all, instead of inventing an
answer. Same two metrics, same judge, genuinely different scenario.
"""
import asyncio
import uuid

import pytest
from langchain_core.messages import HumanMessage

from app.agent import graph as graph_module
from app.agent.graph import GraphDeps
from app.agent.graph_build import build_graph
from tests.conftest import TEST_CTX
from tests.seeding import seed_thread

pytestmark = pytest.mark.deepeval

_CONTEXT = (
    "[1] Ecorp support hours are 9am to 5pm Monday through Friday, "
    "closed on weekends and public holidays.\n\n"
    "[2] Once a refund is approved by a human agent, it takes 3-5 business "
    "days to process."
)
_CITATIONS = [
    {
        "marker": "[1]",
        "doc_id": "support-hours-doc",
        "title": "support-hours",
        "text": "Ecorp support hours are 9am to 5pm Monday through Friday.",
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


async def _real_search(query: str, ctx=None) -> tuple[str, list[dict]]:
    return _CONTEXT, _CITATIONS


@pytest.fixture(autouse=True)
def real_ollama_chat_model(monkeypatch, deepeval_ollama):
    monkeypatch.setattr(graph_module, "CHAT_MODEL", deepeval_ollama["model"])
    monkeypatch.setattr(graph_module, "OPENAI_API_BASE", deepeval_ollama["openai_api_base"])


async def _seed_and_answer(question: str) -> str:
    """Shared by every test below: fresh graph/thread, seed it properly
    (see tests/seeding.py's own `seed_thread` docstring — without
    this, `build_graph().ainvoke()` never triggers the SYSTEM_PROMPT
    seeding every production path relies on), run one turn, return the
    final answer text."""
    graph = build_graph(GraphDeps(search_docs=_real_search))
    config = {"configurable": {"thread_id": str(uuid.uuid4()), "ctx": TEST_CTX}}
    await seed_thread(graph, config["configurable"]["thread_id"])
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=question)]},
        config=config,
    )
    return result["messages"][-1].content


def test_grounded_answer_is_faithful_and_relevant_by_a_real_llm_judge(deepeval_ollama, deepeval_judge):
    from deepeval import assert_test
    from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric
    from deepeval.test_case import LLMTestCase

    question = "What are Ecorp's support hours, and how long do refunds take once approved?"
    # asyncio.run(...), not the sync .invoke() this used to be — see
    # app/agent/graph.py: agent/retrieve_context/etc. are async def now.
    answer = asyncio.run(_seed_and_answer(question))

    judge = deepeval_judge
    test_case = LLMTestCase(
        input=question,
        actual_output=answer,
        retrieval_context=[_CONTEXT],
        flaky=True,
    )
    assert_test(
        test_case,
        [
            FaithfulnessMetric(model=judge, threshold=0.5, include_reason=True),
            AnswerRelevancyMetric(model=judge, threshold=0.5, include_reason=True),
        ],
    )


def test_ungrounded_question_is_answered_without_hallucination(deepeval_ollama, deepeval_judge):
    """A genuinely different question from the grounded case above: not
    "is a supported claim faithful to its context," but "does the model
    correctly decline/hedge instead of inventing an answer when the
    retrieved context doesn't cover the question at all." `_CONTEXT` only
    ever covers support hours and refund timing — deliberately nothing
    about revenue — so a faithful answer has no real number to report and
    must say so, not invent one. `FaithfulnessMetric` still applies
    cleanly here: a claim like "I don't have that information" is TRUE
    relative to the context (which indeed doesn't have it), so a correct
    abstention should score well, not just "not fail" — this is a positive
    check that abstention is handled properly, not merely the absence of a
    number in the output."""
    from deepeval import assert_test
    from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric
    from deepeval.test_case import LLMTestCase

    question = "What was Ecorp's total revenue last year?"
    answer = asyncio.run(_seed_and_answer(question))

    judge = deepeval_judge
    test_case = LLMTestCase(
        input=question,
        actual_output=answer,
        retrieval_context=[_CONTEXT],
        flaky=True,
    )
    assert_test(
        test_case,
        [
            FaithfulnessMetric(model=judge, threshold=0.5, include_reason=True),
            AnswerRelevancyMetric(model=judge, threshold=0.5, include_reason=True),
        ],
    )

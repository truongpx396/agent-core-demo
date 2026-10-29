"""LLM-judged RAG quality metrics against the real graph, via deepeval (`@pytest.mark.deepeval`, `make deepeval`;
GRAPH_PATTERNS.md pattern 48).

A different kind of check, not a smaller copy of another: promptfoo/support.yaml asserts keyword presence
(deterministic); scripts/eval.py's `ungrounded_claims_count` counts citation markers against sentences and
never reads whether a cited CLAIM is true; test_prompt_injection_via_retrieval.py is a binary leak check on
one adversarial input. deepeval's FaithfulnessMetric/AnswerRelevancyMetric are semantic and LLM-judged: do the
answer's claims follow from the retrieved context, and does it address the question asked?

Disclosed finding, verified against the small `qwen2.5:1.5b` that tests/live/ uses for CI speed: a judge that
small is unreliable (as with promptfoo's redteam grading, pattern 48). A hand-verified GOOD answer scored
`FaithfulnessMetric` 0.0 with a `reason` saying "no contradictions... both statements align perfectly", and
`AnswerRelevancyMetric` scored a good answer and a deliberately off-topic one the same (0.5).

Judge: `deepeval_judge` (tests/deepeval/conftest.py, `DEEPEVAL_JUDGE_MODEL`, default `gemini-3.1-flash-lite`),
a SEPARATE knob from `deepeval_ollama`/`DEEPEVAL_MODEL`, which drives the TARGET (`graph_module.CHAT_MODEL`)
and stays local. A bigger target (`qwen2.5:3b`) helped some but still needed reading reasons by hand; moving
the JUDGE to a hosted model is the lever promptfoo's `redteam.provider` pulled, a deliberate, disclosed
exception to the suite's local posture. It uses the same model and key as `redteam.provider`, one shared
exception instead of two.

CI: `LLMTestCase(..., flaky=True)` is deepeval's mechanism for this: a flaky case's failed metrics
`warnings.warn` instead of raising `AssertionError` (verified in `deepeval==4.2.0` `assert_test`), so judge
noise doesn't turn CI red. This is not the manual `make promptfoo-redteam`/`make garak` (pattern 48): a
genuine crash (broken `build_graph()`, unreachable Ollama, import error) still fails the job, since `flaky`
swallows only a failed METRIC. Still read the `reason` fields by hand: `flaky=True` makes a bad score
non-blocking, not meaningful.

No system prompt needs seeding: the agent node puts the domain's prompt first on every call, so a graph built
directly is never un-guided (an earlier version ran that way for weeks unnoticed).

Two cases: the grounded one asks whether a claim the model DID make is faithful to its context;
`test_ungrounded_question_is_answered_without_hallucination` asks the complement: does the model decline or
hedge on what the context doesn't cover, rather than inventing an answer? Same metrics and judge.
"""
import uuid

import pytest
from langchain_core.messages import HumanMessage

from tests.conftest import TEST_CTX
from tests.ecorp import build_graph
from tests.ollama import ollama_deps

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


async def _answer(endpoint: dict, question: str) -> str:
    """Shared by every test below: fresh graph/thread, run one turn, return the final answer
    text."""
    graph = build_graph(ollama_deps(endpoint, search_docs=_real_search))
    config = {"configurable": {"thread_id": str(uuid.uuid4()), "ctx": TEST_CTX}}
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=question)]},
        config=config,
    )
    return result["messages"][-1].content


async def test_grounded_answer_is_faithful_and_relevant_by_a_real_llm_judge(deepeval_ollama, deepeval_judge):
    from deepeval import assert_test
    from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric
    from deepeval.test_case import LLMTestCase

    question = "What are Ecorp's support hours, and how long do refunds take once approved?"
    # asyncio.run(...), not the sync .invoke() this used to be — see
    # app/agent/graph/core.py: agent/retrieve_context/etc. are async def now.
    answer = await _answer(deepeval_ollama, question)

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


async def test_ungrounded_question_is_answered_without_hallucination(deepeval_ollama, deepeval_judge):
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
    answer = await _answer(deepeval_ollama, question)

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

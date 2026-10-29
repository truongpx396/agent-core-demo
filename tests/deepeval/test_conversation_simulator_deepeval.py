"""Multi-turn conversation quality against the real graph, via deepeval's `ConversationSimulator`
(`@pytest.mark.deepeval`, `make deepeval`; GRAPH_PATTERNS.md pattern 48).

Unlike `test_rag_quality_deepeval.py` (one isolated turn), a simulated persona (role-played by the SAME judge
model) drives a multi-turn conversation against the REAL checkpointed graph with one stable `thread_id`, so
LangGraph's real checkpointer carries state as in a Telegram/API conversation. Only this catches cross-turn
failures: does the assistant stay in character (`RoleAdherenceMetric`) and recall what was established
(`KnowledgeRetentionMetric`)?

Two real, disclosed findings:

1. **A naive `model_callback` breaks when the simulated user triggers a MUTATING tool call.** `messages[-1]`
   is then the tool-calling `AIMessage` with legitimately empty `.content` (the `human_approval` interrupt,
   pattern 36, paused the run). Reproduced 3/3 with `qwen2.5:3b` calling `remember`. `model_callback` checks
   `graph.aget_state(config).next` and auto-approves (`Command(resume=True)`) before reading the answer. This
   is a real requirement for ANY external harness driving this graph across turns, not a deepeval workaround.
2. **The target model adopted a customer's FALSE premise instead of correcting it from its own context.** The
   persona asserted support hours were "Monday and Thursday afternoons" (`_CONTEXT` says Monday-Friday
   9am-5pm); `qwen2.5:3b` affirmed it, across runs. Unlike the adversarial injection defense in
   `test_prompt_injection_via_retrieval.py` (which held), this is an ordinary leading question the model got
   wrong. Not remediated here; disclosed so it isn't lost.

The small-judge caveat of pattern 48 applies again: `RoleAdherenceMetric` scored a verbose-but-on-topic
response 0.67/PASS while its `reason` read like a critical review, and `KnowledgeRetentionMetric` scored 0.0
with a directionally coherent `reason`. Not a clean pass/fail signal; read the reasons by hand.

Judge: `deepeval_conversation_judge` (tests/deepeval/conftest.py), Groq's `openai/gpt-oss-120b`
(`GROQ_API_KEY`). Unlike `test_rag_quality_deepeval.py` this file did NOT move to Gemini:
`KnowledgeRetentionMetric` requests a Dict-typed structured-output schema that Gemini's `response_schema`
rejects (CI run 35230147154, PR #41). It plays both roles, simulating the customer (`simulator_model=judge`)
and grading the two metrics.

CI: `test_case.flaky = True` as in `test_rag_quality_deepeval.py`: `assert_test` warns instead of raising on a
failed metric (verified in `deepeval==4.2.0`). A genuine crash (the interrupt-unaware callback, the
simulator's API) still fails the job, since `flaky` swallows only a failed METRIC score.

No system prompt needs seeding: the agent node puts the domain's prompt first on every call, so a graph built
directly is never un-guided (an earlier version of this suite ran that way for weeks before it was noticed).

Two scenarios share `_make_model_callback` (the `human_approval` logic): one stresses
`KnowledgeRetentionMetric` (recall of its own earlier answer across a follow-up);
`test_conversation_resists_off_topic_persona_pressure` stresses `RoleAdherenceMetric` (staying a Tier-1
support copilot when the persona steers into an unrelated request after a real answer).
"""

import pytest
from langchain_core.messages import HumanMessage
from langgraph.types import Command

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

# How many human_approval interrupts one turn will auto-resume through
# before giving up — a real turn only ever needs 0 or 1 (see finding #1
# above); this is just a safety cap against an unexpected infinite pause.
_MAX_APPROVAL_ROUNDS = 3


async def _real_search(query: str, ctx=None) -> tuple[str, list[dict]]:
    return _CONTEXT, _CITATIONS


def _make_model_callback(graph):
    """Shared by every scenario below — see this file's own docstring
    (finding #1) for why the human_approval-interrupt handling is
    required at all."""
    from deepeval.test_case import Turn

    async def model_callback(input: str, thread_id: str) -> Turn:
        config = {"configurable": {"thread_id": thread_id, "ctx": TEST_CTX}}
        result = await graph.ainvoke({"messages": [HumanMessage(content=input)]}, config=config)
        for _ in range(_MAX_APPROVAL_ROUNDS):
            if not (await graph.aget_state(config)).next:
                break
            result = await graph.ainvoke(Command(resume=True), config=config)
        return Turn(role="assistant", content=result["messages"][-1].content)

    return model_callback


def test_multiturn_conversation_stays_grounded_and_in_role(deepeval_ollama, deepeval_conversation_judge):
    from deepeval import assert_test
    from deepeval.dataset import ConversationalGolden, Persona
    from deepeval.metrics import KnowledgeRetentionMetric, RoleAdherenceMetric
    from deepeval.simulator import ConversationSimulator

    graph = build_graph(ollama_deps(deepeval_ollama, search_docs=_real_search))
    model_callback = _make_model_callback(graph)

    # Same `judge` object for BOTH roles below (simulating the persona's
    # turns AND grading the metrics) — preserves this file's own original
    # double-duty shape, just pointed at deepeval_conversation_judge (Groq)
    # instead of the local target model; see tests/deepeval/conftest.py's
    # DEEPEVAL_CONVERSATION_JUDGE_MODEL comment for why this file needs its
    # own separate judge fixture rather than sharing deepeval_judge
    # (Gemini) with the other two deepeval files.
    judge = deepeval_conversation_judge

    golden = ConversationalGolden(
        scenario=(
            "A customer first asks about Ecorp's support hours, then "
            "follows up a moment later asking whether a refund would still "
            "be processed in time given those hours."
        ),
        expected_outcome=(
            "The assistant answers the support-hours question directly "
            "using the knowledge base, then in the follow-up correctly "
            "reuses/recalls that same support-hours answer rather than "
            "contradicting itself or claiming to have no memory of the "
            "earlier turn."
        ),
        persona=Persona(
            characteristics=(
                "A mildly impatient existing customer who references what "
                "the assistant just told them."
            )
        ),
    )

    simulator = ConversationSimulator(
        model_callback=model_callback,
        simulator_model=judge,
        max_concurrent=1,
        async_mode=False,
    )
    test_cases = simulator.simulate(conversational_goldens=[golden], max_user_simulations=3)
    test_case = test_cases[0]
    test_case.chatbot_role = "A Tier-1 customer support copilot for Ecorp"
    test_case.flaky = True

    assert_test(
        test_case,
        [
            RoleAdherenceMetric(model=judge, threshold=0.5, include_reason=True),
            KnowledgeRetentionMetric(model=judge, threshold=0.5, include_reason=True),
        ],
    )


def test_conversation_resists_off_topic_persona_pressure(deepeval_ollama, deepeval_conversation_judge):
    """A genuinely different stress from the scenario above, not a second
    copy of it: that one tests whether the assistant RECALLS its own
    earlier answer across turns (knowledge retention, on-topic the whole
    way through). This one tests whether it STAYS a Tier-1 support
    copilot when the persona tries to steer it somewhere else entirely —
    a real support-hours question first (so there's a real answer to stay
    grounded on), then a request with nothing to do with Ecorp support at
    all. `RoleAdherenceMetric` is the metric this scenario is actually
    built to stress; `KnowledgeRetentionMetric` still applies since the
    first answer is still something a later turn could contradict."""
    from deepeval import assert_test
    from deepeval.dataset import ConversationalGolden, Persona
    from deepeval.metrics import KnowledgeRetentionMetric, RoleAdherenceMetric
    from deepeval.simulator import ConversationSimulator

    graph = build_graph(ollama_deps(deepeval_ollama, search_docs=_real_search))
    model_callback = _make_model_callback(graph)
    judge = deepeval_conversation_judge

    golden = ConversationalGolden(
        scenario=(
            "A customer first asks about Ecorp's support hours, gets a "
            "real answer, then — in the same conversation — asks the "
            "assistant to do something with nothing to do with Ecorp "
            "support at all, like writing a short poem or answering an "
            "unrelated general-knowledge trivia question."
        ),
        expected_outcome=(
            "The assistant answers the support-hours question directly "
            "using the knowledge base, then politely declines or redirects "
            "the unrelated request instead of complying with it or "
            "otherwise abandoning its role as a Tier-1 support copilot for "
            "the rest of the conversation."
        ),
        persona=Persona(
            characteristics=(
                "A customer who, after getting a real answer, casually "
                "tries to use the assistant for something unrelated to see "
                "what it will do."
            )
        ),
    )

    simulator = ConversationSimulator(
        model_callback=model_callback,
        simulator_model=judge,
        max_concurrent=1,
        async_mode=False,
    )
    test_cases = simulator.simulate(conversational_goldens=[golden], max_user_simulations=3)
    test_case = test_cases[0]
    test_case.chatbot_role = "A Tier-1 customer support copilot for Ecorp"
    test_case.flaky = True

    assert_test(
        test_case,
        [
            RoleAdherenceMetric(model=judge, threshold=0.5, include_reason=True),
            KnowledgeRetentionMetric(model=judge, threshold=0.5, include_reason=True),
        ],
    )

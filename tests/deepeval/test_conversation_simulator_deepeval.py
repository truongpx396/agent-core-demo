"""Multi-turn conversation quality against the real graph, via deepeval's `ConversationSimulator`
(`@pytest.mark.deepeval`, `make deepeval`; GRAPH_PATTERNS.md pattern 48).

Distinct from `test_rag_quality_deepeval.py` (one isolated turn) and every other tier: a simulated persona
(deepeval's `ConversationSimulator`, role-playing a customer via the SAME judge model) drives a multi-turn
conversation against this app's REAL, checkpointed graph, with one stable `thread_id` across turns so
LangGraph's real checkpointer (not a mock) carries state as in a real Telegram/API conversation. Only this
catches cross-turn failures: does the assistant stay in character over several turns (`RoleAdherenceMetric`),
and recall what was already established (`KnowledgeRetentionMetric`)?

TWO real, disclosed findings from running this:

1. **A naive `model_callback` breaks the instant the simulated user's message causes a MUTATING tool call.**
   `asyncio.run(graph.ainvoke())`'s returned `messages[-1]` is then the tool-calling `AIMessage` itself, with
   legitimately empty `.content` (the `human_approval` interrupt, pattern 36, paused the run; the real answer
   doesn't exist yet). Reproduced 3/3 with `qwen2.5:3b` calling `remember` mid-conversation. `model_callback`
   checks `asyncio.run(graph.aget_state(config)).next` and auto-approves (`Command(resume=True)`) before
   reading the final answer. This is a real integration requirement for ANY external harness driving this
   graph across turns, not a deepeval workaround.

2. **The target model adopted a customer's FALSE premise instead of correcting it against its own retrieved
   context**, a target-model finding, not a judge artifact. The persona asserted Ecorp's support hours were
   "Monday and Thursday afternoons" (nowhere in `_CONTEXT`, which says Monday-Friday 9am-5pm); the real
   `qwen2.5:3b` affirmed that schedule ("it would be processed according to their usual schedule of Monday and
   Thursday afternoons") though its grounded context contradicted it. Reproduced across runs. Distinct from
   `test_prompt_injection_via_retrieval.py`'s adversarial-injection defense (which held): an ordinary,
   non-adversarial leading question the model still got wrong. Not remediated here (out of scope for adding
   deepeval); disclosed so it isn't lost.

The small-local-judge caveat (pattern 48's promptfoo-redteam and single-turn-deepeval findings) applies a
third time, with a third grading mechanism: `RoleAdherenceMetric` scored a verbose-but-on-topic response
0.67/PASS while its `reason` read like a critical review, and `KnowledgeRetentionMetric` scored 0.0 with a
`reason` at least directionally coherent (unlike the flat self-contradictions in the single-turn probe). Not a
clean pass/fail signal; read the reasons by hand.

JUDGE MODEL: `judge` below is `deepeval_conversation_judge` (tests/deepeval/conftest.py), Groq's
`openai/gpt-oss-120b`, the same target/judge split `test_rag_quality_deepeval.py` got (see its JUDGE MODEL
paragraph). Unlike that file, this one did NOT move to Gemini on 2026-09-17: run 35230147154 (PR #41) hit
`google.genai.errors.ClientError: 400 INVALID_ARGUMENT ... "additional_properties"` on both tests, because
`KnowledgeRetentionMetric` unconditionally requests a Dict-typed structured-output schema
(`deepeval.metrics.knowledge_retention.schema.Knowledge.data`) and Gemini's `response_schema` doesn't support
that JSON Schema keyword for any Gemini model (confirmed in deepeval's schema class). So this file keeps its
own `deepeval_conversation_judge` (`GROQ_API_KEY`); see tests/deepeval/conftest.py. It serves BOTH of
`judge`'s roles here, simulating the customer (`simulator_model=judge`) and grading the two metrics, as this
file always reused one model for both.

CI WIRING: `test_case.flaky = True` is the mechanism `test_rag_quality_deepeval.py` uses (see its docstring:
`assert_test` does `warnings.warn` instead of `AssertionError` on a failed metric, verified in the installed
`deepeval==4.2.0`). A genuine crash in this file's integration findings (the interrupt-unaware
`model_callback`, the ConversationSimulator's own API) still fails the CI job: `flaky` swallows only a failed
METRIC score, never an exception.

The system prompt needs no seeding here: the agent node puts the domain's prompt first on every call, so a
graph built directly is never an un-guided agent (an earlier version of this suite ran exactly that, for
weeks, before it was noticed).

Two scenarios, sharing `_make_model_callback` rather than duplicating its human_approval logic: the original
stresses `KnowledgeRetentionMetric` (does the assistant recall its own earlier answer across an on-topic
follow-up); `test_conversation_resists_off_topic_persona_pressure` stresses `RoleAdherenceMetric` from another
angle (does it stay a Tier-1 support copilot when the persona tries to steer it into an unrelated request
after a real answer, rather than staying on-topic because the conversation never left the topic).
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

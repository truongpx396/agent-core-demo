"""Multi-turn conversation quality against the real graph, via deepeval's
`ConversationSimulator` — `@pytest.mark.deepeval`, `make deepeval`. See
GRAPH_PATTERNS.md pattern 48 for the full writeup.

Distinct from `test_rag_quality_deepeval.py` (single isolated turn) and
every other tier in this suite: a simulated persona (deepeval's own
`ConversationSimulator`, role-playing a customer via the SAME judge model)
drives a multi-turn conversation against this app's REAL, checkpointed
graph — one stable `thread_id` across every turn, so LangGraph's real
checkpointer (not a mock) carries state exactly the way a real Telegram/API
conversation would. Only this can catch cross-turn failures: does the
assistant stay in character over several turns (`RoleAdherenceMetric`), does
it recall/stay consistent with what was already established
(`KnowledgeRetentionMetric`)?

TWO real, disclosed findings from actually running this, not assumed from
deepeval's docs:

1. **A naive `model_callback` breaks the instant the simulated user's
   message causes a MUTATING tool call.** `asyncio.run(graph.ainvoke())`'s returned
   `messages[-1]` at that point is the tool-calling `AIMessage` itself —
   legitimately empty `.content` (LangGraph's `human_approval` interrupt,
   pattern 36, has paused the run for approval; the real answer doesn't
   exist yet). Reproduced 3/3 with `qwen2.5:3b` calling `remember`
   mid-conversation instead of answering directly. `model_callback` below
   checks `asyncio.run(graph.aget_state(config)).next` and auto-approves
   (`Command(resume=True)`) before reading the final answer — this is a
   real integration requirement for ANY external harness driving this
   graph across turns, not a deepeval-specific workaround.

2. **The target model itself uncritically adopted a customer's FALSE
   premise instead of correcting it against its own retrieved context** —
   a real target-model finding, not a judge artifact. The simulated
   persona asserted Ecorp's support hours were "Monday and Thursday
   afternoons" (nowhere in `_CONTEXT`, which says Monday-Friday 9am-5pm);
   the real `qwen2.5:3b` answer affirmed that fabricated schedule back
   ("it would be processed according to their usual schedule of Monday and
   Thursday afternoons") instead of citing its own grounded context, which
   directly contradicted it. Reproduced across runs. Distinct from
   `test_prompt_injection_via_retrieval.py`'s adversarial-injection defense
   (which held) — this is an ordinary, non-adversarial leading question the
   model still got wrong. Not remediated here (out of scope for adding
   deepeval) — disclosed so it isn't lost.

The now-familiar small-local-judge caveat (GRAPH_PATTERNS.md pattern 48's
promptfoo-redteam and single-turn-deepeval findings) applied here too, a
third time, with a third distinct grading mechanism: `RoleAdherenceMetric`
scored a verbose-but-on-topic response 0.67/PASS while its own `reason`
read like a critical review, and `KnowledgeRetentionMetric` scored 0.0 with
a `reason` that was at least directionally coherent this time (unlike the
flat self-contradictions seen in the single-turn probe) — still not
something to trust as a clean pass/fail signal. Read the reasons by hand.

JUDGE MODEL (2026-09-16): `judge` below is now `deepeval_judge`
(tests/live/conftest.py, Groq's `openai/gpt-oss-120b` by default),
the same target/judge split `test_rag_quality_deepeval.py` got — see that
file's own JUDGE MODEL paragraph for the full reasoning (a stronger
grader, `compound` deliberately avoided as agentic/unpredictable for a
verdict-only role). Applies to BOTH of `judge`'s roles in this file:
simulating the customer persona (`simulator_model=judge` below) and
grading the two metrics — this file already reused one model for both
roles before this change, so the fix preserves that shape rather than
introducing a third separate knob nobody asked for.

CI WIRING (2026-09-16): `test_case.flaky = True` below is the same
mechanism `test_rag_quality_deepeval.py` uses — see that file's own
docstring for how `assert_test` actually handles it (`warnings.warn`
instead of `AssertionError` on a failed metric, verified directly in the
installed `deepeval==4.2.0` source). A genuine crash anywhere in this
file's own real integration findings above (the interrupt-unaware
`model_callback`, the ConversationSimulator's own real API) still fails
the CI job for real — `flaky` only swallows a failed METRIC score, never
an exception.

SEEDING (2026-09-16): `model_callback` now seeds the system prompt before
each turn — see tests/live/conftest.py's own docstring for a real,
disclosed finding that affected every `build_graph().ainvoke()` caller in
this repo, this file included: without it, the target agent had ZERO
tool-routing guidance from `SYSTEM_PROMPT`, only each tool's own
individual docstring, for the whole simulated conversation.

Two scenarios, not one, as of the same day, sharing `_make_model_callback`
rather than duplicating its human_approval/seeding logic per test: the
original scenario above stresses `KnowledgeRetentionMetric` (does the
assistant recall its own earlier answer across an on-topic follow-up);
`test_conversation_resists_off_topic_persona_pressure` stresses
`RoleAdherenceMetric` from a different angle — does the assistant stay a
Tier-1 support copilot when the persona tries to steer it into an
unrelated request after a real answer, rather than just staying on-topic
because the conversation itself never left the topic.
"""
import asyncio

import pytest
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from app.agent import graph as graph_module
from app.agent.graph import GraphDeps
from app.agent.graph_build import build_graph
from tests.conftest import TEST_CTX
from tests.live.conftest import seed_thread

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


def _real_search(query: str, ctx=None) -> tuple[str, list[dict]]:
    return _CONTEXT, _CITATIONS


@pytest.fixture(autouse=True)
def real_ollama_chat_model(monkeypatch, deepeval_ollama):
    monkeypatch.setattr(graph_module, "CHAT_MODEL", deepeval_ollama["model"])
    monkeypatch.setattr(graph_module, "OPENAI_API_BASE", deepeval_ollama["openai_api_base"])


def _make_model_callback(graph):
    """Shared by every scenario below — see this file's own docstring
    (finding #1) for why the human_approval-interrupt handling is
    required at all, and tests/live/conftest.py's `seed_thread` docstring
    for why seeding is required before the first real turn on a thread.
    `seed_thread` is called on EVERY turn, not just the first: it's
    idempotent/cheap (checks an in-process set, then the thread's actual
    persisted state) and `model_callback` has no other natural "first
    call" hook to hang this on, since `ConversationSimulator` owns the
    turn loop, not this function."""
    from deepeval.test_case import Turn

    async def _seed_and_invoke(config, human_content):
        await seed_thread(graph, config["configurable"]["thread_id"])
        return await graph.ainvoke({"messages": [HumanMessage(content=human_content)]}, config=config)

    def model_callback(input: str, thread_id: str) -> Turn:
        config = {"configurable": {"thread_id": thread_id, "ctx": TEST_CTX}}
        result = asyncio.run(_seed_and_invoke(config, input))
        for _ in range(_MAX_APPROVAL_ROUNDS):
            if not asyncio.run(graph.aget_state(config)).next:
                break
            result = asyncio.run(graph.ainvoke(Command(resume=True), config=config))
        return Turn(role="assistant", content=result["messages"][-1].content)

    return model_callback


def test_multiturn_conversation_stays_grounded_and_in_role(deepeval_ollama, deepeval_judge):
    from deepeval import assert_test
    from deepeval.dataset import ConversationalGolden, Persona
    from deepeval.metrics import KnowledgeRetentionMetric, RoleAdherenceMetric
    from deepeval.simulator import ConversationSimulator

    graph = build_graph(GraphDeps(search_docs=_real_search))
    model_callback = _make_model_callback(graph)

    # Same `judge` object for BOTH roles below (simulating the persona's
    # turns AND grading the metrics) — preserves this file's own original
    # double-duty shape, just pointed at deepeval_judge (Groq) instead of
    # the local target model; see tests/live/conftest.py's
    # DEEPEVAL_JUDGE_MODEL comment for why this is a separate knob from the
    # target now, not a new third role.
    judge = deepeval_judge

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


def test_conversation_resists_off_topic_persona_pressure(deepeval_ollama, deepeval_judge):
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

    graph = build_graph(GraphDeps(search_docs=_real_search))
    model_callback = _make_model_callback(graph)
    judge = deepeval_judge

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

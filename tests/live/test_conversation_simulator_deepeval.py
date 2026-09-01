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
   message causes a MUTATING tool call.** `graph.invoke()`'s returned
   `messages[-1]` at that point is the tool-calling `AIMessage` itself —
   legitimately empty `.content` (LangGraph's `human_approval` interrupt,
   pattern 36, has paused the run for approval; the real answer doesn't
   exist yet). Reproduced 3/3 with `qwen2.5:3b` calling `remember`
   mid-conversation instead of answering directly. `model_callback` below
   checks `graph.get_state(config).next` and auto-approves
   (`Command(resume=True)`) before reading the final answer — this is a
   real integration requirement for ANY external harness driving this
   graph across turns, not a deepeval-specific workaround.

2. **The target model itself uncritically adopted a customer's FALSE
   premise instead of correcting it against its own retrieved context** —
   a real target-model finding, not a judge artifact. The simulated
   persona asserted Acme Corp's support hours were "Monday and Thursday
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
promptfoo-redteam and single-turn-deepeval findings) applies here too, a
third time, with a third distinct grading mechanism: `RoleAdherenceMetric`
scored a verbose-but-on-topic response 0.67/PASS while its own `reason`
read like a critical review, and `KnowledgeRetentionMetric` scored 0.0 with
a `reason` that was at least directionally coherent this time (unlike the
flat self-contradictions seen in the single-turn probe) — still not
something to trust as a clean pass/fail signal. Read the reasons by hand.
"""
import pytest
from langchain_core.messages import HumanMessage
from langgraph.types import Command

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


def test_multiturn_conversation_stays_grounded_and_in_role(deepeval_ollama):
    from deepeval import assert_test
    from deepeval.dataset import ConversationalGolden, Persona
    from deepeval.metrics import KnowledgeRetentionMetric, RoleAdherenceMetric
    from deepeval.models import OllamaModel
    from deepeval.simulator import ConversationSimulator
    from deepeval.test_case import Turn

    graph = build_graph(GraphDeps(search_docs=_real_search))

    def model_callback(input: str, thread_id: str) -> Turn:
        config = {"configurable": {"thread_id": thread_id, "ctx": TEST_CTX}}
        result = graph.invoke({"messages": [HumanMessage(content=input)]}, config=config)
        for _ in range(_MAX_APPROVAL_ROUNDS):
            if not graph.get_state(config).next:
                break
            result = graph.invoke(Command(resume=True), config=config)
        return Turn(role="assistant", content=result["messages"][-1].content)

    judge = OllamaModel(
        model=deepeval_ollama["model"],
        base_url=deepeval_ollama["openai_api_base"].removesuffix("/v1"),
        temperature=0,
    )

    golden = ConversationalGolden(
        scenario=(
            "A customer first asks about Acme Corp's support hours, then "
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
    test_case.chatbot_role = "A Tier-1 customer support copilot for Acme Corp"

    assert_test(
        test_case,
        [
            RoleAdherenceMetric(model=judge, threshold=0.5, include_reason=True),
            KnowledgeRetentionMetric(model=judge, threshold=0.5, include_reason=True),
        ],
    )

"""When history compaction fires, the summary and the retrieved context still go BEFORE the question
(in the same message since pattern 79: as text ahead of what the person wrote).

`compact_history` runs just before `retrieve_context`, and everything a node returns is appended to
`messages`, so the compaction breadcrumb lands AFTER the turn's question. `retrieve_context`
recorded its anchor as `len(messages) - 1`, "the question is the last message", which is the
breadcrumb in that case. The model was then sent the question ahead of the summary and the context instead of
behind them, and the retrieval query's "previous turn" lookup (everything before
the anchor) included the current question. Found while verifying #169; it predates the work in
#168-#172. (GRAPH_PATTERNS.md pattern 41.)
"""
import uuid

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph.build import GraphDeps
from app.agent.graph.messages import COMPACTION_MARKER_KEY
from app.agent.graph.retrieval import make_retrieve_context_node
from app.agent.manifest import AgentManifest
from tests.agent.fakes import cfg, scripted_llm
from tests.ecorp import build_graph

BREADCRUMB = SystemMessage(
    content="[2 earlier turns summarized to keep this conversation within budget.]",
    additional_kwargs={COMPACTION_MARKER_KEY: True},
)


async def test_the_anchor_is_the_question_not_a_breadcrumb_appended_after_it():
    queries = []

    async def search(query, ctx):
        queries.append(query)
        return "[1] REFUND POLICY TEXT", [{"marker": "[1]", "text": "REFUND POLICY TEXT"}]

    retrieve_context = make_retrieve_context_node(search)
    messages = [
        HumanMessage(content="what is the refund policy for annual plans", id="h0"),
        AIMessage(content="Refunds are available within thirty days of purchase.", id="a0"),
        HumanMessage(content="more detail pls", id="h1"),
        BREADCRUMB,  # compact_history ran first, and what it returns is appended
    ]

    result = await retrieve_context({"messages": messages})

    assert result["context_anchor_index"] == 2
    # The vague follow-up is enriched with the PREVIOUS turn's question. With the anchor on the
    # breadcrumb, "before the anchor" included the current question, so it was enriched with itself.
    assert "refund policy" in queries[0]


async def test_the_model_is_sent_the_summary_and_context_before_the_question_after_a_compaction():
    seen = []

    def respond(messages):
        if "Turns to summarize" in str(messages[-1].content):
            return AIMessage(content="SUMMARY OF EARLIER TURNS")
        return AIMessage(content="The refund policy says so, as cited [1].")

    async def search(query, ctx):
        return "[1] REFUND POLICY TEXT", [{"marker": "[1]", "text": "REFUND POLICY TEXT"}]

    saver, config = MemorySaver(), cfg(str(uuid.uuid4()))
    g = build_graph(
        GraphDeps(llm=scripted_llm(respond, seen), search_docs=search),
        checkpointer=saver,
        manifest=AgentManifest("x", "You are the prompt."),
        history_token_ceiling=300,
        history_token_floor=100,
    )
    history = []
    for i in range(6):
        history.append(HumanMessage(content=f"an earlier question number {i}, " + "with plenty of words " * 8, id=f"h{i}"))
        history.append(AIMessage(content=f"an earlier answer number {i}, " + "with plenty of words " * 8, id=f"a{i}"))
    await g.aupdate_state(config, {"messages": history})

    await g.ainvoke({"messages": [HumanMessage(content="what is the refund policy")]}, config=config)

    compacted = [m for m in (await g.aget_state(config)).values["messages"] if getattr(m, "additional_kwargs", {}).get(COMPACTION_MARKER_KEY)]
    assert compacted, "test setup: compaction never fired, so this proves nothing"
    # The agent's call: the calls before it are the compaction's own, the one after it the follow-ups'.
    call_ = next(c for c in seen if c[0].content == "You are the prompt." and any("REFUND POLICY TEXT" in str(m.content) for m in c))
    sent = next(m for m in call_ if "REFUND POLICY TEXT" in str(m.content))
    assert isinstance(sent, HumanMessage), "the context rides on the question, not on a system message (pattern 79)"
    text = str(sent.content)
    summary_at = text.index("SUMMARY OF EARLIER TURNS")
    context_at = text.index("REFUND POLICY TEXT")
    question_at = text.index("what is the refund policy")
    assert summary_at < context_at < question_at

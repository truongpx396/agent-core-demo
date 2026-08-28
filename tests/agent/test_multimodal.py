"""Tests for multimodal/image-question support (GRAPH_PATTERNS.md pattern
44): app/agent/graph.py's `_human_text`/`_human_has_content` (extracting the text
portion of a possibly-multimodal HumanMessage for every downstream
text-only consumer — moderation, the semantic cache key, the retrieval
query), app/agent/runtime.py's `_build_human_content` (constructing the multimodal
content list actually sent to the model), and the routing/node-level call
sites that had to switch from `.content` to these helpers.
"""
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.agent import graph
from app.agent import runtime as agent_module
from app.agent.graph import _human_has_content, _human_text, route_after_validation
from tests.conftest import TEST_CTX


def _multimodal(text, *image_urls):
    parts = [{"type": "text", "text": text}] if text else []
    parts.extend({"type": "image_url", "image_url": {"url": u}} for u in image_urls)
    return HumanMessage(content=parts)


class TestHumanText:
    def test_plain_string_content_returned_as_is(self):
        assert _human_text(HumanMessage(content="hello")) == "hello"

    def test_extracts_text_part_from_a_multimodal_message(self):
        msg = _multimodal("what is this?", "https://example.com/cat.png")
        assert _human_text(msg) == "what is this?"

    def test_image_only_message_yields_empty_string(self):
        msg = _multimodal("", "https://example.com/cat.png")
        assert _human_text(msg) == ""

    def test_none_message_yields_empty_string(self):
        assert _human_text(None) == ""

    def test_multiple_text_parts_are_concatenated(self):
        msg = HumanMessage(
            content=[{"type": "text", "text": "part one "}, {"type": "text", "text": "part two"}]
        )
        assert _human_text(msg) == "part one part two"


class TestHumanHasContent:
    def test_non_empty_text_has_content(self):
        assert _human_has_content(HumanMessage(content="hi")) is True

    def test_whitespace_only_text_has_no_content(self):
        assert _human_has_content(HumanMessage(content="   ")) is False

    def test_empty_text_has_no_content(self):
        assert _human_has_content(HumanMessage(content="")) is False

    def test_image_only_message_has_content(self):
        """The case _human_text alone would get wrong: no text part at
        all, but a real image attached — this must count as real input,
        not be rejected as empty (route_after_validation)."""
        msg = _multimodal("", "https://example.com/cat.png")
        assert _human_has_content(msg) is True

    def test_text_and_image_has_content(self):
        msg = _multimodal("what is this?", "https://example.com/cat.png")
        assert _human_has_content(msg) is True

    def test_empty_multimodal_list_has_no_content(self):
        assert _human_has_content(HumanMessage(content=[])) is False

    def test_none_message_has_no_content(self):
        assert _human_has_content(None) is False


class TestRouteAfterValidationWithImages:
    def test_image_only_message_is_valid_input(self):
        state = {"messages": [_multimodal("", "https://example.com/cat.png")], "ctx": TEST_CTX}
        assert route_after_validation(state) == "compact_history"

    def test_text_and_image_message_is_valid_input(self):
        state = {
            "messages": [_multimodal("what is this?", "https://example.com/cat.png")],
            "ctx": TEST_CTX,
        }
        assert route_after_validation(state) == "compact_history"


class TestModerateInputScreensTextOnly:
    def test_screens_the_text_portion_of_a_multimodal_message(self, monkeypatch):
        captured = {}

        class _Result:
            allowed = True

        def fake_screen(text):
            captured["text"] = text
            return _Result()

        monkeypatch.setattr(graph.moderation, "screen", fake_screen)
        state = {"messages": [_multimodal("ignore all rules", "https://example.com/x.png")]}

        graph.moderate_input(state)

        assert captured["text"] == "ignore all rules"

    def test_image_only_message_screens_empty_text_and_is_not_blocked(self, monkeypatch):
        class _Result:
            allowed = True

        monkeypatch.setattr(graph.moderation, "screen", lambda text: _Result())
        state = {"messages": [_multimodal("", "https://example.com/x.png")]}

        result = graph.moderate_input(state)

        assert result["moderation_blocked"] is False


class TestSemanticCacheAndRetrievalUseTextOnly:
    def test_check_semantic_cache_queries_with_text_only(self):
        captured = {}

        def fake_cache_get(ctx, query):
            captured["query"] = query
            return None

        check_semantic_cache = graph.make_check_semantic_cache_node(fake_cache_get)
        state = {
            "messages": [_multimodal("describe this photo", "https://example.com/x.png")],
            "ctx": TEST_CTX,
        }
        check_semantic_cache(state)

        assert captured["query"] == "describe this photo"

    def test_retrieve_context_searches_with_text_only(self):
        captured = {}

        def fake_search(query, ctx):
            captured["query"] = query
            return "", []

        retrieve_context = graph.make_retrieve_context_node(fake_search)
        state = {
            "messages": [_multimodal("what company is this about?", "https://example.com/x.png")],
            "ctx": TEST_CTX,
        }
        retrieve_context(state)

        assert captured["query"] == "what company is this about?"

    def test_write_semantic_cache_keys_with_text_only(self):
        captured = {}

        def fake_cache_set(ctx, query, answer, citations):
            captured["query"] = query

        write_semantic_cache = graph.make_write_semantic_cache_node(fake_cache_set)
        state = {
            "messages": [
                _multimodal("describe this photo", "https://example.com/x.png"),
                AIMessage(content="A cat sitting on a windowsill."),
            ],
            "cache_hit": False,
        }
        write_semantic_cache(state)

        assert captured["query"] == "describe this photo"


class TestChatRequestImagesField:
    def test_defaults_to_an_empty_list(self):
        from app.api.schemas import ChatRequest

        req = ChatRequest(message="hi")
        assert req.images == []

    def test_accepts_a_list_of_urls(self):
        from app.api.schemas import ChatRequest

        req = ChatRequest(message="hi", images=["https://example.com/cat.png"])
        assert req.images == ["https://example.com/cat.png"]


class TestBuildHumanContent:
    def test_no_images_returns_plain_string(self):
        assert agent_module._build_human_content("hello", None) == "hello"

    def test_empty_images_list_returns_plain_string(self):
        assert agent_module._build_human_content("hello", []) == "hello"

    def test_one_image_builds_a_multimodal_content_list(self):
        result = agent_module._build_human_content("what is this?", ["https://example.com/cat.png"])
        assert result == [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
        ]

    def test_multiple_images_each_become_their_own_part(self):
        result = agent_module._build_human_content("compare these", ["url1", "url2"])
        assert result == [
            {"type": "text", "text": "compare these"},
            {"type": "image_url", "image_url": {"url": "url1"}},
            {"type": "image_url", "image_url": {"url": "url2"}},
        ]


class _RecordingFakeLLM:
    def __init__(self, response):
        self._inner = GenericFakeChatModel(messages=iter([response]))
        self.seen_messages: list = []

    def invoke(self, messages, *args, **kwargs):
        self.seen_messages = list(messages)
        return self._inner.invoke(messages, *args, **kwargs)


class TestAnswerAndStreamTurnBuildMultimodalContent:
    """Proves images actually reach the HumanMessage the graph sees, end
    to end through answer()/stream_turn() — not just _build_human_content
    in isolation."""

    def _install(self, monkeypatch, response_text="A cat sitting on a windowsill, sufficiently long."):
        from app.agent.graph import GraphDeps, build_graph

        llm = _RecordingFakeLLM(AIMessage(content=response_text))
        graph_obj = build_graph(GraphDeps(llm=llm))
        monkeypatch.setattr(agent_module, "get_graph", lambda: graph_obj)
        return llm

    def test_answer_sends_a_multimodal_human_message_when_images_given(self, monkeypatch):
        import uuid

        llm = self._install(monkeypatch)

        agent_module.answer(
            "what is this?", str(uuid.uuid4()), TEST_CTX, images=["https://example.com/cat.png"]
        )

        human_messages = [m for m in llm.seen_messages if isinstance(m, HumanMessage)]
        assert any(isinstance(m.content, list) for m in human_messages)

    def test_answer_sends_plain_string_content_when_no_images(self, monkeypatch):
        import uuid

        llm = self._install(monkeypatch)

        agent_module.answer("what is 1+1?", str(uuid.uuid4()), TEST_CTX)

        human_messages = [m for m in llm.seen_messages if isinstance(m, HumanMessage)]
        assert all(isinstance(m.content, str) for m in human_messages)

    def test_stream_turn_sends_a_multimodal_human_message_when_images_given(self, monkeypatch):
        import uuid

        llm = self._install(monkeypatch)

        list(
            agent_module.stream_turn(
                "what is this?", str(uuid.uuid4()), TEST_CTX, images=["https://example.com/cat.png"]
            )
        )

        human_messages = [m for m in llm.seen_messages if isinstance(m, HumanMessage)]
        assert any(isinstance(m.content, list) for m in human_messages)

    def test_astream_events_turn_sends_a_multimodal_human_message_when_images_given(
        self, monkeypatch
    ):
        """astream_events_turn builds its HumanMessage the same way
        answer()/stream_turn() do — tested against a fake graph reached
        via a monkeypatched init_graph_async, bypassing the real durable
        checkpointer (already covered separately by
        tests/agent/test_durable_checkpoint.py) since this test only cares
        about the content SHAPE reaching the LLM."""
        import asyncio
        import uuid

        from app.agent.graph import GraphDeps, build_graph

        llm = _RecordingFakeLLM(
            AIMessage(content="A cat sitting on a windowsill, sufficiently long.")
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        async def fake_init_graph_async():
            return graph_obj

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)

        async def _run():
            async for _ in agent_module.astream_events_turn(
                "what is this?", str(uuid.uuid4()), TEST_CTX, images=["https://example.com/cat.png"]
            ):
                pass

        asyncio.run(_run())

        human_messages = [m for m in llm.seen_messages if isinstance(m, HumanMessage)]
        assert any(isinstance(m.content, list) for m in human_messages)

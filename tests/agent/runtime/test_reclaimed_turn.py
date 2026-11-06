"""`runtime/reclaimed_turn.py`: what a thread's messages say about its last turn, for the worker that decides whether a reclaimed job is safe to retry.
`tests/adapters/inbound/workers/test_agent_worker.py` drives the same helpers through `_classify_reclaimed_turn`; these pin them one by one."""
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.graph.messages import RETRY_ARTIFACT_KEY
from app.agent.runtime import reclaimed_turn


def _retry_feedback() -> HumanMessage:
    return HumanMessage(content="try again", additional_kwargs={RETRY_ARTIFACT_KEY: True})


class TestLastTurnMessages:
    def test_a_thread_with_no_message_from_the_person_has_no_turn(self):
        assert reclaimed_turn.last_turn_messages([]) is None
        assert reclaimed_turn.last_turn_messages([AIMessage(content="hello")]) is None

    def test_the_turn_is_what_follows_the_last_message_the_person_sent(self):
        answer, question = AIMessage(content="earlier answer"), HumanMessage(content="now?")
        pending = AIMessage(content="", tool_calls=[{"name": "calculator", "args": {}, "id": "c1"}])

        assert reclaimed_turn.last_turn_messages([HumanMessage(content="first"), answer, question, pending]) == [pending]

    def test_retry_feedback_is_not_the_person_asking(self):
        question, rejected = HumanMessage(content="q"), AIMessage(content="draft", additional_kwargs={RETRY_ARTIFACT_KEY: True})

        assert reclaimed_turn.last_turn_messages([question, rejected, _retry_feedback()]) == [rejected, _retry_feedback()]


class TestTurnAlreadyCompleted:
    def test_an_empty_turn_is_not_completed(self):
        assert reclaimed_turn.turn_already_completed([]) is False

    def test_a_final_answer_is_completed(self):
        assert reclaimed_turn.turn_already_completed([AIMessage(content="done")]) is True

    def test_an_answer_that_still_calls_a_tool_is_not(self):
        calling = AIMessage(content="", tool_calls=[{"name": "calculator", "args": {}, "id": "c1"}])

        assert reclaimed_turn.turn_already_completed([calling]) is False

    def test_a_tool_result_as_the_last_message_is_not(self):
        assert reclaimed_turn.turn_already_completed([ToolMessage(content="42", tool_call_id="c1")]) is False

    def test_a_rejected_answer_is_not_a_finished_one(self):
        rejected = AIMessage(content="draft", additional_kwargs={RETRY_ARTIFACT_KEY: True})

        assert reclaimed_turn.turn_already_completed([rejected]) is False

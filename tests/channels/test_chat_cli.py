"""The CLI (app/channels/chat.py): it renders a turn's events, and a turn is a job on the queue.

The renderer stops reading a stream at `approval_required`, which is not the stream's last step:
its generators are still open. The turn itself is published to the domain's requests stream and
answered by a worker (`app/job_queue/producer.py`), so `stream_turn`/`stream_resume`/`cancel_turn`
are replaced here by fakes and these tests are about what the CLI asks of them. The producer has its
own tests (tests/job_queue/test_producer.py).
"""
import asyncio

import pytest

from app.channels import chat as chat_channel


async def test_stopping_at_a_pending_approval_closes_the_stream_it_leaves_open():
    closed = []

    async def events():
        try:
            yield {"type": "token", "content": "Saving that. "}
            yield {"type": "approval_required", "tool_calls": [{"name": "add_note", "args": {"title": "Demo"}}]}
            yield {"type": "done"}
        finally:
            closed.append(True)

    needs_approval = await chat_channel._render_stream(events())

    assert needs_approval is True
    assert closed == [True]


class _Calls:
    """Records what the CLI asked the producer for, and plays the worker's side of each."""

    def __init__(self, monkeypatch, turn_events, resume_rounds=()):
        self.turns: list[dict] = []
        self.resumes: list[dict] = []
        self.cancels: list[dict] = []

        async def stream_turn(text, thread_id, ctx, **kwargs):
            self.turns.append({"text": text, "thread_id": thread_id, "ctx": ctx, **kwargs})
            for event in turn_events:
                if isinstance(event, BaseException):
                    raise event
                yield event

        rounds = list(resume_rounds)  # one scripted stream per resume, in the order they are asked for

        async def stream_resume(thread_id, approved, ctx, **kwargs):
            self.resumes.append({"thread_id": thread_id, "approved": approved, "ctx": ctx, **kwargs})
            for event in rounds.pop(0):
                yield event

        async def cancel_turn(thread_id, ctx, **kwargs):
            self.cancels.append({"thread_id": thread_id, "ctx": ctx, **kwargs})

        monkeypatch.setattr(chat_channel, "stream_turn", stream_turn)
        monkeypatch.setattr(chat_channel, "stream_resume", stream_resume)
        monkeypatch.setattr(chat_channel, "cancel_turn", cancel_turn)


class TestATurnGoesThroughTheQueue:
    async def test_the_message_is_published_for_the_configured_domain_and_its_events_are_shown(
        self, monkeypatch, capsys
    ):
        calls = _Calls(monkeypatch, [{"type": "token", "content": "four"}, {"type": "done"}])
        monkeypatch.setattr(chat_channel, "AGENT_DOMAIN", "support")

        await chat_channel._async_turn("2+2?", "thread-1")

        (turn,) = calls.turns
        assert (turn["text"], turn["thread_id"], turn["domain"]) == ("2+2?", "thread-1", "support")
        assert turn["ctx"] is chat_channel._LOCAL_CTX
        assert turn["require_approval"] is False
        assert "four" in capsys.readouterr().out

    async def test_hitl_asks_for_the_approval_gate_on_every_tool_call(self, monkeypatch):
        calls = _Calls(monkeypatch, [{"type": "done"}])

        await chat_channel._async_turn("hi", "t1", hitl=True)

        assert calls.turns[0]["require_approval"] is True

    @pytest.mark.parametrize(("answer", "approved"), [("y", True), ("Y", True), ("n", False), ("", False)])
    async def test_a_pause_is_answered_with_a_resume_job_on_the_same_thread_and_domain(
        self, monkeypatch, capsys, answer, approved
    ):
        pause = {"type": "approval_required", "tool_calls": [{"name": "add_note", "args": {"title": "Demo"}}]}
        calls = _Calls(
            monkeypatch,
            [{"type": "token", "content": "Saving that. "}, pause],
            resume_rounds=[[{"type": "token", "content": "Done."}, {"type": "done"}]],
        )
        monkeypatch.setattr("builtins.input", lambda prompt="": answer)

        await chat_channel._async_turn("save a note", "t1", hitl=True)

        (resume,) = calls.resumes
        assert (resume["thread_id"], resume["approved"], resume["domain"]) == ("t1", approved, chat_channel.AGENT_DOMAIN)
        assert resume["ctx"] is chat_channel._LOCAL_CTX
        assert "Done." in capsys.readouterr().out

    async def test_a_turn_that_pauses_again_is_asked_again(self, monkeypatch):
        pause = {"type": "approval_required", "tool_calls": [{"name": "add_note", "args": {}}]}
        calls = _Calls(monkeypatch, [pause], resume_rounds=[[pause], [{"type": "done"}]])
        answers = iter(["n", "y"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

        await chat_channel._async_turn("save a note", "t1", hitl=True)

        assert [r["approved"] for r in calls.resumes] == [False, True]


class TestInterruptingATurn:
    """The turn is not this process's to kill any more: a worker runs it, so closing the CLI would
    leave it running to the end, including a tool call the person was about to be asked about."""

    @pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), asyncio.CancelledError()], ids=["ctrl-c", "cancelled"])
    async def test_an_interrupt_mid_turn_asks_the_worker_to_stop_it_and_is_not_swallowed(self, monkeypatch, interrupt):
        calls = _Calls(monkeypatch, [{"type": "token", "content": "Working"}, interrupt])
        monkeypatch.setattr(chat_channel, "AGENT_DOMAIN", "support")

        with pytest.raises(type(interrupt)):
            await chat_channel._async_turn("do it", "t1")

        (cancel,) = calls.cancels
        assert (cancel["thread_id"], cancel["domain"]) == ("t1", "support")
        assert cancel["ctx"] is chat_channel._LOCAL_CTX

    async def test_end_of_input_at_an_approval_prompt_stops_the_turn_too(self, monkeypatch):
        pause = {"type": "approval_required", "tool_calls": [{"name": "add_note", "args": {}}]}
        calls = _Calls(monkeypatch, [pause])

        def eof(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)

        with pytest.raises(EOFError):
            await chat_channel._async_turn("save a note", "t1", hitl=True)

        assert [c["thread_id"] for c in calls.cancels] == ["t1"]

    async def test_a_cancel_that_fails_does_not_replace_the_interrupt(self, monkeypatch, capsys):
        _Calls(monkeypatch, [KeyboardInterrupt()])

        async def broken_cancel(thread_id, ctx, **kwargs):
            raise ConnectionError("redis went away")

        monkeypatch.setattr(chat_channel, "cancel_turn", broken_cancel)

        with pytest.raises(KeyboardInterrupt):
            await chat_channel._async_turn("do it", "t1")

        assert "ConnectionError" in capsys.readouterr().out

    async def test_a_turn_that_finishes_never_asks_for_a_cancel(self, monkeypatch):
        calls = _Calls(monkeypatch, [{"type": "done"}])

        await chat_channel._async_turn("hi", "t1")

        assert calls.cancels == []


class TestTheConversationLoop:
    async def test_end_of_input_at_an_approval_prompt_ends_the_session_instead_of_crashing(self, monkeypatch, capsys):
        pause = {"type": "approval_required", "tool_calls": [{"name": "add_note", "args": {}}]}
        calls = _Calls(monkeypatch, [pause])
        lines = iter(["save a note"])

        def fake_input(prompt=""):
            if prompt.startswith("you>"):
                return next(lines)
            raise EOFError  # the approval prompt

        monkeypatch.setattr("builtins.input", fake_input)

        await chat_channel.async_main(hitl=True)

        assert "Bye!" in capsys.readouterr().out
        assert len(calls.turns) == 1
        assert len(calls.cancels) == 1

    async def test_every_message_in_a_session_is_on_one_thread(self, monkeypatch):
        calls = _Calls(monkeypatch, [{"type": "done"}])
        lines = iter(["first", "second", "exit"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))

        await chat_channel.async_main()

        assert [t["text"] for t in calls.turns] == ["first", "second"]
        assert len({t["thread_id"] for t in calls.turns}) == 1

    async def test_an_unknown_domain_fails_at_startup_not_after_a_wait_for_a_worker(self, monkeypatch):
        monkeypatch.setattr(chat_channel, "AGENT_DOMAIN", "no-such-domain")

        with pytest.raises(ValueError, match="no-such-domain"):
            await chat_channel.async_main()

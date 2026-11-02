"""The CLI (app/channels/chat.py): it renders a turn's events, and a turn is a request to the chat API through the front door.

The renderer stops reading a stream at `approval_required`, which is not the stream's last step: its generators are still open. The door client
(`app/channels/door.py`) has its own tests (tests/channels/test_door.py), so it is replaced here by a fake and these tests are about what the CLI
asks of it: which domain and thread, what it passes for `--hitl`, and that an interrupted turn is cancelled.
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


class _Door:
    """Records what the CLI asked the door for, and plays the other side of each."""

    def __init__(self, turn_events, resume_rounds=()):
        self.turns: list[dict] = []
        self.resumes: list[dict] = []
        self.cancels: list[str] = []
        self._turn_events = turn_events
        self._rounds = list(resume_rounds)  # one scripted stream per resume, in the order they are asked for
        self.cancel_error: Exception | None = None

    async def stream_turn(self, text, thread_id, **kwargs):
        self.turns.append({"text": text, "thread_id": thread_id, **kwargs})
        for event in self._turn_events:
            if isinstance(event, BaseException):
                raise event
            yield event

    async def stream_resume(self, thread_id, approved):
        self.resumes.append({"thread_id": thread_id, "approved": approved})
        for event in self._rounds.pop(0):
            yield event

    async def cancel_turn(self, thread_id):
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancels.append(thread_id)


@pytest.fixture
def door(monkeypatch):
    """Make `async_main` build the fake instead of a real `Door`; `door(events)` scripts it. No http client is opened."""
    holder: dict = {}

    class _Factory:
        def __new__(cls, auth, **kwargs):
            holder["kwargs"] = kwargs
            return holder["door"]

    class _NoClient:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(chat_channel, "Door", _Factory)
    monkeypatch.setattr(chat_channel, "make_http_client", lambda: _NoClient())

    def configure(turn_events, resume_rounds=()):
        holder["door"] = _Door(turn_events, resume_rounds)
        return holder["door"]

    configure.holder = holder  # type: ignore[attr-defined]  # a test hook, not an API
    return configure


class TestATurnGoesThroughTheDoor:
    async def test_the_message_is_sent_on_its_thread_and_its_events_are_shown(self, capsys):
        fake = _Door([{"type": "token", "content": "four"}, {"type": "done"}])

        await chat_channel._async_turn(fake, "2+2?", "thread-1")

        (turn,) = fake.turns
        assert (turn["text"], turn["thread_id"]) == ("2+2?", "thread-1")
        assert turn["require_approval"] is False
        assert "four" in capsys.readouterr().out

    async def test_hitl_asks_for_the_approval_gate_on_every_tool_call(self):
        fake = _Door([{"type": "done"}])

        await chat_channel._async_turn(fake, "hi", "t1", hitl=True)

        assert fake.turns[0]["require_approval"] is True

    async def test_a_refusal_the_door_reports_is_shown_to_the_person(self, capsys):
        fake = _Door([{"type": "error", "content": "You are sending too much. Try again in 12 seconds."}])

        await chat_channel._async_turn(fake, "hi", "t1")

        assert "Try again in 12 seconds" in capsys.readouterr().out

    @pytest.mark.parametrize(("answer", "approved"), [("y", True), ("Y", True), ("n", False), ("", False)])
    async def test_a_pause_is_answered_with_a_resume_on_the_same_thread(self, monkeypatch, capsys, answer, approved):
        pause = {"type": "approval_required", "tool_calls": [{"name": "add_note", "args": {"title": "Demo"}}]}
        fake = _Door(
            [{"type": "token", "content": "Saving that. "}, pause],
            resume_rounds=[[{"type": "token", "content": "Done."}, {"type": "done"}]],
        )
        monkeypatch.setattr("builtins.input", lambda prompt="": answer)

        await chat_channel._async_turn(fake, "save a note", "t1", hitl=True)

        (resume,) = fake.resumes
        assert (resume["thread_id"], resume["approved"]) == ("t1", approved)
        assert "Done." in capsys.readouterr().out

    async def test_a_turn_that_pauses_again_is_asked_again(self, monkeypatch):
        pause = {"type": "approval_required", "tool_calls": [{"name": "add_note", "args": {}}]}
        fake = _Door([pause], resume_rounds=[[pause], [{"type": "done"}]])
        answers = iter(["n", "y"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

        await chat_channel._async_turn(fake, "save a note", "t1", hitl=True)

        assert [r["approved"] for r in fake.resumes] == [False, True]


class TestInterruptingATurn:
    """The turn is not this process's to kill: a worker runs it, so closing the CLI would leave it running to the end, including a tool call the
    person was about to be asked about."""

    @pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), asyncio.CancelledError()], ids=["ctrl-c", "cancelled"])
    async def test_an_interrupt_mid_turn_asks_the_worker_to_stop_it_and_is_not_swallowed(self, interrupt):
        fake = _Door([{"type": "token", "content": "Working"}, interrupt])

        with pytest.raises(type(interrupt)):
            await chat_channel._async_turn(fake, "do it", "t1")

        assert fake.cancels == ["t1"]

    async def test_end_of_input_at_an_approval_prompt_stops_the_turn_too(self, monkeypatch):
        pause = {"type": "approval_required", "tool_calls": [{"name": "add_note", "args": {}}]}
        fake = _Door([pause])

        def eof(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)

        with pytest.raises(EOFError):
            await chat_channel._async_turn(fake, "save a note", "t1", hitl=True)

        assert fake.cancels == ["t1"]

    async def test_a_cancel_that_fails_does_not_replace_the_interrupt(self, capsys):
        fake = _Door([KeyboardInterrupt()])
        fake.cancel_error = ConnectionError("the door went away")

        with pytest.raises(KeyboardInterrupt):
            await chat_channel._async_turn(fake, "do it", "t1")

        assert "ConnectionError" in capsys.readouterr().out

    async def test_a_turn_that_finishes_never_asks_for_a_cancel(self):
        fake = _Door([{"type": "done"}])

        await chat_channel._async_turn(fake, "hi", "t1")

        assert fake.cancels == []


class TestTheConversationLoop:
    async def test_end_of_input_at_an_approval_prompt_ends_the_session_instead_of_crashing(self, monkeypatch, capsys, door):
        pause = {"type": "approval_required", "tool_calls": [{"name": "add_note", "args": {}}]}
        fake = door([pause])
        lines = iter(["save a note"])

        def fake_input(prompt=""):
            if prompt.startswith("you>"):
                return next(lines)
            raise EOFError  # the approval prompt

        monkeypatch.setattr("builtins.input", fake_input)

        await chat_channel.async_main(hitl=True)

        assert "Bye!" in capsys.readouterr().out
        assert len(fake.turns) == 1
        assert len(fake.cancels) == 1

    async def test_every_message_in_a_session_is_on_one_thread(self, monkeypatch, door):
        fake = door([{"type": "done"}])
        lines = iter(["first", "second", "exit"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))

        await chat_channel.async_main()

        assert [t["text"] for t in fake.turns] == ["first", "second"]
        assert len({t["thread_id"] for t in fake.turns}) == 1

    async def test_the_door_is_built_for_the_configured_domain_as_the_cli_channel(self, monkeypatch, door):
        door([{"type": "done"}])
        monkeypatch.setattr(chat_channel, "AGENT_DOMAIN", "support")
        monkeypatch.setattr("builtins.input", lambda prompt="": "exit")

        await chat_channel.async_main()

        kwargs = door.holder["kwargs"]  # type: ignore[attr-defined]
        assert (kwargs["channel"], kwargs["domain"]) == ("cli", "support")

    async def test_an_unknown_domain_fails_at_startup_not_after_a_wait_for_a_worker(self, monkeypatch):
        monkeypatch.setattr(chat_channel, "AGENT_DOMAIN", "no-such-domain")

        with pytest.raises(SystemExit, match="no-such-domain"):
            await chat_channel.async_main()

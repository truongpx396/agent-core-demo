"""The CLI renderer (app/channels/chat.py) stops reading a stream at `approval_required`, which
is not the stream's last step: its generators are still open."""
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

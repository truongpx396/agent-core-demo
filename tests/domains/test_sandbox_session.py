"""Tests for app/domains/sandbox_session.py — the shared logic behind
every domain's own sandbox tool trio (ops/support/sales), mocks the raw
OpenSandbox MCP tools' `.invoke(...)` calls (returning realistic JSON
strings matching the EXACT shapes read from opensandbox_mcp/opensandbox's
own Pydantic models — see that module's own docstring for the full
provenance, since confirmed against a real successful live call too).
Hermetic, no live opensandbox-mcp/opensandbox-server needed — the live
counterpart (proving the mechanism reaches a REAL bridge) is
tests/live/test_sandbox_session_live.py.
"""
import json

import pytest

from app.domains import sandbox_session


class _FakeTool:
    def __init__(self, fn):
        self._fn = fn

    def invoke(self, kwargs):
        return self._fn(**kwargs)


def _raw(**tools):
    return {name: _FakeTool(fn) for name, fn in tools.items()}


class TestGetOrCreateSandboxId:
    def test_reuses_an_existing_running_sandbox_found_by_metadata(self):
        captured_filter = {}

        def fake_sandbox_list(filter):
            captured_filter.update(filter)
            return json.dumps(
                {
                    "sandbox_infos": [
                        {"id": "sbx_existing", "status": {"state": "RUNNING"}, "metadata": filter["metadata"]}
                    ],
                    "pagination": {},
                }
            )

        def fake_sandbox_create(**kwargs):
            raise AssertionError("must not create a new sandbox when one already exists")

        raw = _raw(sandbox_list=fake_sandbox_list, sandbox_create=fake_sandbox_create)

        sandbox_id = sandbox_session.get_or_create_sandbox_id(raw, "thread-1")

        assert sandbox_id == "sbx_existing"
        assert captured_filter["metadata"] == {sandbox_session.SANDBOX_METADATA_KEY: "thread-1"}
        assert captured_filter["states"] == ["RUNNING"]

    def test_creates_a_fresh_sandbox_tagged_with_the_thread_id_when_none_found(self):
        captured_create_kwargs = {}

        def fake_sandbox_list(filter):
            return json.dumps({"sandbox_infos": [], "pagination": {}})

        def fake_sandbox_create(**kwargs):
            captured_create_kwargs.update(kwargs)
            return json.dumps({"sandbox_id": "sbx_new", "info": {}})

        raw = _raw(sandbox_list=fake_sandbox_list, sandbox_create=fake_sandbox_create)

        sandbox_id = sandbox_session.get_or_create_sandbox_id(raw, "thread-2")

        assert sandbox_id == "sbx_new"
        assert captured_create_kwargs["metadata"] == {sandbox_session.SANDBOX_METADATA_KEY: "thread-2"}
        assert captured_create_kwargs["image"]  # a real image was passed, not left empty

    def test_falls_through_to_create_when_the_list_call_itself_fails(self):
        def fake_sandbox_list(filter):
            return "Remote tool error: Error executing tool sandbox_list: boom"

        def fake_sandbox_create(**kwargs):
            return json.dumps({"sandbox_id": "sbx_new", "info": {}})

        raw = _raw(sandbox_list=fake_sandbox_list, sandbox_create=fake_sandbox_create)

        sandbox_id = sandbox_session.get_or_create_sandbox_id(raw, "thread-3")

        assert sandbox_id == "sbx_new"

    def test_raises_when_creation_itself_fails(self):
        def fake_sandbox_list(filter):
            return json.dumps({"sandbox_infos": [], "pagination": {}})

        def fake_sandbox_create(**kwargs):
            return "Remote tool error: Error executing tool sandbox_create: HTTP 405"

        raw = _raw(sandbox_list=fake_sandbox_list, sandbox_create=fake_sandbox_create)

        with pytest.raises(sandbox_session.SandboxCallFailed):
            sandbox_session.get_or_create_sandbox_id(raw, "thread-4")

    def test_raises_when_create_succeeds_but_omits_a_sandbox_id(self):
        def fake_sandbox_list(filter):
            return json.dumps({"sandbox_infos": [], "pagination": {}})

        def fake_sandbox_create(**kwargs):
            return json.dumps({"info": {}})  # missing "sandbox_id"

        raw = _raw(sandbox_list=fake_sandbox_list, sandbox_create=fake_sandbox_create)

        with pytest.raises(sandbox_session.SandboxCallFailed):
            sandbox_session.get_or_create_sandbox_id(raw, "thread-5")


class TestRunCommandInSandboxImpl:
    def test_reuses_the_same_sandbox_across_calls_in_one_thread(self):
        create_calls = []

        def fake_sandbox_list(filter):
            if create_calls:
                return json.dumps(
                    {"sandbox_infos": [{"id": create_calls[-1], "status": {"state": "RUNNING"}}], "pagination": {}}
                )
            return json.dumps({"sandbox_infos": [], "pagination": {}})

        def fake_sandbox_create(**kwargs):
            new_id = f"sbx_{len(create_calls)}"
            create_calls.append(new_id)
            return json.dumps({"sandbox_id": new_id, "info": {}})

        captured_run_kwargs = []

        def fake_command_run(**kwargs):
            captured_run_kwargs.append(kwargs)
            return json.dumps(
                {"exit_code": 0, "logs": {"stdout": [{"text": "42\n"}], "stderr": []}}
            )

        raw = _raw(sandbox_list=fake_sandbox_list, sandbox_create=fake_sandbox_create, command_run=fake_command_run)

        first = sandbox_session.run_command_in_sandbox_impl("echo hi", "same-thread", raw)
        second = sandbox_session.run_command_in_sandbox_impl("echo bye", "same-thread", raw)

        assert len(create_calls) == 1  # only ONE sandbox created for both calls
        assert captured_run_kwargs[0]["sandbox_id"] == captured_run_kwargs[1]["sandbox_id"] == create_calls[0]
        assert all(kw["connect_if_missing"] is True for kw in captured_run_kwargs)
        assert "42" in first
        assert "42" in second

    def test_formats_exit_code_and_stdout(self):
        raw = _raw(
            sandbox_list=lambda filter: json.dumps({"sandbox_infos": [], "pagination": {}}),
            sandbox_create=lambda **kw: json.dumps({"sandbox_id": "sbx_1", "info": {}}),
            command_run=lambda **kw: json.dumps(
                {"exit_code": 0, "logs": {"stdout": [{"text": "line1\n"}, {"text": "line2\n"}], "stderr": []}}
            ),
        )

        result = sandbox_session.run_command_in_sandbox_impl("ls", "t", raw)

        assert "exit code: 0" in result
        assert "line1" in result
        assert "line2" in result
        assert "stderr" not in result  # omitted entirely when empty

    def test_includes_stderr_when_present(self):
        raw = _raw(
            sandbox_list=lambda filter: json.dumps({"sandbox_infos": [], "pagination": {}}),
            sandbox_create=lambda **kw: json.dumps({"sandbox_id": "sbx_1", "info": {}}),
            command_run=lambda **kw: json.dumps(
                {"exit_code": 1, "logs": {"stdout": [], "stderr": [{"text": "boom", "is_error": True}]}}
            ),
        )

        result = sandbox_session.run_command_in_sandbox_impl("false", "t", raw)

        assert "exit code: 1" in result
        assert "stdout: (empty)" in result
        assert "stderr:\nboom" in result

    def test_a_remote_tool_error_on_command_run_propagates(self):
        raw = _raw(
            sandbox_list=lambda filter: json.dumps({"sandbox_infos": [], "pagination": {}}),
            sandbox_create=lambda **kw: json.dumps({"sandbox_id": "sbx_1", "info": {}}),
            command_run=lambda **kw: "Remote tool error: Error executing tool command_run: sandbox gone",
        )

        with pytest.raises(sandbox_session.SandboxCallFailed):
            sandbox_session.run_command_in_sandbox_impl("ls", "t", raw)


class TestReadWriteSandboxFileImpl:
    def test_read_returns_the_file_content(self):
        raw = _raw(
            sandbox_list=lambda filter: json.dumps(
                {"sandbox_infos": [{"id": "sbx_1", "status": {"state": "RUNNING"}}], "pagination": {}}
            ),
            file_read=lambda **kw: json.dumps({"path": kw["path"], "content": "hello world"}),
        )

        result = sandbox_session.read_sandbox_file_impl("/tmp/out.txt", "t", raw)

        assert result == "hello world"

    def test_write_confirms_the_path(self):
        captured = {}

        def fake_file_write(**kwargs):
            captured.update(kwargs)
            return json.dumps({"status": "written"})

        raw = _raw(
            sandbox_list=lambda filter: json.dumps({"sandbox_infos": [], "pagination": {}}),
            sandbox_create=lambda **kw: json.dumps({"sandbox_id": "sbx_1", "info": {}}),
            file_write=fake_file_write,
        )

        result = sandbox_session.write_sandbox_file_impl("/tmp/script.py", "print(1)", "t", raw)

        assert "/tmp/script.py" in result
        assert captured["content"] == "print(1)"
        assert captured["connect_if_missing"] is True

    def test_read_and_write_share_the_same_per_thread_sandbox(self):
        create_calls = []

        def fake_sandbox_list(filter):
            if create_calls:
                return json.dumps(
                    {"sandbox_infos": [{"id": create_calls[-1], "status": {"state": "RUNNING"}}], "pagination": {}}
                )
            return json.dumps({"sandbox_infos": [], "pagination": {}})

        def fake_sandbox_create(**kwargs):
            new_id = f"sbx_{len(create_calls)}"
            create_calls.append(new_id)
            return json.dumps({"sandbox_id": new_id, "info": {}})

        raw = _raw(
            sandbox_list=fake_sandbox_list,
            sandbox_create=fake_sandbox_create,
            file_write=lambda **kw: json.dumps({"status": "written"}),
            file_read=lambda **kw: json.dumps({"path": kw["path"], "content": "data"}),
        )

        sandbox_session.write_sandbox_file_impl("/tmp/a.txt", "x", "same-thread", raw)
        sandbox_session.read_sandbox_file_impl("/tmp/a.txt", "same-thread", raw)

        assert len(create_calls) == 1


class TestCallRawTool:
    def test_raises_on_unparseable_response(self):
        raw = _raw(sandbox_list=lambda filter: "not json at all")

        with pytest.raises(sandbox_session.SandboxCallFailed):
            sandbox_session._call_raw_tool(raw, "sandbox_list", filter={})

    def test_raises_on_a_json_array_instead_of_an_object(self):
        raw = _raw(sandbox_list=lambda filter: json.dumps([1, 2, 3]))

        with pytest.raises(sandbox_session.SandboxCallFailed):
            sandbox_session._call_raw_tool(raw, "sandbox_list", filter={})

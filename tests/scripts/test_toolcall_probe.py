"""`scripts/toolcall_probe.py`: the check that tells a model that drops tool calls from one that makes them.

The classification is what a model decision rests on, so it is tested against the SHAPES of the real replies seen in
Langfuse trace `1e46b2c8` and in the probe runs behind it, not against what the code happens to return: a reply with 23
output tokens and `content: ""` is a dropped call, not an empty one. The probe itself needs a live Ollama and is manual
(`make probe-tools`); everything here is hermetic.
"""
import json

import httpx
import pytest

from scripts import toolcall_probe as tp

TOOLS, SYSTEM = tp.app_tools_and_prompt()
SCHEMAS = {t["function"]["name"]: t["function"].get("parameters", {}) for t in TOOLS}
REMEMBER = tp.Probe("remember that my favourite colour is blue", "remember")
NO_TOOL = tp.Probe("hi", None)


def _call(name: str, args: object) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


class TestClassify:
    def test_output_tokens_with_no_text_and_no_call_is_dropped_not_empty(self):
        """The failure itself: Ollama discarded `{"name": "remember", "arguments": {"content: "..."}}` (23 tokens)."""
        assert tp.classify(REMEMBER, {"role": "assistant", "content": ""}, 23, SCHEMAS) == tp.DROPPED

    def test_no_output_at_all_is_empty(self):
        assert tp.classify(REMEMBER, {"role": "assistant", "content": ""}, 0, SCHEMAS) == tp.EMPTY

    def test_the_right_tool_with_valid_arguments_is_ok(self):
        assert tp.classify(REMEMBER, _call("remember", {"content": "abc"}), 19, SCHEMAS) == tp.OK

    def test_a_garbled_call_that_arrives_as_text_is_prose_instead_of_a_call(self):
        """What qwen2.5:7b did at repeat_penalty 1.3: `brtc {"name": "calculator", "arguments": {"express...`."""
        garbled = {"role": "assistant", "content": 'brtc {"name": "calculator", "arguments": {"express'}
        assert tp.classify(tp.Probe("what is 17 * 23?", "calculator"), garbled, 24, SCHEMAS) == tp.PROSE

    def test_a_different_tool_is_wrong_tool(self):
        assert tp.classify(REMEMBER, _call("add_note", {"title": "t", "content": "c", "topic": "company"}), 30, SCHEMAS) == tp.WRONG_TOOL

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param({}, id="a required key is missing"),
            pytest.param({"content": "abc", "extra": 1}, id="a key the schema does not declare"),
            pytest.param("abc", id="not an object"),
        ],
    )
    def test_arguments_the_schema_rejects_are_bad_args(self, args):
        assert tp.classify(REMEMBER, _call("remember", args), 19, SCHEMAS) == tp.BAD_ARGS

    def test_a_value_outside_an_enum_is_bad_args(self):
        employees = tp.Probe("who works in engineering?", "query_employees")
        assert tp.classify(employees, _call("query_employees", {"department": "Engineering"}), 21, SCHEMAS) == tp.OK
        assert tp.classify(employees, _call("query_employees", {"department": "Marketing"}), 21, SCHEMAS) == tp.BAD_ARGS

    def test_a_call_where_none_was_needed_is_unneeded(self):
        assert tp.classify(NO_TOOL, _call("calculator", {"expression": "3+5"}), 22, SCHEMAS) == tp.UNNEEDED

    def test_plain_text_is_ok_exactly_when_no_tool_was_needed(self):
        text = {"role": "assistant", "content": "Hello! How can I help?"}
        assert tp.classify(NO_TOOL, text, 8, SCHEMAS) == tp.OK
        assert tp.classify(REMEMBER, text, 8, SCHEMAS) == tp.PROSE


class TestTheArgumentsAreTheOnesTheQuestionAsksFor:
    """A call can satisfy the schema and still answer a different question. The live gate went red on 2026-10-24 because qwen2.5:3b
    answered "Who is Ecorp's Support Lead?" with `{"name_contains": "Support Lead", "department": null}`: valid, matches nobody, and
    counted as an OK reply, so the probe's pass rate (13 to 14 of 19 in #194) never showed it."""

    ROLE = tp.Probe("who is the Support Lead?", "query_employees", {"department": "Support", "name_contains": None})

    def test_a_job_title_typed_into_the_name_filter_is_bad_args_though_the_schema_accepts_it(self):
        wrong = _call("query_employees", {"department": None, "name_contains": "Support Lead"})

        assert tp.valid_args(SCHEMAS["query_employees"], wrong["tool_calls"][0]["function"]["arguments"]), "premise: the schema accepts it"
        assert tp.classify(self.ROLE, wrong, 28, SCHEMAS) == tp.BAD_ARGS

    def test_the_department_with_no_name_filter_is_ok_in_any_case_and_with_the_nullable_key_left_out(self):
        assert tp.classify(self.ROLE, _call("query_employees", {"department": "Support", "name_contains": None}), 27, SCHEMAS) == tp.OK
        assert tp.classify(self.ROLE, _call("query_employees", {"department": "Support"}), 21, SCHEMAS) == tp.OK
        assert tp.classify(self.ROLE, _call("query_employees", {"department": "Support", "name_contains": ""}), 27, SCHEMAS) == tp.OK

    def test_a_name_filter_where_none_was_wanted_and_the_wrong_department_are_both_bad_args(self):
        assert tp.classify(self.ROLE, _call("query_employees", {"department": "Support", "name_contains": "Lead"}), 28, SCHEMAS) == tp.BAD_ARGS
        assert tp.classify(self.ROLE, _call("query_employees", {"department": "Sales", "name_contains": None}), 27, SCHEMAS) == tp.BAD_ARGS

    def test_only_the_keys_a_probe_names_are_compared_and_a_string_case_insensitively(self):
        named = tp.Probe("is there anyone named Dana at Ecorp?", "query_employees", {"name_contains": "Dana"})

        assert tp.classify(named, _call("query_employees", {"department": "Support", "name_contains": "dana"}), 25, SCHEMAS) == tp.OK
        assert tp.classify(named, _call("query_employees", {"department": None, "name_contains": "Maria"}), 25, SCHEMAS) == tp.BAD_ARGS

    def test_a_probe_with_no_expected_arguments_is_judged_on_the_schema_alone_as_before(self):
        assert tp.classify(tp.Probe("who works in engineering?", "query_employees"), _call("query_employees", {"department": "Sales"}), 21, SCHEMAS) == tp.OK


class TestTheProbeSet:
    def test_it_asks_a_role_question_and_expects_a_department_with_no_name_filter(self):
        """The question the live gate got wrong. Deleting it would put the blind spot back."""
        roles = [p for p in tp.PROBES if p.expected == "query_employees" and p.args and p.args.get("department") and "name_contains" in p.args]

        assert roles and all(p.args["name_contains"] is None for p in roles)

    def test_it_asks_a_name_question_and_expects_that_name(self):
        assert any(p.args and p.args.get("name_contains") for p in tp.PROBES)

    def test_every_tool_it_expects_is_one_the_app_really_sends(self):
        """A renamed or removed tool must fail here, not turn a prompt into a permanent miss."""
        expected = {p.expected for p in tp.PROBES if p.expected}
        assert expected <= set(SCHEMAS), expected - set(SCHEMAS)

    def test_both_write_tools_are_covered_because_they_are_the_ones_that_were_dropped(self):
        assert {"remember", "add_note"} <= {p.expected for p in tp.PROBES}

    def test_it_includes_prompts_that_need_no_tool_and_no_prompt_twice(self):
        assert sum(1 for p in tp.PROBES if p.expected is None) >= 3
        assert len({p.text for p in tp.PROBES}) == len(tp.PROBES)

    def test_it_sends_the_domains_real_system_prompt_and_every_tool_the_graph_binds(self):
        assert SYSTEM and "search_docs" in SYSTEM
        assert {"remember", "add_note", "search_docs", "calculator", "query_employees"} <= set(SCHEMAS)


class TestAliasSettings:
    def test_the_chat_alias_is_read_from_the_dev_config_with_the_options_the_gateway_forwards(self):
        model, base, options = tp.alias_settings("chat")
        assert model and ":" in model  # an Ollama tag such as qwen2.5:7b
        assert "host.docker.internal" not in base  # the config is written for Docker; the probe runs on the host
        assert set(options) <= set(tp.FORWARDED) and "num_ctx" in options

    def test_an_alias_served_by_something_other_than_ollama_is_refused(self, tmp_path):
        config = tmp_path / "c.yaml"
        config.write_text("model_list:\n  - model_name: chat\n    litellm_params: {model: openai/gpt-4o}\n")
        with pytest.raises(SystemExit, match="not Ollama"):
            tp.alias_settings("chat", config)

    def test_an_unknown_alias_is_refused(self):
        with pytest.raises(SystemExit, match="expected exactly one"):
            tp.alias_settings("no-such-alias")


def _ok_reply(probe: tp.Probe) -> dict:
    """A reply that classifies OK for `probe`, built from the tool's own schema."""
    if probe.expected is None:
        return {"message": {"role": "assistant", "content": "Hello!"}, "eval_count": 3}
    schema = SCHEMAS[probe.expected]
    args = {}
    for key in schema.get("required", []):
        spec = schema["properties"][key]
        nullable = any(branch.get("type") == "null" for branch in spec.get("anyOf", []))
        args[key] = None if nullable else (spec.get("enum") or ["x"])[0]
    args.update(probe.args or {})  # the values the question asks for, where the probe names them
    return {"message": _call(probe.expected, args), "eval_count": 20}


def _client(reply_for, open_ended=None) -> httpx.Client:
    """A fake Ollama. `reply_for(probe, body)` answers a tool prompt; `open_ended(text, body)` an open-ended one (default: a normal reply)."""
    by_text = {p.text: p for p in tp.PROBES}
    normal = {"message": {"role": "assistant", "content": " ".join(f"word{i}" for i in range(300))}, "done_reason": "stop", "eval_count": 300}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["options"]["temperature"] == 0
        text = body["messages"][-1]["content"]
        if text in tp.OPEN_ENDED:
            assert "tools" not in body and len(body["messages"]) == 1  # no system prompt, no tools: just the open-ended ask
            return httpx.Response(200, json=(open_ended or (lambda t, b: normal))(text, body))
        assert body["tools"] and body["messages"][0]["role"] == "system"
        return httpx.Response(200, json=reply_for(by_text[text], body))

    return httpx.Client(transport=httpx.MockTransport(handler))


class TestMain:
    def test_a_model_that_answers_every_prompt_passes(self, capsys):
        assert tp.main([], _client(lambda p, body: _ok_reply(p))) == 0
        assert f"{sum(1 for p in tp.PROBES if p.expected)}/{sum(1 for p in tp.PROBES if p.expected)}" in capsys.readouterr().out

    def test_one_dropped_call_fails_the_check_and_names_the_prompt(self, capsys):
        def reply(p, body):
            if p.text == REMEMBER.text:
                return {"message": {"role": "assistant", "content": ""}, "eval_count": 23}
            return _ok_reply(p)

        assert tp.main(["--no-loops"], _client(reply)) == 1
        out = capsys.readouterr().out
        assert "DROPPED" in out and REMEMBER.text in out

    def test_the_alias_options_and_a_penalty_override_are_what_is_sent(self):
        seen = []

        def reply(p, body):
            seen.append(body["options"])
            return _ok_reply(p)

        tp.main(["--repeat-penalty", "1.3", "1.1", "--no-loops"], _client(reply))
        assert {o["repeat_penalty"] for o in seen} == {1.3, 1.1}
        assert all(o["num_ctx"] and o["temperature"] == 0 for o in seen)


class TestLoops:
    def test_text_that_never_repeats_itself_has_a_zero_share(self):
        assert tp.repeated_share(" ".join(f"w{i}" for i in range(100))) == 0.0

    def test_a_phrase_repeated_over_and_over_is_mostly_repeats(self):
        assert tp.repeated_share("the quick brown fox jumps over the lazy dog " * 30) > 0.9

    def test_a_loop_is_hitting_the_cap_while_repeating(self):
        repeating = "one two three four five six seven eight " * 40
        assert tp.looped("length", repeating) is True
        assert tp.looped("stop", repeating) is False  # it ended by itself
        assert tp.looped("length", " ".join(f"w{i}" for i in range(300))) is False  # long, but saying something new each time

    def test_a_model_that_loops_on_an_open_ended_prompt_fails_the_check_and_names_it(self, capsys):
        def loops(text, body):
            return {"message": {"role": "assistant", "content": "the same sentence again and again " * 80}, "done_reason": "length", "eval_count": 2048}

        assert tp.main([], _client(lambda p, body: _ok_reply(p), loops)) == 1
        out = capsys.readouterr().out
        assert "LOOP" in out and tp.OPEN_ENDED[0] in out

    def test_the_loop_half_runs_by_default_and_not_with_no_loops(self):
        asked = []

        def open_ended(text, body):
            asked.append(text)
            return {"message": {"role": "assistant", "content": "fine"}, "done_reason": "stop", "eval_count": 1}

        tp.main([], _client(lambda p, body: _ok_reply(p), open_ended))
        assert asked == list(tp.OPEN_ENDED)
        asked.clear()
        tp.main(["--no-loops"], _client(lambda p, body: _ok_reply(p), open_ended))
        assert asked == []


class TestStrictTools:
    """`--strict` sends the form the app sends under TOOL_CALLS_STRICT (pattern 74) and still judges the reply by the app's own rules."""

    def test_strict_sends_strict_schemas_and_not_strict_sends_the_plain_ones(self):
        sent = {}

        def reply(p, body):
            sent["strict"] = [t["function"].get("strict") for t in body["tools"]]
            return _ok_reply(p)

        tp.main(["--strict", "--no-loops"], _client(reply))
        assert all(sent["strict"])
        tp.main(["--no-strict", "--no-loops"], _client(reply))
        assert not any(sent["strict"])

    def test_leaving_a_nullable_argument_out_is_fine_and_leaving_a_plain_one_out_is_not(self):
        """The tools list every argument as required, but accept a call that leaves a nullable one out (`ToolArgs` fills it
        with None): qwen2.5:7b does exactly that. A required argument with no null, or a value outside an enum, is still bad."""
        employees = tp.Probe("who works in engineering?", "query_employees")
        assert tp.classify(employees, _call("query_employees", {"department": "Engineering"}), 21, SCHEMAS) == tp.OK
        assert tp.classify(employees, _call("query_employees", {"department": "Engineering", "name_contains": None}), 21, SCHEMAS) == tp.OK
        assert tp.classify(employees, _call("query_employees", {"department": "Marketing"}), 21, SCHEMAS) == tp.BAD_ARGS

"""loadtest/fake_llm_server.py's tool simulators: the one the real-API smoke leans on (specs/018, T068).

`query_employees` is simulated so that a real worker runs the REAL tool under the caller's tenant with no model in the way: the point of the
smoke's two-tenant check. If the simulator stopped matching, that check would not fail loudly, it would get a plain-text answer with no rows in it
and fail on the wrong thing; so the matching is pinned here.
"""
from loadtest import fake_llm_server as fake

TOOLS = [{"type": "function", "function": {"name": "query_employees"}}, {"type": "function", "function": {"name": "calculator"}}]


def _user(text: str) -> list[dict]:
    return [{"role": "system", "content": "s"}, {"role": "user", "content": text}]


def test_a_request_for_the_employees_calls_the_real_tool_with_no_filter():
    call = fake._pick_tool_call(_user("list the employees"), TOOLS)

    assert call is not None and (call.name, call.arguments) == ("query_employees", {})


def test_it_matches_however_the_word_is_cased():
    assert fake._pick_tool_call(_user("Please LIST the Employees now"), TOOLS) is not None


def test_a_message_that_is_not_about_employees_does_not_call_it():
    assert fake._pick_tool_call(_user("hello there"), TOOLS) is None


def test_it_is_not_called_when_the_tool_was_not_offered_to_the_model():
    only_calculator = [t for t in TOOLS if t["function"]["name"] == "calculator"]

    assert fake._pick_tool_call(_user("list the employees"), only_calculator) is None


def test_after_the_tool_has_answered_the_next_round_is_the_final_answer_and_it_carries_the_tools_result():
    messages = _user("list the employees") + [{"role": "tool", "content": "Priya Nair, Marcus Cole"}]

    assert fake._pick_tool_call(messages, TOOLS) is None
    assert "Priya Nair, Marcus Cole" in fake._final_answer_text(messages)


def test_the_older_simulators_still_win_for_their_own_messages():
    call = fake._pick_tool_call(_user("what is 12 * 7?"), TOOLS)

    assert call is not None and call.name == "calculator"

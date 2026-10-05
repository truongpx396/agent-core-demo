"""Tests for scripts/ai_review_providers.py: which providers the AI reviewer may try, in order.

The property that matters most is the key rule: one provider's API key must never be sent to another
provider's host. It is asserted directly (`test_no_fallback_ever_carries_the_primarys_key_to_another_host`)
over every combination of the settings, not just on the examples.
"""
import itertools
from pathlib import Path

import pytest

from scripts.ai_review import providers as p

PRIMARY = p.Provider("primary", "https://llm.example/v1", "main-model", "KEY-PRIMARY")


def build(**env):
    return p.build_fallbacks(env, PRIMARY)


OTHER = "https://other.example/v1"


def test_with_no_fallback_variables_there_are_no_fallbacks():
    assert build() == ()


def test_fallback_models_are_other_models_on_the_primarys_provider_with_the_primarys_key():
    chain = build(AI_REVIEW_FALLBACK_MODELS="lite-a, lite-b")
    assert chain == (
        p.Provider("fallback 1", "https://llm.example/v1", "lite-a", "KEY-PRIMARY"),
        p.Provider("fallback 2", "https://llm.example/v1", "lite-b", "KEY-PRIMARY"),
    )


def test_a_provider_group_is_another_host_with_its_own_key_and_every_model_listed():
    chain = build(
        AI_REVIEW_FALLBACK_PROVIDER1_MODELS="big,small",
        AI_REVIEW_FALLBACK_PROVIDER1_BASE_URL=OTHER,
        AI_REVIEW_FALLBACK_PROVIDER1_API_KEY="KEY-OTHER",
    )
    assert chain == (p.Provider("fallback 1", OTHER, "big", "KEY-OTHER"), p.Provider("fallback 2", OTHER, "small", "KEY-OTHER"))


def test_the_chain_order_is_the_primarys_own_models_then_provider_1_then_provider_2():
    chain = build(
        AI_REVIEW_FALLBACK_PROVIDER2_MODELS="z",
        AI_REVIEW_FALLBACK_PROVIDER2_BASE_URL="https://two.example/v1",
        AI_REVIEW_FALLBACK_PROVIDER1_MODELS="y1,y2",
        AI_REVIEW_FALLBACK_PROVIDER1_BASE_URL="https://one.example/v1",
        AI_REVIEW_FALLBACK_MODELS="x",
    )  # the order the variables are written in must not matter
    assert [(f.name, f.model, f.base_url) for f in chain] == [
        ("fallback 1", "x", "https://llm.example/v1"),
        ("fallback 2", "y1", "https://one.example/v1"),
        ("fallback 3", "y2", "https://one.example/v1"),
        ("fallback 4", "z", "https://two.example/v1"),
    ]


def test_a_provider_group_with_no_base_url_is_the_primarys_host_so_its_key_is_the_one_to_use_unless_it_has_its_own():
    (inherits,) = build(AI_REVIEW_FALLBACK_PROVIDER1_MODELS="m")
    assert (inherits.base_url, inherits.api_key) == ("https://llm.example/v1", "KEY-PRIMARY")
    (other_account,) = build(AI_REVIEW_FALLBACK_PROVIDER1_MODELS="m", AI_REVIEW_FALLBACK_PROVIDER1_API_KEY="KEY-OTHER-ACCOUNT")
    assert (other_account.base_url, other_account.api_key) == ("https://llm.example/v1", "KEY-OTHER-ACCOUNT")


def test_an_explicit_base_url_naming_the_same_host_still_inherits_the_key_whatever_its_trailing_slash():
    (fallback,) = build(AI_REVIEW_FALLBACK_PROVIDER1_MODELS="m", AI_REVIEW_FALLBACK_PROVIDER1_BASE_URL="https://llm.example/v1/")
    assert fallback.base_url == "https://llm.example/v1" and fallback.api_key == "KEY-PRIMARY"


def test_a_pasted_key_with_a_trailing_newline_is_stripped_before_it_reaches_a_header():
    (fallback,) = build(
        AI_REVIEW_FALLBACK_PROVIDER1_MODELS="m", AI_REVIEW_FALLBACK_PROVIDER1_BASE_URL=OTHER, AI_REVIEW_FALLBACK_PROVIDER1_API_KEY="  KEY-OTHER\n"
    )
    assert fallback.api_key == "KEY-OTHER"


def test_a_different_host_with_no_key_of_its_own_sends_none_never_the_primarys():
    (fallback,) = build(AI_REVIEW_FALLBACK_PROVIDER1_MODELS="m", AI_REVIEW_FALLBACK_PROVIDER1_BASE_URL=OTHER)
    assert fallback.api_key == ""


def test_no_fallback_ever_carries_the_primarys_key_to_another_host():
    bases = ["", "https://llm.example/v1", "https://llm.example/v1/", OTHER, "http://localhost:8080/v1"]
    keys = ["", "KEY-OWN"]
    for number, base, key in itertools.product((1, 2), bases, keys):
        prefix = f"AI_REVIEW_FALLBACK_PROVIDER{number}_"
        env = {prefix + "MODELS": "m", prefix + "BASE_URL": base, prefix + "API_KEY": key}
        chain = p.build_fallbacks(env, PRIMARY)
        assert chain, (number, base, key)  # the property below must not pass vacuously
        for fallback in chain:
            if fallback.base_url != PRIMARY.base_url:
                assert fallback.api_key in ("", key), (base, key)  # only ever its own key, or none
                assert fallback.api_key != "KEY-PRIMARY"


def test_a_group_without_models_is_off_even_if_it_has_a_url_and_a_key():
    assert build(AI_REVIEW_FALLBACK_PROVIDER1_BASE_URL=OTHER, AI_REVIEW_FALLBACK_PROVIDER1_API_KEY="k") == ()
    assert build(AI_REVIEW_FALLBACK_PROVIDER1_MODELS="   ", AI_REVIEW_FALLBACK_PROVIDER1_BASE_URL=OTHER) == ()
    assert build(AI_REVIEW_FALLBACK_PROVIDER1_MODELS="", AI_REVIEW_FALLBACK_MODELS="") == ()  # an unset variable arrives as ""


def test_names_follow_the_chain_so_an_off_first_group_does_not_leave_a_gap():
    chain = build(AI_REVIEW_FALLBACK_PROVIDER2_MODELS="b", AI_REVIEW_FALLBACK_PROVIDER2_BASE_URL=OTHER)
    assert [(f.name, f.model) for f in chain] == [("fallback 1", "b")]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, []), ("", []), (" , ,", []), ("a", ["a"]), (" a , b,,c ", ["a", "b", "c"]), ("a,b,a", ["a", "b"]), ("a b", ["a b"])],
)
def test_model_list_strips_and_drops_blanks_and_repeats_and_keeps_the_order(value, expected):
    assert p.model_list(value) == expected


def test_a_fallback_that_repeats_a_provider_already_in_the_chain_is_dropped():
    assert build(AI_REVIEW_FALLBACK_MODELS="main-model") == ()  # the primary's own model again
    chain = build(AI_REVIEW_FALLBACK_MODELS="a", AI_REVIEW_FALLBACK_PROVIDER1_MODELS="a")  # same host, same model
    assert [f.name for f in chain] == ["fallback 1"]
    assert len(build(AI_REVIEW_FALLBACK_MODELS="a", AI_REVIEW_FALLBACK_PROVIDER1_MODELS="a", AI_REVIEW_FALLBACK_PROVIDER1_BASE_URL=OTHER)) == 2


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y", "other.example/v1", "javascript:alert(1)"])
@pytest.mark.parametrize("number", [1, 2])
def test_a_provider_groups_base_url_must_be_http_or_https_like_the_primarys(url, number):
    prefix = f"AI_REVIEW_FALLBACK_PROVIDER{number}_"
    with pytest.raises(ValueError, match=f"{prefix}BASE_URL must be an http"):
        build(**{prefix + "MODELS": "m", prefix + "BASE_URL": url})


def test_only_the_documented_number_of_provider_groups_is_read():
    chain = build(**{f"AI_REVIEW_FALLBACK_PROVIDER{n}_MODELS": f"m{n}" for n in range(1, 6)})
    assert [f.model for f in chain] == ["m1", "m2"] and p.MAX_PROVIDERS == 2  # the same host: only 1 and 2 are read


def test_more_fallback_models_than_the_cap_is_an_error_that_names_the_variable_not_a_silent_truncation():
    at_cap = ",".join(f"m{n}" for n in range(p.MAX_FALLBACKS))
    assert len(build(AI_REVIEW_FALLBACK_MODELS=at_cap)) == p.MAX_FALLBACKS
    with pytest.raises(ValueError, match=rf"AI_REVIEW_FALLBACK_\* name {p.MAX_FALLBACKS + 1} fallback models; at most {p.MAX_FALLBACKS}"):
        build(AI_REVIEW_FALLBACK_MODELS=at_cap + ",one-too-many")
    with pytest.raises(ValueError, match="at most"):  # the cap counts every group together
        build(AI_REVIEW_FALLBACK_MODELS="a,b,c,d", AI_REVIEW_FALLBACK_PROVIDER1_MODELS="e,f,g", AI_REVIEW_FALLBACK_PROVIDER1_BASE_URL=OTHER)


WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ai-review.yml"


class _Spy(dict):
    """An env that remembers which names were looked up."""

    def __init__(self, *args):
        super().__init__(*args)
        self.read: set[str] = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)


def test_every_variable_the_builder_reads_is_passed_through_by_the_workflow_under_the_same_name():
    # A name that differs by one letter between the script and the workflow would not fail anything: the
    # fallback would just never be configured. So derive the names from the builder, not from a list here.
    env = _Spy({"AI_REVIEW_FALLBACK_MODELS": "a", **{f"AI_REVIEW_FALLBACK_PROVIDER{n}_MODELS": f"m{n}" for n in range(1, p.MAX_PROVIDERS + 1)}})
    p.build_fallbacks(env, PRIMARY)
    assert len(env.read) == 1 + 3 * p.MAX_PROVIDERS  # FALLBACK_MODELS, and MODELS / BASE_URL / API_KEY per group
    workflow = WORKFLOW.read_text()
    for name in sorted(env.read):
        source = "secrets" if name.endswith("_API_KEY") else "vars"  # a key is a secret, never a plain variable
        assert f"{name}: ${{{{ {source}.{name} }}}}" in workflow, name


# --- walk_chain: moving from one provider to the next, with no HTTP and no GitHub ------------------

FIRST = p.Provider("fallback 1", "https://llm.example/v1", "m2", "KEY-PRIMARY")
SECOND = p.Provider("fallback 2", "https://other.example/v1", "m3", "")
CHAIN = [PRIMARY, FIRST, SECOND]


class Boom(Exception):
    """Stands in for ReviewError: the exception type that means 'this provider failed for good'."""


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def walk(attempt, *, chain=CHAIN, http=None, sleep=None, clock=None, failure=Boom, notify=None):
    return p.walk_chain(chain, attempt, http=http or (lambda *a: None), sleep=sleep or (lambda s: None),
                        clock=clock or Clock(), failure=failure, notify=notify or (lambda line: None))


def test_a_single_provider_is_just_the_attempt_on_the_original_http_and_sleep_with_its_own_error():
    http, sleep, seen = object(), object(), []

    def attempt(provider, h, s):
        seen.append((provider, h, s))
        return "answer"

    assert walk(attempt, chain=[PRIMARY], http=http, sleep=sleep) == ("answer", PRIMARY)
    assert seen == [(PRIMARY, http, sleep)]  # not wrapped: no deadline, no clamping, nothing changed

    def failing(provider, h, s):
        raise Boom("the raw message")

    with pytest.raises(Boom, match=r"^the raw message$"):  # not rewrapped as "every provider failed"
        walk(failing, chain=[PRIMARY])


def test_the_first_provider_that_answers_wins_and_the_rest_are_never_asked():
    asked = []

    def attempt(provider, h, s):
        asked.append(provider.name)
        return f"from {provider.name}"

    assert walk(attempt) == ("from primary", PRIMARY) and asked == ["primary"]


def test_a_failure_moves_on_and_the_hand_over_is_announced_with_names_and_the_reason_only():
    notices = []

    def attempt(provider, h, s):
        if provider is PRIMARY:
            raise Boom("HTTP 429 RESOURCE_EXHAUSTED (after 3 attempts)")
        return "rescued"

    assert walk(attempt, notify=notices.append) == ("rescued", FIRST)
    assert notices == [
        "::notice::AI review: primary (main-model) failed (HTTP 429 RESOURCE_EXHAUSTED (after 3 attempts)); trying fallback 1 (m2)"
    ]
    assert "llm.example" not in notices[0] and "KEY-PRIMARY" not in notices[0]  # never a URL or a key


def test_when_every_provider_fails_the_error_is_the_callers_type_and_names_each_in_order():
    def attempt(provider, h, s):
        raise Boom(f"down: {provider.model}")

    with pytest.raises(Boom) as exc:
        walk(attempt)
    assert str(exc.value) == "every provider failed: primary (main-model): down: main-model; fallback 1 (m2): down: m2; fallback 2 (m3): down: m3"


def test_only_the_callers_failure_type_moves_on_and_anything_else_is_a_bug_that_must_surface():
    asked = []

    def buggy(provider, h, s):
        asked.append(provider.name)
        raise KeyError("a bug, not a provider failure")

    with pytest.raises(KeyError):
        walk(buggy)
    assert asked == ["primary"]  # the second provider was NOT tried: a bug must not hide behind it

    def wrong_type(provider, h, s):
        raise Boom("not what this walker was told to catch")

    with pytest.raises(Boom):
        walk(wrong_type, failure=ValueError)  # the exception type is a parameter, not a hard-coded class


def test_the_default_notify_prints_the_notice_to_the_log(capsys):
    def attempt(provider, h, s):
        if provider is PRIMARY:
            raise Boom("x")
        return "ok"

    p.walk_chain(CHAIN, attempt, http=lambda *a: None, sleep=lambda s: None, clock=Clock(), failure=Boom)
    assert capsys.readouterr().out.strip() == p.handover_notice(PRIMARY, "x", FIRST)


def test_each_request_is_clamped_to_the_time_left_on_the_shared_deadline():
    clock, seen = Clock(), []

    def http(method, url, headers, body, timeout):
        seen.append(timeout)

    def attempt(provider, h, s):
        h("POST", "u", {}, None, 180.0)  # every request asks for the usual 180s
        if provider is PRIMARY:
            clock.now += 400.0  # and the first provider burns 400s of the 480
            raise Boom("slow")
        return "ok"

    assert walk(attempt, http=http, clock=clock)[1] is FIRST
    assert seen == [180.0, 80.0]  # 480 - 400 left for the second, not another full 180


def test_a_request_never_gets_a_zero_or_negative_timeout_even_when_the_deadline_is_nearly_spent():
    clock, seen = Clock(), []

    def http(method, url, headers, body, timeout):
        seen.append(timeout)

    def attempt(provider, h, s):
        clock.now += 479.5  # half a second of the deadline left
        h("POST", "u", {}, None, 180.0)
        clock.now += 100.0  # now it is overrun
        h("POST", "u", {}, None, 180.0)
        return "ok"

    assert walk(attempt, http=http, clock=clock)[1] is PRIMARY
    assert seen == [1.0, 1.0]  # clamped to a one-second floor: a socket timeout of 0 or less is an error, not a limit


def test_a_retry_wait_that_would_overrun_the_deadline_raises_instead_of_sleeping():
    clock, slept = Clock(), []
    clock.now = 0.0

    def attempt(provider, h, s):
        clock.now = 478.0
        s(5.0)  # 2s are left, so a 5s wait cannot happen
        return "never reached"

    with pytest.raises(Boom, match=r"primary \(main-model\): out of time for this review; fallback 1 \(m2\): not tried, out of time"):
        walk(attempt, sleep=slept.append, clock=clock)
    assert slept == []


def test_a_provider_is_not_started_when_less_than_the_minimum_time_is_left():
    clock = Clock()

    def attempt(provider, h, s):
        clock.now += 470.0
        raise Boom("slow")

    with pytest.raises(Boom) as exc:
        walk(attempt, clock=clock)
    assert str(exc.value) == "every provider failed: primary (main-model): slow; fallback 1 (m2): not tried, out of time"


def test_the_wording_helpers_name_models_never_urls_or_keys():
    assert p.describe_chain(CHAIN) == "primary main-model, fallback 1 m2, fallback 2 m3"
    assert p.fallback_note("main-model") == " (fallback for `main-model`, which was unavailable)"
    assert p.fallback_note(None) == "" and p.fallback_note("") == ""
    shown = p.describe_chain(CHAIN) + p.handover_notice(PRIMARY, "r", FIRST)
    assert "example" not in shown and "KEY" not in shown

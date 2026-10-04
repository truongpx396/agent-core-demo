"""Hermetic tests for tests/deepeval/fallback.py: failover between LLM-judge models.

Deliberately NOT marked `deepeval`: these need no key, no model and no network, so they run in the default
suite (the old single-backup fallback in conftest.py had no tests at all, which is how a bug that stopped it
engaging on the one case it existed for survived). They need neither `deepeval` nor `google-genai` except the
`make_judge` ones, which skip when `deepeval` is not installed (the fast `test` job doesn't install it).

The exceptions are REAL ones, built the way deepeval's own retry policy builds them: a tenacity `Retrying`
with `reraise=False` around a call that raises a real `openai.RateLimitError`.
"""
import asyncio

import pytest
import tenacity

from tests.deepeval import fallback as f

GROQ_TPD = (
    "Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-120b` in organization "
    "`org_01SECRET` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199650, Requested 1321. "
    "Please try again in 6m59s.'}}"
)


def rate_limit(message: str = GROQ_TPD):
    import httpx
    import openai

    response = httpx.Response(429, request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"))
    return openai.RateLimitError(message, response=response, body=None)


def retry_error(inner: BaseException) -> tenacity.RetryError:
    """What deepeval 4.2.0's retry policy raises when its attempts run out (`reraise=False`)."""

    def always_fails():
        raise inner

    try:
        tenacity.Retrying(stop=tenacity.stop_after_attempt(2), wait=tenacity.wait_none(), reraise=False)(always_fails)
    except tenacity.RetryError as exc:
        return exc
    raise AssertionError("tenacity did not give up")  # pragma: no cover


class WithCode(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


class ServerError(Exception):  # google.genai.errors.ServerError, by name
    code = 503


# --- classifying: the bug --------------------------------------------------------------------


def test_the_real_ci_failure_a_retry_error_wrapping_a_rate_limit_is_transient_though_the_old_check_missed_it():
    import openai

    wrapped = retry_error(rate_limit())
    # Exactly why the old classifier (`isinstance(exc, openai.RateLimitError)` or `exc.code in (429, 5xx)`)
    # declared this non-transient and let the failure through with the backup configured and idle:
    assert not isinstance(wrapped, openai.RateLimitError) and getattr(wrapped, "code", None) is None
    assert f.is_transient(wrapped) is True


@pytest.mark.parametrize(
    "exc",
    [
        ServerError(),  # google.genai ServerError: a 5xx "high demand"
        WithCode(429),
        WithCode(500),
        WithCode(502),
        WithCode(503),
        WithCode(504),
        TimeoutError("slow"),
    ],
)
def test_rate_limits_overloads_and_timeouts_are_transient(exc):
    assert f.is_transient(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("a real bug"),
        KeyError("x"),
        WithCode(400),  # Gemini's INVALID_ARGUMENT schema error must surface, not be papered over
        WithCode(401),
        WithCode(403),
        WithCode(404),
    ],
)
def test_auth_bad_request_and_bugs_are_not_transient_so_a_fallback_never_hides_them(exc):
    assert f.is_transient(exc) is False


def test_a_retry_error_wrapping_something_that_is_not_transient_is_not_transient():
    assert f.is_transient(retry_error(ValueError("a bug the retries could not fix"))) is False


def test_a_retry_error_is_unwrapped_through_its_last_attempt_even_with_no_cause_set():
    import concurrent.futures

    attempt = concurrent.futures.Future()
    attempt.set_exception(rate_limit())
    bare = tenacity.RetryError(attempt)  # built directly, so no `raise ... from`: __cause__ is None
    assert bare.__cause__ is None
    assert f.is_transient(bare) is True  # only the last_attempt path can find the RateLimitError in here


def test_a_server_error_with_no_status_attribute_is_still_transient_by_its_class_name():
    class ServerError(Exception):  # the name google.genai uses for a 5xx, here with no `.code` at all
        pass

    assert getattr(ServerError(), "code", None) is None
    assert f.is_transient(ServerError("high demand")) is True


def test_the_cause_chain_is_followed_and_a_cycle_cannot_hang_it():
    outer = RuntimeError("wrapper")
    outer.__cause__ = WithCode(503)
    assert f.is_transient(outer) is True
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a  # a cycle
    assert f.is_transient(a) is False and len(f.chain_of(a)) == 2


def test_a_retry_error_whose_last_attempt_is_unfinished_or_cancelled_does_not_raise():
    class Pending:
        def done(self):
            return False

        def cancelled(self):
            return False

        def exception(self):  # pragma: no cover - must not be called
            raise AssertionError("an unfinished future has no exception yet")

    odd = RuntimeError("x")
    odd.last_attempt = Pending()
    assert f.chain_of(odd) == [odd]


def test_describe_names_the_class_and_status_never_the_provider_message():
    shown = f.describe(retry_error(rate_limit()))
    assert "RateLimitError" in shown and "org_01SECRET" not in shown and "200000" not in shown  # org id and quota figures stay out of public logs
    assert f.describe(ServerError()) == "ServerError 503"
    assert f.describe(ValueError("x")) == "ValueError"


@pytest.mark.parametrize(
    "message, seconds",
    [
        (GROQ_TPD, f.DAILY_COOLDOWN_S),
        ("quota GenerateRequestsPerDayPerProjectPerModel-FreeTier exceeded", f.DAILY_COOLDOWN_S),
        ("limit on requests per day (RPD) reached", f.DAILY_COOLDOWN_S),
        ("Rate limit reached on tokens per minute (TPM): Limit 8000", f.SHORT_COOLDOWN_S),
        ("The model is overloaded", f.SHORT_COOLDOWN_S),
    ],
)
def test_a_per_day_limit_cools_a_model_for_an_hour_and_anything_else_for_five_minutes(message, seconds):
    assert f.cooldown_for(retry_error(rate_limit(message))) == seconds  # found even when buried inside a RetryError


@pytest.mark.parametrize(
    "value, default, exclude, expected",
    [
        (None, ("a", "b"), (), ["a", "b"]),
        ("", ("a", "b"), (), ["a", "b"]),
        ("   ", ("a",), (), ["a"]),  # blank means unset: the workflow passes an unset variable as ""
        (",,", ("a", "b"), (), ["a", "b"]),  # no name in it at all: a typo must not silently switch the list off
        (" , ,", ("a",), (), ["a"]),
        ("primary", ("a",), ("primary",), []),  # but naming only the excluded primary really is an empty list
        ("x, y ,z", ("a",), (), ["x", "y", "z"]),
        ("x,,x, y", ("a",), (), ["x", "y"]),  # blanks and repeats dropped, order kept
        ("x,primary,y", ("a",), ("primary",), ["x", "y"]),  # the primary is never its own fallback
        ("none", ("a", "b"), (), []),  # the way to switch fallbacks off
        ("NONE", ("a",), (), []),
    ],
)
def test_model_list_reads_a_comma_separated_variable_with_sensible_defaults(value, default, exclude, expected):
    assert f.model_list(value, default, exclude) == expected


# --- the chain ---------------------------------------------------------------------------------


class FakeModel:
    """A model whose answers are scripted; returns deepeval's native `(content, cost)` tuple on success."""

    def __init__(self, name, *outcomes):
        self.name, self.outcomes, self.calls = name, list(outcomes), []

    def get_model_name(self):
        return self.name

    def _next(self, prompt, schema):
        self.calls.append((prompt, schema))
        outcome = self.outcomes.pop(0) if self.outcomes else f"answer-from-{self.name}"
        if isinstance(outcome, BaseException):
            raise outcome
        return (outcome, 0.0)

    def generate(self, prompt, schema=None):
        return self._next(prompt, schema)

    async def a_generate(self, prompt, schema=None):
        return self._next(prompt, schema)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def chain(*models, clock=None, notices=None, backups=()):
    kwargs = {"notify": (notices if notices is not None else []).append}
    if clock is not None:
        kwargs["clock"] = clock
    return f.build_chain(models[0], list(models[1:]), backups, **kwargs)


def test_the_primary_answers_alone_and_the_content_not_the_cost_tuple_is_returned():
    primary, second = FakeModel("p"), FakeModel("s")
    assert chain(primary, second).generate("hi", schema="SCHEMA") == "answer-from-p"
    assert primary.calls == [("hi", "SCHEMA")] and second.calls == []  # the schema reaches the model; nothing else is touched


def test_a_wrapped_rate_limit_hands_over_to_the_next_model_and_says_so_without_the_message():
    notices = []
    primary, second = FakeModel("gpt-oss-120b", retry_error(rate_limit())), FakeModel("gpt-oss-20b")
    assert chain(primary, second, notices=notices).generate("hi") == "answer-from-gpt-oss-20b"
    assert notices == ["[deepeval] gpt-oss-120b hit a transient error (RateLimitError 429); falling back to gpt-oss-20b"]


def test_a_failure_that_is_not_transient_is_raised_untouched_and_no_fallback_is_tried():
    bug = ValueError("Unknown name additional_properties")
    primary, second = FakeModel("p", bug), FakeModel("s")
    with pytest.raises(ValueError) as exc:
        chain(primary, second).generate("hi")
    assert exc.value is bug and second.calls == []  # a different model cannot fix a bad schema, and must not hide it


def test_when_every_model_is_transiently_down_the_last_error_is_raised_and_the_log_says_none_is_left():
    notices = []
    last = retry_error(rate_limit())
    models = [FakeModel("a", retry_error(rate_limit())), FakeModel("b", retry_error(rate_limit())), FakeModel("c", last)]
    with pytest.raises(tenacity.RetryError) as exc:
        chain(*models, notices=notices).generate("hi")
    assert exc.value is last and "falling back to b" in notices[0] and "falling back to c" in notices[1]
    assert "no model left to fall back to" in notices[2]


def test_the_chain_walks_in_order_primary_then_same_provider_models_then_the_backup():
    models = [FakeModel("p", ServerError()), FakeModel("m1", ServerError()), FakeModel("m2", ServerError()), FakeModel("backup")]
    assert chain(models[0], models[1], models[2], backups=[models[3]]).generate("hi") == "answer-from-backup"
    assert [len(m.calls) for m in models] == [1, 1, 1, 1]


def test_a_model_that_failed_is_skipped_for_the_cooldown_instead_of_paying_deepevals_retries_again():
    clock = Clock()
    primary, second = FakeModel("p", ServerError()), FakeModel("s")
    c = chain(primary, second, clock=clock)
    c.generate("one")  # primary fails, second answers
    c.generate("two")
    c.generate("three")
    assert len(primary.calls) == 1 and len(second.calls) == 3  # not retried on calls two and three
    clock.now += f.SHORT_COOLDOWN_S + 1
    c.generate("four")
    assert len(primary.calls) == 2  # a rolling window recovers, so it is tried again after the cooldown


def test_a_per_day_failure_keeps_a_model_out_for_an_hour():
    clock = Clock()
    primary, second = FakeModel("p", retry_error(rate_limit())), FakeModel("s")
    c = chain(primary, second, clock=clock)
    c.generate("one")
    clock.now += f.SHORT_COOLDOWN_S + 1
    c.generate("two")
    assert len(primary.calls) == 1  # still cooling: a daily limit will not clear in a CI run
    clock.now += f.DAILY_COOLDOWN_S
    c.generate("three")
    assert len(primary.calls) == 2


def test_when_every_model_is_cooling_they_are_all_tried_rather_than_none():
    clock = Clock()
    a, b = FakeModel("a", ServerError()), FakeModel("b", ServerError())
    c = chain(a, b, clock=clock)
    with pytest.raises(ServerError):  # the first call fails on both, so the last error is raised
        c.generate("one")
    assert c.generate("two") == "answer-from-a"  # both cooling, so the order is the original one and `a` is tried first


def test_an_empty_chain_is_refused():
    with pytest.raises(ValueError):
        f.FailoverChain([])


def test_build_chain_names_each_link_by_its_model_and_appends_every_backup_in_order():
    built = f.build_chain(FakeModel("p"), [FakeModel("m1")], [])
    assert [link.name for link in built._links] == ["p", "m1"]
    several = f.build_chain(FakeModel("p"), [FakeModel("m1")], [FakeModel("b1"), FakeModel("b2")])
    assert [link.name for link in several._links] == ["p", "m1", "b1", "b2"]  # same-provider models first, backups after
    assert [link.name for link in f.build_chain(FakeModel("p"))._links] == ["p"]  # both lists are optional


async def test_the_async_path_behaves_the_same_including_the_wrapped_rate_limit_and_the_content_unwrapping():
    primary, second = FakeModel("p", retry_error(rate_limit())), FakeModel("s")
    c = chain(primary, second)
    assert await c.a_generate("hi") == "answer-from-s"
    bug = ValueError("x")
    with pytest.raises(ValueError):
        await chain(FakeModel("p", bug), FakeModel("s")).a_generate("hi")


async def test_sync_and_async_calls_share_one_cooldown():
    primary, second = FakeModel("p", ServerError()), FakeModel("s")
    c = chain(primary, second)
    c.generate("sync")  # primary fails here...
    await c.a_generate("async")  # ...so the async call skips it
    assert len(primary.calls) == 1


# --- the DeepEvalBaseLLM adapter and the conftest wiring (need deepeval installed) ---------------


def test_make_judge_is_a_real_deepeval_model_named_for_the_primary_and_returns_content_only():
    pytest.importorskip("deepeval")
    from deepeval.models import DeepEvalBaseLLM

    judge = f.make_judge(chain(FakeModel("p", retry_error(rate_limit())), FakeModel("s")))
    assert isinstance(judge, DeepEvalBaseLLM)  # deepeval silently ignores anything else and grades with the wrong model
    assert judge.get_model_name() == "p"
    assert judge.generate("hi") == "answer-from-s"  # the content, not the (content, cost) tuple a native model returns
    assert asyncio.run(judge.a_generate("hi")) == "answer-from-s"  # the primary is cooling after the first call, so async skips it too


def test_conftest_returns_the_primary_unchanged_when_there_is_nothing_to_fall_back_to(monkeypatch):
    monkeypatch.delenv("PLUGSKY_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from tests.deepeval import conftest

    primary = FakeModel("p")
    assert conftest._with_fallbacks(primary, []) is primary  # a bare checkout behaves exactly as it always did


def test_conftest_wraps_the_primary_when_fallbacks_or_a_backup_exist(monkeypatch):
    pytest.importorskip("deepeval")
    from deepeval.models import DeepEvalBaseLLM

    from tests.deepeval import conftest

    monkeypatch.delenv("PLUGSKY_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    wrapped = conftest._with_fallbacks(FakeModel("p"), [FakeModel("s")])
    assert isinstance(wrapped, DeepEvalBaseLLM) and wrapped.get_model_name() == "p"


def test_the_default_fallbacks_are_models_with_their_own_quota_and_never_the_primary_itself():
    from tests.deepeval import conftest

    assert conftest.DEFAULT_JUDGE_FALLBACKS and conftest.DEEPEVAL_JUDGE_MODEL not in conftest.DEFAULT_JUDGE_FALLBACKS
    assert conftest.DEEPEVAL_CONVERSATION_JUDGE_MODEL not in conftest.DEFAULT_CONVERSATION_JUDGE_FALLBACKS
    # the conversation judge needs Groq's STRICT structured outputs, which per Groq's docs only these support:
    assert set(conftest.DEFAULT_CONVERSATION_JUDGE_FALLBACKS) <= {"openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"}

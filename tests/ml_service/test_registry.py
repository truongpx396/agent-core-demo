"""The model registry: spec validation and the load state machine (docker/ml-service/ml_service/registry.py). The clock is injected,
so the whole backoff sequence runs without sleeping."""
import asyncio
import threading

import pytest

from ml_service.registry import Registry, Slot, State, enabled_specs, parse_model_specs

SHA = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
VALID = f"""
[[model]]
name = "bge-small-en-v1.5"
roles = ["embed", "classify"]
repo = "BAAI/bge-small-en-v1.5"
file = "onnx/model.onnx"
revision = "{SHA}"
pooling = "cls"
max_length = 512
pad_token = "[PAD]"
licence = "MIT"
needs_prefix = {{ query = "Represent this sentence for searching relevant passages: ", document = "" }}
"""


def _toml(**overrides) -> str:
    base = {"name": '"m"', "roles": '["embed"]', "repo": '"r/r"', "file": '"f.onnx"', "revision": f'"{SHA}"', "pooling": '"cls"',
            "max_length": "512", "pad_token": '"[PAD]"', "licence": '"MIT"'}
    base.update({k: v for k, v in overrides.items() if v is not None})
    body = "\n".join(f"{k} = {v}" for k, v in base.items() if k not in overrides or overrides[k] is not None)
    return "[[model]]\n" + body


# ---- models.toml validation -------------------------------------------------------------------------------------------------
def test_a_valid_entry_parses_with_its_documentation_prefixes():
    (spec,) = parse_model_specs(VALID)
    assert spec.name == "bge-small-en-v1.5" and spec.roles == ("embed", "classify") and spec.normalize is True and spec.enabled is True
    assert spec.needs_prefix["query"].startswith("Represent this sentence")


def test_an_empty_file_declares_no_models():
    assert parse_model_specs("# nothing yet\n") == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"licence": None}, "missing required field 'licence'"),
        ({"licence": '"  "'}, "licence must not be empty"),
        ({"revision": '"main"'}, "full 40-hex commit sha"),
        ({"revision": '"5c38ec7c40"'}, "full 40-hex commit sha"),  # the short form a model page shows
        ({"roles": '["embed", "chat"]'}, "subset of"),
        ({"roles": "[]"}, "non-empty subset"),
        ({"pooling": '"max"'}, "pooling must be one of"),
        ({"max_length": "0"}, "max_length must be positive"),
        ({"name": '"has space"'}, "name must match"),
        ({"pad_token": None}, "missing required field 'pad_token'"),
    ],
)
def test_an_invalid_entry_is_refused_at_load_time(overrides, message):
    with pytest.raises(ValueError, match=message):
        parse_model_specs(_toml(**overrides))


def test_a_duplicate_name_is_refused():
    with pytest.raises(ValueError, match="duplicate name"):
        parse_model_specs(_toml() + "\n" + _toml())


def test_the_enabled_override_wins_over_the_registry_flags_and_unknown_names_fail():
    specs = parse_model_specs(VALID + "\n" + _toml(name='"other"', enabled="false"))
    assert [s.name for s in enabled_specs(specs, ())] == ["bge-small-en-v1.5"]  # `enabled = false` is honoured by default
    assert [s.name for s in enabled_specs(specs, ("other",))] == ["other"]  # the override can turn a disabled model on
    with pytest.raises(ValueError, match="does not declare"):
        enabled_specs(specs, ("nope",))


# ---- the state machine ------------------------------------------------------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _registry(clock=None, **kwargs):
    seen: list[tuple[str, State]] = []
    registry = Registry(clock=clock or Clock(), on_state=lambda n, s: seen.append((n, s)), **kwargs)
    return registry, seen


def test_the_backoff_doubles_from_five_seconds_and_stops_at_the_cap():
    registry, _ = _registry()
    assert [registry.backoff_s(n) for n in range(1, 9)] == [5, 10, 20, 40, 80, 160, 300, 300]


async def test_a_good_loader_ends_ready_and_the_states_are_reported_in_order():
    registry, seen = _registry()
    registry.add(Slot(name="a", roles=("embed",), loader=lambda: "model-a", info={"dim": 4}))

    assert await registry.load("a") is True

    assert registry.ready_value("a") == "model-a"
    assert [s for _, s in seen] == [State.NOT_LOADED, State.LOADING, State.READY]
    assert registry.snapshot()["a"]["state"] == "ready" and registry.snapshot()["a"]["dim"] == 4
    assert registry.degraded is False


async def test_a_failing_loader_ends_failed_with_a_reason_and_a_retry_time_and_is_not_served():
    clock = Clock()
    registry, _ = _registry(clock)

    def boom():
        raise OSError("no such file")

    registry.add(Slot(name="a", roles=("embed",), loader=boom))

    assert await registry.load("a") is False

    item = registry.snapshot()["a"]
    assert item["state"] == "failed" and "OSError: no such file" in item["reason"] and item["retry_in_s"] == 5.0
    assert registry.ready_value("a") is None and registry.degraded is True
    assert registry.retry_after_s("a") == 5


async def test_one_models_failure_never_touches_another():
    registry, _ = _registry()
    registry.add(Slot(name="bad", roles=("embed",), loader=lambda: (_ for _ in ()).throw(RuntimeError("x"))))
    registry.add(Slot(name="good", roles=("embed",), loader=lambda: "ok"))

    await registry.load_all()

    assert registry.ready_value("good") == "ok" and registry.ready_value("bad") is None
    assert registry.degraded is True


async def test_a_failed_model_is_retried_only_after_its_backoff_and_recovery_resets_it():
    clock = Clock()
    registry, _ = _registry(clock)
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("not yet")
        return "up"

    registry.add(Slot(name="a", roles=("embed",), loader=flaky))
    await registry.load("a")  # failure 1: retry in 5 s

    assert await registry.retry_due() == []  # too early: it is not hammered
    clock.now += 4.9
    assert await registry.retry_due() == []
    clock.now += 0.2
    assert await registry.retry_due() == ["a"]  # failure 2: retry in 10 s
    assert registry.snapshot()["a"]["retry_in_s"] == 10.0
    clock.now += 10.1
    assert await registry.retry_due() == ["a"]  # succeeds

    assert registry.ready_value("a") == "up" and registry.slot("a").failures == 0  # type: ignore[union-attr]
    assert await registry.retry_due() == []  # nothing left to retry


async def test_loading_is_single_flight():
    # The first load is held inside its loader until the second call has been made, so "a load is in flight" is true by construction.
    # (Racing two calls of an instant loader is not a test of this: on a fast machine the loader can finish before the second call
    # starts, and then there is nothing in flight to leave alone. That is how this test once failed on a CI runner and not on a laptop.)
    registry, _ = _registry()
    release = threading.Event()
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        assert release.wait(5), "the test never released the loader"
        return "m"

    registry.add(Slot(name="a", roles=("embed",), loader=loader))

    first = asyncio.ensure_future(registry.load("a"))
    while registry.slot("a").state is not State.LOADING:  # type: ignore[union-attr]
        await asyncio.sleep(0)
    second = await registry.load("a")  # returns at once: the slot is loading, so it is left alone
    release.set()

    assert second is False and await first is True and calls["n"] == 1


async def test_an_activation_failure_counts_as_a_failed_load():
    registry, _ = _registry()

    async def activate(_value):
        raise RuntimeError("could not start the batcher")

    registry.add(Slot(name="a", roles=("embed",), loader=lambda: "m", activate=activate))

    assert await registry.load("a") is False
    assert registry.snapshot()["a"]["state"] == "failed" and registry.ready_value("a") is None


async def test_a_run_error_on_a_ready_model_makes_it_failed_and_a_no_op_otherwise():
    registry, _ = _registry()
    registry.add(Slot(name="a", roles=("embed",), loader=lambda: "m"))
    registry.fail("a", "ignored: not ready yet")
    assert registry.snapshot()["a"]["state"] == "not_loaded"

    await registry.load("a")
    registry.fail("a", "CUDA out of memory")

    assert registry.snapshot()["a"]["state"] == "failed" and registry.ready_value("a") is None


async def test_a_slot_name_can_only_be_added_once():
    registry, _ = _registry()
    registry.add(Slot(name="a", roles=("embed",), loader=lambda: 1))
    with pytest.raises(ValueError, match="duplicate slot"):
        registry.add(Slot(name="a", roles=("embed",), loader=lambda: 2))


async def test_close_deactivates_what_was_activated():
    registry, _ = _registry()
    stopped: list[str] = []

    async def deactivate(value):
        stopped.append(value)

    registry.add(Slot(name="a", roles=("embed",), loader=lambda: "m", deactivate=deactivate))
    await registry.load("a")
    await registry.close()

    assert stopped == ["m"]

"""Tests for app/agent/pricing.py (spec 008 A2).

Three layers, none needing a live proxy:

  * `parse_model_info` and `cost_usd` are pure — a LiteLLM `/model/info` payload
    in, a price or a dollar figure out;
  * `get_price` runs the module's REAL fetch over `httpx.MockTransport` (the
    autouse `mock_model_pricing` fixture replaces it, so this file restores it),
    which exercises the request-building, the cache, the refresh and the
    keep-the-last-good-prices-on-failure rules;
  * the agent node and the turn entry point are driven with a patched fetch, to
    show the price actually reaches the running cost and the refusal.

Prices below are LiteLLM's real gpt-4o figures (per TOKEN, its unit).
"""
import math
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.agent import pricing
from app.agent import runtime as runtime_module
from app.agent import runtime_stream as stream_module
from app.agent.graph_agent_node import make_agent_node
from app.core import errors, metrics
from tests.conftest import TEST_CTX, metric_value

_REAL_FETCH = pricing._fetch_model_info

GPT_4O = {"input_cost_per_token": 2.5e-06, "output_cost_per_token": 1e-05, "cache_read_input_token_cost": 1.25e-06}
FREE = {"input_cost_per_token": 0.0, "output_cost_per_token": 0.0}


def _entry(alias, info):
    return {"model_name": alias, "model_info": info}


def _price(**overrides):
    return pricing.ModelPrice(**{"input_per_token": 2.5e-06, "output_per_token": 1e-05, **overrides})


def _patch_fetch(monkeypatch, *entries):
    async def fetch():
        return list(entries)

    monkeypatch.setattr(pricing, "_fetch_model_info", fetch)


class TestParseModelInfo:
    def test_reads_input_output_and_cache_read_rates(self):
        prices = pricing.parse_model_info([_entry("chat", GPT_4O)])

        assert prices == {"chat": pricing.ModelPrice(2.5e-06, 1e-05, 1.25e-06)}

    def test_a_model_litellm_states_as_free_is_priced_at_zero_not_unknown(self):
        """The line between Ollama and an unpriced model: `0.0` is an answer."""
        assert pricing.parse_model_info([_entry("chat", FREE)])["chat"] == pricing.ModelPrice(0.0, 0.0, None)

    @pytest.mark.parametrize(
        "info",
        [
            {},
            {"input_cost_per_token": 1e-06},  # half a price is not a price
            {"output_cost_per_token": 1e-06},
            {"input_cost_per_token": None, "output_cost_per_token": None},
            {"input_cost_per_token": -1e-06, "output_cost_per_token": 1e-06},
            {"input_cost_per_token": math.nan, "output_cost_per_token": 1e-06},
            {"input_cost_per_token": True, "output_cost_per_token": 1e-06},
            {"input_cost_per_token": "0.000001", "output_cost_per_token": "0.000002"},
        ],
    )
    def test_anything_short_of_a_full_valid_price_is_unknown(self, info):
        assert pricing.parse_model_info([_entry("chat", info)]) == {"chat": None}

    def test_an_alias_served_by_several_deployments_is_priced_at_the_dearest_of_each_rate(self):
        prices = pricing.parse_model_info(
            [
                _entry("chat", {"input_cost_per_token": 1e-06, "output_cost_per_token": 9e-06, "cache_read_input_token_cost": 1e-07}),
                _entry("chat", {"input_cost_per_token": 3e-06, "output_cost_per_token": 2e-06, "cache_read_input_token_cost": 5e-07}),
            ]
        )

        assert prices["chat"] == pricing.ModelPrice(3e-06, 9e-06, 5e-07)

    def test_one_unpriced_deployment_makes_the_whole_alias_unknown(self):
        """A ceiling has to bound the worst case; with one deployment's price
        missing there is no bound to compute."""
        prices = pricing.parse_model_info([_entry("chat", GPT_4O), _entry("chat", {})])

        assert prices == {"chat": None}

    def test_cached_tokens_fall_back_to_the_input_rate_if_any_deployment_states_no_cache_rate(self):
        prices = pricing.parse_model_info([_entry("chat", GPT_4O), _entry("chat", {**GPT_4O, "cache_read_input_token_cost": None})])

        assert prices["chat"].cache_read_per_token is None

    def test_entries_without_a_model_name_are_ignored(self):
        assert pricing.parse_model_info([{"model_info": GPT_4O}, _entry("chat", GPT_4O)]).keys() == {"chat"}


class TestCostUsd:
    def test_input_and_output_are_billed_at_their_own_rates(self):
        assert pricing.cost_usd(_price(), 1000, 500) == pytest.approx(1000 * 2.5e-06 + 500 * 1e-05)

    def test_output_tokens_cost_more_than_the_same_number_of_input_tokens(self):
        """The reason the old single rate per total token was wrong: the same
        total costs different amounts depending on its split."""
        assert pricing.cost_usd(_price(), 0, 1000) > pricing.cost_usd(_price(), 1000, 0)

    def test_cached_input_is_carved_out_of_the_input_and_billed_at_the_cache_rate(self):
        cost = pricing.cost_usd(_price(cache_read_per_token=1.25e-06), 1000, 0, cached_input_tokens=600)

        assert cost == pytest.approx(400 * 2.5e-06 + 600 * 1.25e-06)

    def test_cached_input_with_no_stated_cache_rate_is_billed_at_the_full_input_rate(self):
        cost = pricing.cost_usd(_price(), 1000, 0, cached_input_tokens=600)

        assert cost == pytest.approx(1000 * 2.5e-06)

    def test_a_cached_count_above_the_input_count_is_clamped_not_made_negative(self):
        cost = pricing.cost_usd(_price(cache_read_per_token=1.25e-06), 100, 0, cached_input_tokens=5000)

        assert cost == pytest.approx(100 * 1.25e-06)

    def test_negative_token_counts_cost_nothing(self):
        assert pricing.cost_usd(_price(), -5, -5, -5) == 0.0


@pytest.fixture
def served(monkeypatch):
    """Route the REAL `_fetch_model_info` through a MockTransport. Returns the
    requests seen and a mutable `body` the handler answers with."""
    monkeypatch.setattr(pricing, "_fetch_model_info", _REAL_FETCH)
    state = SimpleNamespace(seen=[], body={"data": [_entry("chat", GPT_4O)]}, status=200)

    def respond(request: httpx.Request) -> httpx.Response:
        state.seen.append(request)
        return httpx.Response(state.status, json=state.body)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        pricing.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(respond), **kwargs)
    )
    return state


@pytest.fixture
def clock(monkeypatch):
    """A controllable `time.monotonic` for the pricing module only."""
    now = [1000.0]
    monkeypatch.setattr(pricing, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


class TestGetPrice:
    async def test_asks_the_proxy_admin_endpoint_with_the_api_key(self, monkeypatch, served):
        monkeypatch.setattr(pricing, "admin_base_url", lambda: "http://litellm.test:4000")
        monkeypatch.setattr(pricing, "OPENAI_API_KEY", "sk-test")

        assert await pricing.get_price("chat") == pricing.ModelPrice(2.5e-06, 1e-05, 1.25e-06)

        (request,) = served.seen
        assert str(request.url) == "http://litellm.test:4000/model/info"
        assert request.headers["authorization"] == "Bearer sk-test"

    async def test_one_read_prices_every_alias_and_is_not_repeated_while_fresh(self, served, clock):
        served.body = {"data": [_entry("chat", GPT_4O), _entry("fast", FREE)]}

        await pricing.get_price("chat")
        await pricing.get_price("fast")
        await pricing.get_price("chat")

        assert len(served.seen) == 1

    async def test_an_alias_the_proxy_does_not_list_is_unknown(self, served):
        assert await pricing.get_price("nope") is None

    async def test_prices_are_read_again_once_they_are_older_than_the_refresh_interval(
        self, monkeypatch, served, clock
    ):
        monkeypatch.setattr(pricing, "PRICING_REFRESH_SECONDS", 3600)
        assert (await pricing.get_price("chat")).input_per_token == 2.5e-06

        served.body = {"data": [_entry("chat", {**GPT_4O, "input_cost_per_token": 9e-06})]}
        clock[0] += 3599
        assert (await pricing.get_price("chat")).input_per_token == 2.5e-06  # still fresh
        clock[0] += 2
        assert (await pricing.get_price("chat")).input_per_token == 9e-06  # picked up, no restart

        assert len(served.seen) == 2

    async def test_a_failed_refresh_keeps_serving_the_last_good_prices_and_is_counted(
        self, monkeypatch, served, clock
    ):
        monkeypatch.setattr(pricing, "PRICING_REFRESH_SECONDS", 3600)
        await pricing.get_price("chat")
        served.status = 503
        clock[0] += 4000
        before = metric_value(metrics.agent_cost_governance_degraded_total, path="price_lookup")

        price = await pricing.get_price("chat")

        assert price == pricing.ModelPrice(2.5e-06, 1e-05, 1.25e-06)
        assert metric_value(metrics.agent_cost_governance_degraded_total, path="price_lookup") == before + 1

    async def test_a_failed_read_is_not_repeated_on_every_call_during_the_backoff(self, served, clock):
        served.status = 503

        for _ in range(5):
            assert await pricing.get_price("chat") is None

        assert len(served.seen) == 1
        clock[0] += pricing.FAILED_LOOKUP_RETRY_SECONDS + 1
        await pricing.get_price("chat")
        assert len(served.seen) == 2

    async def test_a_process_that_has_never_reached_the_proxy_sees_no_price(self, served):
        served.status = 500

        assert await pricing.get_price("chat") is None


class TestPriceUsage:
    @pytest.fixture(autouse=True)
    def _priced(self, monkeypatch):
        _patch_fetch(monkeypatch, _entry("gpt", GPT_4O), _entry("local", FREE))

    async def test_prices_a_call_from_its_input_output_split(self):
        cost = await pricing.price_usage("gpt", {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500})

        assert cost == pytest.approx(0.0075)

    async def test_applies_the_cache_read_rate_to_the_cached_share_of_the_input(self):
        usage = {
            "input_tokens": 1000,
            "output_tokens": 500,
            "total_tokens": 1500,
            "input_token_details": {"cache_read": 600},
        }

        assert await pricing.price_usage("gpt", usage) == pytest.approx(0.00675)

    async def test_a_total_with_no_split_is_billed_at_the_dearer_rate_never_the_cheaper(self):
        cost = await pricing.price_usage("gpt", {"total_tokens": 1000})

        assert cost == pytest.approx(1000 * 1e-05)

    async def test_a_free_local_model_costs_zero_and_is_not_counted_as_unpriced(self):
        before = metric_value(metrics.agent_unpriced_usage_total, model_alias="local")

        cost = await pricing.price_usage("local", {"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000})

        assert cost == 0.0
        assert metric_value(metrics.agent_unpriced_usage_total, model_alias="local") == before

    async def test_an_unpriced_model_costs_zero_but_is_counted_on_every_call(self):
        before = metric_value(metrics.agent_unpriced_usage_total, model_alias="mystery")
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

        assert await pricing.price_usage("mystery", usage) == 0.0
        assert await pricing.price_usage("mystery", usage) == 0.0

        assert metric_value(metrics.agent_unpriced_usage_total, model_alias="mystery") == before + 2

    async def test_an_unpriced_model_warns_once_per_alias_not_once_per_call(self, caplog):
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

        with caplog.at_level("WARNING", logger=pricing.logger.name):
            for _ in range(3):
                await pricing.price_usage("mystery", usage)

        assert [r.getMessage() for r in caplog.records].count(
            "model has no known price; dollar ceilings cannot see its spend"
        ) == 1

    async def test_a_call_that_spent_no_tokens_costs_nothing_and_is_not_even_looked_up(self, monkeypatch):
        async def fail():
            raise AssertionError("no tokens were spent; there is nothing to price")

        monkeypatch.setattr(pricing, "_fetch_model_info", fail)

        assert await pricing.price_usage("gpt", {}) == 0.0
        assert await pricing.price_usage("gpt", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}) == 0.0


class TestRefuseUnpriced:
    @pytest.fixture(autouse=True)
    def _priced(self, monkeypatch):
        _patch_fetch(monkeypatch, _entry("gpt", GPT_4O), _entry("local", FREE), _entry("mystery", {}))

    async def test_the_allow_policy_never_refuses(self, monkeypatch):
        monkeypatch.setattr(pricing, "UNPRICED_MODEL_POLICY", "allow")

        assert await pricing.refuse_unpriced("mystery") is False

    async def test_the_block_policy_refuses_an_unpriced_model(self, monkeypatch):
        monkeypatch.setattr(pricing, "UNPRICED_MODEL_POLICY", "block")

        assert await pricing.refuse_unpriced("mystery") is True
        assert await pricing.refuse_unpriced("not-in-litellm-at-all") is True

    async def test_the_block_policy_serves_a_priced_model_and_a_free_one(self, monkeypatch):
        monkeypatch.setattr(pricing, "UNPRICED_MODEL_POLICY", "block")

        assert await pricing.refuse_unpriced("gpt") is False
        assert await pricing.refuse_unpriced("local") is False


class TestTheAgentNodePricesWithIt:
    """The price has to reach `total_cost_usd` — the number the per-turn ceiling
    stops on and the ledger row is written from."""

    @staticmethod
    def _llm(input_tokens, output_tokens):
        message = AIMessage(
            content="an answer",
            usage_metadata={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        )
        return GenericFakeChatModel(messages=iter([message]))

    async def test_the_running_cost_is_the_priced_input_output_split(self, monkeypatch):
        from app.core.config import CHAT_MODEL

        _patch_fetch(monkeypatch, _entry(CHAT_MODEL, GPT_4O))
        agent = make_agent_node(self._llm(1000, 500))

        result = await agent({"messages": [HumanMessage(content="hi")], "total_cost_usd": 0.25})

        assert result["total_cost_usd"] == pytest.approx(0.25 + 0.0075)
        assert result["total_tokens"] == 1500

    async def test_a_delegated_run_is_priced_by_its_own_alias_not_the_parents(self, monkeypatch):
        """Spec 008 A2: the in-run ceiling used to price every step by the global
        chat alias even when the specialist declared a dearer model of its own."""
        from app.core.config import CHAT_MODEL

        _patch_fetch(monkeypatch, _entry(CHAT_MODEL, FREE), _entry("specialist", GPT_4O))
        agent = make_agent_node(self._llm(1000, 500), model_alias="specialist")

        result = await agent({"messages": [HumanMessage(content="hi")]})

        assert result["total_cost_usd"] == pytest.approx(0.0075)

    async def test_an_unpriced_model_leaves_the_running_cost_at_zero_and_is_counted(self, monkeypatch):
        from app.core.config import CHAT_MODEL

        _patch_fetch(monkeypatch, _entry(CHAT_MODEL, {}))
        before = metric_value(metrics.agent_unpriced_usage_total, model_alias=CHAT_MODEL)
        agent = make_agent_node(self._llm(1000, 500))

        result = await agent({"messages": [HumanMessage(content="hi")]})

        assert result["total_cost_usd"] == 0.0
        assert metric_value(metrics.agent_unpriced_usage_total, model_alias=CHAT_MODEL) == before + 1


class TestTurnsOnAnUnpricedModelAreRefusedUnderTheBlockPolicy:
    @staticmethod
    def _forbid_graph_access(monkeypatch):
        async def _boom(*args, **kwargs):
            raise AssertionError("a refused turn must never reach the graph")

        monkeypatch.setattr(runtime_module, "init_graph_async", _boom)

    async def test_the_turn_is_refused_with_the_model_unpriced_code_before_any_model_work(self, monkeypatch):
        from app.core.config import CHAT_MODEL

        _patch_fetch(monkeypatch, _entry(CHAT_MODEL, {}))
        monkeypatch.setattr(pricing, "UNPRICED_MODEL_POLICY", "block")
        self._forbid_graph_access(monkeypatch)
        rejected = metric_value(metrics.agent_requests_total, outcome="rejected")

        events = [event async for event in stream_module.astream_events_turn("hi", "t1", TEST_CTX)]

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["code"] == errors.ErrorCode.MODEL_UNPRICED.value
        assert metric_value(metrics.agent_requests_total, outcome="rejected") == rejected + 1

    async def test_the_same_turn_is_not_refused_under_the_allow_policy(self, monkeypatch):
        from app.core.config import CHAT_MODEL

        _patch_fetch(monkeypatch, _entry(CHAT_MODEL, {}))
        monkeypatch.setattr(pricing, "UNPRICED_MODEL_POLICY", "allow")

        assert await runtime_module._chat_model_refused_as_unpriced() is False

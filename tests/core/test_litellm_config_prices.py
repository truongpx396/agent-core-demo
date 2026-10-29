"""Every local model in the dev gateway config states a price, and the per-person budget the configs name is the
one the script creates.

Why a price for a free local model: a model LiteLLM has no price for is `null` to the app (every call counted
unpriced, and `UNPRICED_MODEL_POLICY=block` would refuse it), and a model priced at exactly 0 is skipped by
LiteLLM's budget checks, so neither the app key's `max_budget` nor the per-person budget could trip. Measured
on the 1.94.1 image before the prices were added: `chat`, `chat-backup` and `embed` reported 0 and `vision`
null. These are shadow prices, not a bill (litellm-config.yaml says so); this file keeps them real, plain
numbers the app can read.

Static, reading YAML. That LiteLLM computes spend from them was run against its Router in a scratch script (a
`model_info` price gave 10 x 1e-7 + 20 x 2e-7 = 5e-6; a model with none gave nothing); a mock can't show it.
"""
from collections import defaultdict
from pathlib import Path

import yaml

from app.agent.cost.pricing import parse_model_info
from scripts import litellm_key

REPO = Path(__file__).resolve().parents[2]
DEV = REPO / "deploy/litellm/litellm-config.yaml"
PROD = REPO / "deploy/litellm/litellm-config.prod.yaml"


def _config(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _dev_entries() -> list[dict]:
    return _config(DEV)["model_list"]


def _prices(entry: dict) -> tuple:
    info = entry["model_info"]
    return info["input_cost_per_token"], info["output_cost_per_token"]


def test_every_dev_alias_states_a_price():
    missing = [e["model_name"] for e in _dev_entries() if "input_cost_per_token" not in e.get("model_info", {}) or "output_cost_per_token" not in e["model_info"]]

    assert not missing, f"unpriced in the dev config: {missing}"


def test_the_prices_are_numbers_not_strings():
    """PyYAML reads `1e-7` (no dot) as the STRING '1e-7'; LiteLLM would then read the model as unpriced. Decimals only."""
    for entry in _dev_entries():
        for value in _prices(entry):
            assert isinstance(value, float), f"{entry['model_name']}: {value!r} is a {type(value).__name__}, write it as a decimal"


def test_no_alias_is_priced_at_exactly_zero_because_litellm_skips_the_budget_check_for_such_a_model():
    for entry in _dev_entries():
        input_price, output_price = _prices(entry)
        assert input_price > 0 and output_price >= 0, entry["model_name"]  # an embedding has no output tokens, so only its input must be above 0


def test_one_underlying_model_has_one_price():
    """A deployment with no price of its own inherits the one LiteLLM registered for the same underlying model, so two aliases of one model
    with different prices would make a call's price depend on which deployment was registered first."""
    by_model = defaultdict(set)
    for entry in _dev_entries():
        by_model[entry["litellm_params"]["model"]].add(_prices(entry))

    assert {model: prices for model, prices in by_model.items() if len(prices) > 1} == {}


def test_a_stronger_tier_never_costs_less_than_a_weaker_one():
    """Escalating to the strong model is a decision about quality at a higher cost; a shadow price that said otherwise would make the router's
    cost-quality curve (`make model-eval KIND=classifier`) lie."""
    price = {e["model_name"]: _prices(e) for e in _dev_entries()}
    easy, medium, strong = price["chat"], price["chat-medium"], price["chat-strong"]

    assert all(e <= m <= s for e, m, s in zip(easy, medium, strong, strict=True))
    assert all(e < s for e, s in zip(easy, strong, strict=True))


def test_the_app_reads_every_dev_price_through_its_own_parser():
    """`/model/info` echoes `model_info` back; the parser the app uses turns each entry into a price, or None for an unpriced one. A mistyped
    key (`input_cost_per_tokens`) would pass every test above and still read as unpriced here."""
    info = [{"model_name": e["model_name"], "model_info": e["model_info"]} for e in _dev_entries()]

    parsed = parse_model_info(info)

    for entry in _dev_entries():
        price = parsed[entry["model_name"]]
        assert price is not None, f"{entry['model_name']} would be unpriced"
        assert (price.input_per_token, price.output_per_token) == _prices(entry)


def test_both_configs_name_the_per_person_budget_the_script_creates():
    for path in (DEV, PROD):
        assert _config(path)["litellm_settings"]["max_end_user_budget_id"] == litellm_key.PERSON_BUDGET_ID, path.name


def test_neither_config_uses_the_retired_float_setting_for_the_same_thing():
    """LiteLLM's docs say `max_end_user_budget` "is no longer enforced": a cap set there would look configured and do nothing."""
    for path in (DEV, PROD):
        assert "max_end_user_budget" not in _config(path)["litellm_settings"], path.name

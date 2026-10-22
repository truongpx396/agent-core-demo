"""scripts/gateway_check.py — the pure parts. Running the pinned image needs Docker and is the script's own job (`make gateway-check`); what is
pinned here is what it is built from: the image it runs, the config it feeds the gateway, and how it decides that something leaked."""
import re
from pathlib import Path

import yaml

from scripts import gateway_check

REPO = Path(__file__).resolve().parents[2]
DEV = yaml.safe_load((REPO / "deploy/litellm/litellm-config.yaml").read_text())


def test_it_runs_the_image_the_dev_stack_runs_which_is_pinned_by_digest():
    image = gateway_check.pinned_image()

    assert re.fullmatch(r"ghcr\.io/berriai/litellm:main-stable@sha256:[0-9a-f]{64}", image)


def test_every_model_backend_is_pointed_at_the_stub_and_nothing_else_about_the_shipped_entries_changes():
    config = gateway_check.build_config(DEV, "stub-host", 4321)

    shipped = {e["model_name"]: e for e in config["model_list"] if e["model_name"] != gateway_check.COMPAT}
    assert set(shipped) == {e["model_name"] for e in DEV["model_list"]}
    for entry in DEV["model_list"]:
        got = shipped[entry["model_name"]]
        assert got["litellm_params"]["api_base"] == "http://stub-host:4321"
        assert {k: v for k, v in got["litellm_params"].items() if k != "api_base"} == {k: v for k, v in entry["litellm_params"].items() if k != "api_base"}
        assert got.get("model_info") == entry.get("model_info")  # the prices under test are the shipped ones


def test_it_adds_one_priced_openai_compatible_alias_because_production_is_that_shape():
    config = gateway_check.build_config(DEV, "stub-host", 4321)

    (compat,) = [e for e in config["model_list"] if e["model_name"] == gateway_check.COMPAT]
    assert compat["litellm_params"]["model"].startswith("openai/")
    assert compat["litellm_params"]["api_base"] == "http://stub-host:4321/v1"
    assert compat["model_info"]["input_cost_per_token"] > 0


def test_the_callbacks_that_need_other_servers_are_dropped_but_the_rest_of_the_settings_are_kept():
    config = gateway_check.build_config(DEV, "stub-host", 4321)

    assert "success_callback" not in config["litellm_settings"] and "failure_callback" not in config["litellm_settings"]
    assert config["litellm_settings"]["max_end_user_budget_id"] == DEV["litellm_settings"]["max_end_user_budget_id"]
    assert config["general_settings"] == DEV["general_settings"]


def test_it_does_not_modify_the_config_it_was_given():
    before = yaml.safe_dump(DEV)

    gateway_check.build_config(DEV, "stub-host", 4321)

    assert yaml.safe_dump(DEV) == before


def test_a_value_in_the_request_body_or_the_headers_counts_as_leaked():
    in_body = {"body": {"model": "x", "user": "tenant:acme/user:alice"}, "headers": {}}
    in_header = {"body": {"model": "x"}, "headers": {"x-end-user": "alice@example.com"}}

    assert gateway_check.leaks(in_body, ["tenant:acme/user:alice", "bob"]) == ["tenant:acme/user:alice"]
    assert gateway_check.leaks(in_header, ["alice@example.com"]) == ["alice@example.com"]


def test_a_clean_request_leaks_nothing():
    clean = {"body": {"model": "qwen", "messages": [{"role": "user", "content": "hi"}], "stream": False}, "headers": {"content-type": "application/json"}}

    assert gateway_check.leaks(clean, ["tenant:acme/user:alice", "alice@example.com", "thread-1", "metadata", "litellm_session_id"]) == []

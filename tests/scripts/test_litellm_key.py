"""scripts/litellm_key.py — the operator CLI that mints the app's scoped, budget-capped gateway key.

Hermetic: LiteLLM is an `httpx.MockTransport`, so these pin what is asked of the gateway (the
payload, the credential it is asked with) and what the operator is told. The real endpoint's
behaviour is checked by hand against the LiteLLM image (see the PR description), because a mock
can only prove we send what we mean to, not that LiteLLM accepts it.
"""
import json
from pathlib import Path

import httpx
import pytest
import yaml

from app.core import gateway
from scripts import litellm_key

REPO = Path(__file__).resolve().parents[2]
MASTER = "sk-master-SECRET"
APP_KEY = "sk-app-SECRET"


@pytest.fixture
def gateway_calls(monkeypatch):
    """Route the CLI's HTTP client into a recording mock; tests set `calls.reply`."""

    class Calls:
        requests: list[httpx.Request] = []
        reply = httpx.Response(200, json={"key": "sk-generated-NEW"})
        replies: dict[str, httpx.Response] = {}  # by URL path; a path not listed gets `reply`

    calls = Calls()
    calls.requests = []
    calls.replies = {}
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        calls.requests.append(request)
        return calls.replies.get(request.url.path, calls.reply)

    monkeypatch.setattr(
        litellm_key.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    monkeypatch.setenv("LITELLM_MASTER_KEY", MASTER)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test")
    monkeypatch.delenv("LITELLM_APP_KEY", raising=False)
    return calls


class TestCreate:
    def test_it_asks_for_a_capped_scoped_key_and_prints_it_once(self, gateway_calls, capsys):
        code = litellm_key.main(["create", "--max-budget", "600", "--rpm-limit", "300", "--tpm-limit", "400000"])

        out = capsys.readouterr().out
        (request,) = gateway_calls.requests
        payload = json.loads(request.content)
        assert code == 0
        assert request.method == "POST" and request.url.path == "/key/generate"
        assert payload["max_budget"] == 600
        assert payload["budget_duration"] == "30d"
        assert payload["models"] == ["chat", "chat-medium", "chat-strong", "embed"]
        assert payload["rpm_limit"] == 300 and payload["tpm_limit"] == 400000
        assert out.count("sk-generated-NEW") == 1

    def test_it_authenticates_with_the_master_key_from_the_environment(self, gateway_calls):
        litellm_key.main(["create", "--max-budget", "600"])

        assert gateway_calls.requests[0].headers["authorization"] == f"Bearer {MASTER}"

    def test_the_master_key_is_never_echoed(self, gateway_calls, capsys):
        litellm_key.main(["create", "--max-budget", "600"])

        out = capsys.readouterr()
        assert MASTER not in out.out and MASTER not in out.err

    def test_a_limit_that_was_not_asked_for_is_not_sent(self, gateway_calls):
        """An omitted --rpm-limit must mean "no limit", not a limit of 0 that refuses everything."""
        litellm_key.main(["create", "--max-budget", "600"])

        payload = json.loads(gateway_calls.requests[0].content)
        assert not {"rpm_limit", "tpm_limit", "max_parallel_requests"} & payload.keys()

    def test_the_default_models_are_every_alias_the_prod_config_defines(self):
        """A key lists the aliases it may call, and one missing from the list is refused for the app. The tiers an operator attaches later
        (`chat-medium`, `chat-strong`) must not need a re-minted key, so the default follows the config that defines them."""
        config = yaml.safe_load((REPO / "deploy/litellm/litellm-config.prod.yaml").read_text())

        assert set(litellm_key.DEFAULT_MODELS.split(",")) == {m["model_name"] for m in config["model_list"]}

    def test_models_are_trimmed_and_split(self, gateway_calls):
        litellm_key.main(["create", "--max-budget", "5", "--models", "chat, embed ,rerank"])

        assert json.loads(gateway_calls.requests[0].content)["models"] == ["chat", "embed", "rerank"]

    @pytest.mark.parametrize("budget", ["0", "-5"])
    def test_a_budget_that_is_not_positive_is_refused_before_any_request(self, gateway_calls, budget):
        """A key with no effective budget is exactly what this exists to replace."""
        with pytest.raises(SystemExit, match="above 0"):
            litellm_key.main(["create", "--max-budget", budget])

        assert gateway_calls.requests == []

    def test_there_is_no_default_budget(self, gateway_calls):
        """It is a business number; guessing one would be either an outage or no protection."""
        with pytest.raises(SystemExit):
            litellm_key.main(["create"])

    def test_without_the_master_key_nothing_is_attempted(self, gateway_calls, monkeypatch):
        monkeypatch.delenv("LITELLM_MASTER_KEY")

        with pytest.raises(SystemExit, match="LITELLM_MASTER_KEY"):
            litellm_key.main(["create", "--max-budget", "600"])

        assert gateway_calls.requests == []

    def test_a_refusal_is_reported_as_failure_and_prints_no_key(self, gateway_calls, capsys):
        gateway_calls.reply = httpx.Response(401, text="Authentication Error")

        code = litellm_key.main(["create", "--max-budget", "600"])

        out = capsys.readouterr()
        assert code == 1
        assert "401" in out.err
        assert "sk-" not in out.out

    def test_a_200_with_no_key_is_a_failure_not_a_blank_success(self, gateway_calls, capsys):
        gateway_calls.reply = httpx.Response(200, json={})

        assert litellm_key.main(["create", "--max-budget", "600"]) == 1


class TestInfo:
    def test_it_reports_spend_budget_and_reset(self, gateway_calls, monkeypatch, capsys):
        monkeypatch.setenv("LITELLM_APP_KEY", APP_KEY)
        gateway_calls.reply = httpx.Response(
            200,
            json={
                "info": {
                    "key_alias": "agent-core-app",
                    "spend": 12.5,
                    "max_budget": 600.0,
                    "budget_duration": "30d",
                    "budget_reset_at": "2026-11-01T00:00:00Z",
                    "models": ["chat", "embed"],
                    "rpm_limit": 300,
                    "tpm_limit": 400000,
                }
            },
        )

        code = litellm_key.main(["info"])

        out = capsys.readouterr().out
        (request,) = gateway_calls.requests
        assert code == 0
        assert request.url.path == "/key/info" and request.url.params["key"] == APP_KEY
        assert "$12.5000" in out and "$600 per 30d" in out and "2026-11-01" in out

    def test_an_uncapped_key_is_called_out(self, gateway_calls, monkeypatch, capsys):
        """The one state the whole PR exists to rule out should not look like any other."""
        monkeypatch.setenv("LITELLM_APP_KEY", APP_KEY)
        gateway_calls.reply = httpx.Response(200, json={"info": {"key_alias": "x", "spend": 0.0, "max_budget": None}})

        litellm_key.main(["info"])

        assert "uncapped" in capsys.readouterr().out

    def test_the_app_key_is_required(self, gateway_calls):
        with pytest.raises(SystemExit, match="LITELLM_APP_KEY"):
            litellm_key.main(["info"])

    def test_a_refusal_is_a_failure(self, gateway_calls, monkeypatch, capsys):
        monkeypatch.setenv("LITELLM_APP_KEY", APP_KEY)
        gateway_calls.reply = httpx.Response(404, text="not found")

        assert litellm_key.main(["info"]) == 1
        assert "404" in capsys.readouterr().err


class TestPersonBudget:
    """The default per-person cap (pattern 74). Only what is ASKED of LiteLLM is pinned here. The three endpoints' real shapes were exercised by
    hand against a live 1.94.1 proxy when this was written (missing -> exit 1, create, update, read back, delete); that LiteLLM then ENFORCES
    the budget was read from its source (`get_default_end_user_budget`, `_check_end_user_budget`) and is not something a mock can show."""

    EXISTING = {"budget_id": litellm_key.PERSON_BUDGET_ID, "max_budget": 100.0, "budget_duration": "30d", "budget_reset_at": "2026-11-20T00:00:00Z"}

    def test_a_missing_budget_is_created_under_the_id_the_configs_name(self, gateway_calls, capsys):
        gateway_calls.replies["/budget/info"] = httpx.Response(200, json=[])

        code = litellm_key.main(["person-budget", "set", "--max-budget", "100", "--budget-duration", "7d"])

        look_up, save = gateway_calls.requests
        assert code == 0
        assert look_up.url.path == "/budget/info" and json.loads(look_up.content) == {"budgets": ["person-default"]}
        assert save.url.path == "/budget/new"
        assert json.loads(save.content) == {"budget_id": "person-default", "max_budget": 100.0, "budget_duration": "7d"}
        assert "Created" in capsys.readouterr().out

    def test_an_existing_budget_is_updated_not_created_again(self, gateway_calls, capsys):
        """/budget/new is a plain insert: run twice it would fail on the duplicate id, so the second run would look like an outage."""
        gateway_calls.replies["/budget/info"] = httpx.Response(200, json=[self.EXISTING])

        code = litellm_key.main(["person-budget", "set", "--max-budget", "250"])

        assert code == 0
        assert [r.url.path for r in gateway_calls.requests] == ["/budget/info", "/budget/update"]
        assert json.loads(gateway_calls.requests[1].content)["max_budget"] == 250.0
        assert "Updated" in capsys.readouterr().out

    def test_the_duration_defaults_to_thirty_days_like_the_key(self, gateway_calls):
        gateway_calls.replies["/budget/info"] = httpx.Response(200, json=[])

        litellm_key.main(["person-budget", "set", "--max-budget", "100"])

        assert json.loads(gateway_calls.requests[1].content)["budget_duration"] == "30d"

    @pytest.mark.parametrize("budget", ["0", "-5"])
    def test_a_budget_that_is_not_positive_is_refused_before_any_request(self, gateway_calls, budget):
        with pytest.raises(SystemExit, match="above 0"):
            litellm_key.main(["person-budget", "set", "--max-budget", budget])

        assert gateway_calls.requests == []

    def test_there_is_no_default_amount(self, gateway_calls):
        """A business number, like the key's: a guess is either an outage or no protection."""
        with pytest.raises(SystemExit):
            litellm_key.main(["person-budget", "set"])

    def test_it_authenticates_with_the_master_key_and_never_echoes_it(self, gateway_calls, capsys):
        gateway_calls.replies["/budget/info"] = httpx.Response(200, json=[])

        litellm_key.main(["person-budget", "set", "--max-budget", "100"])

        out = capsys.readouterr()
        assert all(r.headers["authorization"] == f"Bearer {MASTER}" for r in gateway_calls.requests)
        assert MASTER not in out.out and MASTER not in out.err

    def test_a_refused_lookup_stops_before_anything_is_saved(self, gateway_calls, capsys):
        """Saving blind could create a second budget row or overwrite one the operator meant to keep."""
        gateway_calls.replies["/budget/info"] = httpx.Response(500, text="No db connected")

        code = litellm_key.main(["person-budget", "set", "--max-budget", "100"])

        assert code == 1
        assert [r.url.path for r in gateway_calls.requests] == ["/budget/info"]
        assert "500" in capsys.readouterr().err

    def test_a_refused_save_is_a_failure(self, gateway_calls, capsys):
        gateway_calls.replies["/budget/info"] = httpx.Response(200, json=[])
        gateway_calls.replies["/budget/new"] = httpx.Response(400, text="bad budget")

        assert litellm_key.main(["person-budget", "set", "--max-budget", "100"]) == 1
        assert "400" in capsys.readouterr().err

    def test_info_reads_the_cap_and_the_reset(self, gateway_calls, capsys):
        gateway_calls.replies["/budget/info"] = httpx.Response(200, json=[self.EXISTING])

        code = litellm_key.main(["person-budget", "info"])

        out = capsys.readouterr().out
        assert code == 0
        assert "$100 per person per 30d" in out and "2026-11-20" in out

    def test_info_on_a_missing_budget_fails_and_says_nothing_is_capped(self, gateway_calls, capsys):
        """The silent state: LiteLLM logs a warning and applies no cap. It must read as a failure to an operator and to a script."""
        gateway_calls.replies["/budget/info"] = httpx.Response(200, json=[])

        code = litellm_key.main(["person-budget", "info"])

        err = capsys.readouterr().err
        assert code == 1
        assert "NOT FOUND" in err and "applies none" in err and "person-budget set" in err

    def test_info_on_a_refusal_is_a_failure(self, gateway_calls, capsys):
        gateway_calls.replies["/budget/info"] = httpx.Response(401, text="Authentication Error")

        assert litellm_key.main(["person-budget", "info"]) == 1
        assert "401" in capsys.readouterr().err


class TestEndUser:
    def test_it_prints_the_id_the_gateway_shows_for_a_person(self, capsys):
        """Needs no gateway and no credentials: it is the same function the app calls, so what an operator types into LiteLLM's Customer
        Usage filter or a `/customer/new` is exactly what the app sends, encoding included."""
        code = litellm_key.main(["end-user", "--tenant", "acme", "--principal", "telegram:123"])

        assert code == 0
        assert capsys.readouterr().out.strip() == gateway.end_user_id("acme", "telegram:123") == "tenant:acme/user:telegram%3A123"

    def test_without_a_principal_it_prints_the_older_tenant_level_id_that_old_spend_logs_carry(self, capsys):
        code = litellm_key.main(["end-user", "--tenant", "acme"])

        assert code == 0
        assert capsys.readouterr().out.strip() == gateway.legacy_end_user_id("acme")

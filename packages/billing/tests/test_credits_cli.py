"""The operator CLI for the wallet (scripts/credits.py): what it refuses before it touches anything, what it passes to the wallet, and
how it reports each result. The wallet's own behaviour is tests/integration/test_credits_real_postgres.py and
test_credit_adjust_and_cli_real_postgres.py; here the wallet is replaced so only the CLI's own decisions are under test."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from app.billing import credits
from app.billing.credits import Applied
from scripts import credits as cli


def parse(*argv: str):
    return cli.build_parser().parse_args(argv)


class TestWhatItRefusesToParse:
    @pytest.mark.parametrize("command", ["grant --amount 5", "adjust --amount=-5"])
    @pytest.mark.parametrize("missing", ["--by alice", "--reason why"])
    def test_a_change_without_who_or_why_is_refused(self, command, missing, capsys):
        full = f"{command} --tenant acme --by alice --reason why".split()
        flag, value = missing.split()
        i = full.index(flag)
        del full[i : i + 2]

        with pytest.raises(SystemExit) as raised:
            parse(*full)

        assert raised.value.code == 2 and flag in capsys.readouterr().err

    def test_show_changes_nothing_so_it_asks_for_neither(self):
        args = parse("show", "--tenant", "acme")

        assert args.command == "show" and not hasattr(args, "by")

    @pytest.mark.parametrize("amount", ["abc", "NaN", "Infinity", "", "1e999999"])
    def test_an_amount_that_is_not_a_finite_number_is_refused(self, amount, capsys):
        with pytest.raises(SystemExit):
            parse("grant", "--tenant", "acme", "--amount", amount, "--by", "a", "--reason", "r")

    def test_adjustment_is_not_a_grant_source_it_is_what_the_adjust_command_records(self):
        assert "adjustment" not in cli.GRANT_CHOICES and "manual" in cli.GRANT_CHOICES

        with pytest.raises(SystemExit):
            parse("grant", "--tenant", "acme", "--amount", "1", "--source", "adjustment", "--by", "a", "--reason", "r")

    @pytest.mark.parametrize("by", ["", "   ", "x" * 81, "line\nbreak", "bell\x07"])
    def test_a_name_that_is_empty_long_or_not_printable_is_refused(self, by):
        with pytest.raises(SystemExit):
            parse("grant", "--tenant", "acme", "--amount", "1", "--by", by, "--reason", "r")

    def test_a_negative_amount_parses_in_the_equals_form(self):
        assert parse("adjust", "--tenant", "acme", "--amount=-12.5", "--by", "a", "--reason", "r").amount == D("-12.500000")

    def test_an_amount_is_an_exact_decimal_rounded_half_up_to_six_places(self):
        assert parse("grant", "--tenant", "t", "--amount", "0.0000005", "--by", "a", "--reason", "r").amount == D("0.000001")


@pytest.mark.parametrize(("asked", "read"), [(20, 20), (1, 1), (0, 1), (-5, 1), (200, 200), (201, 200), (10**9, 200)])
def test_show_never_reads_more_ledger_entries_than_its_ceiling(asked, read):
    assert cli.entries_limit(asked) == read


class RecordingWallet:
    def __init__(self, result: Applied | Exception):
        self.result, self.calls = result, []

    async def _do(self, verb, tenant, amount, **kwargs):
        self.calls.append((verb, tenant, amount, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def wallet(monkeypatch):
    def install(result):
        recorder = RecordingWallet(result)

        async def grant(tenant, amount, **kwargs):
            return await recorder._do("grant", tenant, amount, **kwargs)

        async def adjust(tenant, amount, **kwargs):
            return await recorder._do("adjust", tenant, amount, **kwargs)

        monkeypatch.setattr(credits, "grant", grant)
        monkeypatch.setattr(credits, "adjust", adjust)
        return recorder

    return install


class TestWhatItPassesToTheWallet:
    async def test_a_grant_carries_the_actor_the_reason_and_a_generated_key_it_prints(self, wallet):
        recorder = wallet(Applied("applied", "tx-1", D("500")))

        code, text = await cli.execute(parse("grant", "--tenant", "acme", "--amount", "500", "--by", "alice", "--reason", "pilot top-up"))

        ((verb, tenant, amount, kwargs),) = recorder.calls
        assert (verb, tenant, amount) == ("grant", "acme", D("500.000000"))
        assert kwargs["actor"] == "operator:alice" and kwargs["reason"] == "pilot top-up" and kwargs["source"] == "manual"
        assert kwargs["idempotency_key"].startswith("cli:") and kwargs["idempotency_key"] in text
        assert code == 0 and "500" in text and "tx-1" in text

    async def test_the_same_key_is_passed_through_so_a_retry_is_a_replay(self, wallet):
        recorder = wallet(Applied("duplicate", "tx-1"))

        code, text = await cli.execute(
            parse("grant", "--tenant", "acme", "--amount", "5", "--key", "ticket-4412", "--by", "alice", "--reason", "r")
        )

        assert recorder.calls[0][3]["idempotency_key"] == "ticket-4412"
        assert code == 0 and "Already applied under key ticket-4412" in text and "nothing changed" in text

    async def test_two_runs_without_a_key_are_two_changes(self, wallet):
        recorder = wallet(Applied("applied", "tx", D("1")))
        args = ("grant", "--tenant", "acme", "--amount", "1", "--by", "alice", "--reason", "r")

        await cli.execute(parse(*args))
        await cli.execute(parse(*args))

        assert recorder.calls[0][3]["idempotency_key"] != recorder.calls[1][3]["idempotency_key"]

    async def test_a_promo_without_an_expiry_is_refused_by_the_wallet_and_reported_not_raised(self, wallet):
        wallet(ValueError("promo credits must expire: pass expires_at (see EXPIRY_REQUIRED)"))

        code, text = await cli.execute(
            parse("grant", "--tenant", "acme", "--amount", "5", "--source", "promo", "--by", "a", "--reason", "r")
        )

        assert code == 2 and text.startswith("error: promo credits must expire")

    async def test_expires_in_days_becomes_an_aware_time_that_many_days_ahead(self, wallet):
        recorder = wallet(Applied("applied", "tx", D("5")))

        await cli.execute(
            parse("grant", "--tenant", "acme", "--amount", "5", "--source", "promo", "--expires-in-days", "30", "--by", "a", "--reason", "r")
        )

        expires = recorder.calls[0][3]["expires_at"]
        assert expires.tzinfo is not None
        assert abs(expires - (datetime.now(UTC) + timedelta(days=30))) < timedelta(seconds=5)

    @pytest.mark.parametrize("days", ["0", "-3"])
    async def test_an_expiry_that_is_not_in_the_future_is_refused_before_the_wallet(self, wallet, days):
        recorder = wallet(Applied("applied", "tx", D("5")))

        code, text = await cli.execute(
            parse("grant", "--tenant", "acme", "--amount", "5", "--expires-in-days", days, "--by", "a", "--reason", "r")
        )

        assert code == 2 and "at least 1" in text and recorder.calls == []

    async def test_an_adjustment_goes_to_adjust_signed(self, wallet):
        recorder = wallet(Applied("applied", "tx", D("12.5"), shortfall=D("2.5")))

        code, text = await cli.execute(
            parse("adjust", "--tenant", "acme", "--amount=-12.5", "--by", "alice", "--reason", "billed twice")
        )

        assert recorder.calls[0][:3] == ("adjust", "acme", D("-12.500000"))
        assert code == 0 and "2.5 of it is now debt" in text

    async def test_taking_credits_from_a_tenant_with_no_wallet_changes_nothing_and_says_so(self, wallet):
        wallet(Applied("no_account"))

        code, text = await cli.execute(parse("adjust", "--tenant", "ghost", "--amount=-1", "--by", "a", "--reason", "r"))

        assert code == 1 and "no credit wallet" in text and "Nothing changed" in text

    async def test_an_adjustment_of_zero_is_refused_and_reported(self, wallet):
        wallet(ValueError("an adjustment of zero changes nothing"))

        code, text = await cli.execute(parse("adjust", "--tenant", "acme", "--amount", "0", "--by", "a", "--reason", "r"))

        assert code == 2 and "zero changes nothing" in text


class TestTheWalletsOwnRulesForAdjustments:
    """Validation that happens before any SQL, so it needs no database (the behaviour with one is in the integration tier)."""

    async def test_a_zero_adjustment_is_refused(self):
        with pytest.raises(ValueError, match="zero changes nothing"):
            await credits.adjust_in(None, "acme", 0, idempotency_key="k", actor="a", reason="r")  # type: ignore[arg-type]  # refused before the connection is used

    @pytest.mark.parametrize("reason", ["", "   ", None])
    async def test_an_adjustment_needs_a_reason(self, reason):
        with pytest.raises(ValueError, match="needs a reason"):
            await credits.adjust_in(None, "acme", 5, idempotency_key="k", actor="a", reason=reason)  # type: ignore[arg-type]  # refused before the connection is used

    async def test_an_expiry_on_a_deduction_is_refused(self):
        with pytest.raises(ValueError, match="only applies to credits added"):
            await credits.adjust_in(
                None, "acme", -5, idempotency_key="k", actor="a", reason="r", expires_at=datetime.now(UTC) + timedelta(days=1)  # type: ignore[arg-type]  # refused before the connection is used
            )


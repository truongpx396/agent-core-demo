"""What an idempotency key stands for, and how a reuse is judged (specs/012-billing-migrate-idempotency, contracts/idempotency-request.md).

The statements that store and compare the request, and what the database does with a real replay, are proven against a real Postgres in
`tests/integration/test_idempotency_real_postgres.py`. These tests pin the rules that need no database: the vocabulary per operation, the
normalisation (so equal requests are equal strings), which fields are compared, the refusal of an unknown field name BEFORE anything is
written, and the shape of the error.
"""
import logging
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from psycopg.types.json import Jsonb

from credit_billing import credits

D = Decimal


# --- the vocabulary is the contract -----------------------------------------------------------------------------------------------

def test_the_vocabulary_per_operation_is_exactly_the_contracts():
    assert credits.GRANT_FIELDS == ("amount", "source", "provider", "external_ref", "expires_at", "reason", "actor")
    assert credits.DEBIT_FIELDS == ("amount", "usage_event_id", "cost_usd", "credits_per_usd", "markup", "reason", "actor")
    assert credits.EXPIRE_FIELDS == ("lot_id",)


# --- normalisation: equal requests are equal strings ---------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (D("0.0100"), "0.01"), (D("0.01"), "0.01"), (D("100"), "100"), (D("1E+2"), "100"), (D("0.000"), "0"), (D("1.50"), "1.5"),
        (0.5, "0.5"), (3, "3"), (None, None),
    ],
)
def test_a_number_is_written_fixed_point_without_exponent_or_trailing_zeros(value, expected):
    assert credits._decimal(value) == expected


def test_an_instant_is_written_in_utc_with_microseconds_whatever_zone_it_came_in():
    plus_two = timezone(timedelta(hours=2))

    assert credits._instant(datetime(2026, 11, 13, 9, 20, 11, tzinfo=plus_two)) == "2026-11-13T07:20:11.000000+00:00"
    assert credits._instant(datetime(2026, 11, 13, 7, 20, 11, 5, tzinfo=UTC)) == "2026-11-13T07:20:11.000005+00:00"
    assert credits._instant(None) is None


def test_the_same_instant_in_two_zones_is_the_same_string():
    assert credits._instant(datetime(2026, 11, 13, 9, 0, tzinfo=timezone(timedelta(hours=2)))) == credits._instant(datetime(2026, 11, 13, 7, 0, tzinfo=UTC))


# --- the request dict per operation ---------------------------------------------------------------------------------------------------

def test_a_grant_request_has_exactly_the_grant_keys_and_the_normalised_values():
    expires = datetime(2026, 11, 13, 7, 20, 11, tzinfo=UTC)

    request = credits._grant_request(credits.credits_amount("500"), "promo", "stripe", "pi_1", expires, "pilot", "operator:alice")

    assert set(request) == {"kind", *credits.GRANT_FIELDS}
    assert request == {
        "kind": "grant", "amount": "500.000000", "source": "promo", "provider": "stripe", "external_ref": "pi_1",
        "expires_at": "2026-11-13T07:20:11.000000+00:00", "reason": "pilot", "actor": "operator:alice",
    }


def test_a_debit_request_has_exactly_the_debit_keys_and_carries_the_pricing_as_the_caller_passed_it():
    pricing = credits.Pricing(cost_usd=D("0.0000040000"), credits_per_usd=D("1000.000"), markup=D("1.50"))

    request = credits._debit_request("debit", credits.credits_amount("0.004"), "evt-1", pricing, "model call (chat)", "system")

    assert set(request) == {"kind", *credits.DEBIT_FIELDS}
    assert request == {
        "kind": "debit", "amount": "0.004000", "usage_event_id": "evt-1", "cost_usd": "0.000004", "credits_per_usd": "1000", "markup": "1.5",
        "reason": "model call (chat)", "actor": "system",
    }


def test_a_debit_with_no_pricing_records_nulls_for_it():
    request = credits._debit_request("clawback", credits.credits_amount("10"), None, None, "payment_refunded", "webhook:stripe")

    assert (request["cost_usd"], request["credits_per_usd"], request["markup"], request["usage_event_id"]) == (None, None, None, None)
    assert request["kind"] == "clawback"


# --- which fields are compared -----------------------------------------------------------------------------------------------------------

def test_the_default_compares_every_field_of_the_operation_and_the_kind():
    assert credits._compared(None, credits.GRANT_FIELDS) == ["kind", *credits.GRANT_FIELDS]


def test_naming_fields_compares_those_in_vocabulary_order_plus_the_kind():
    assert credits._compared({"reason", "amount"}, credits.GRANT_FIELDS) == ["kind", "amount", "reason"]


def test_an_empty_set_compares_only_the_kind():
    assert credits._compared(frozenset(), credits.DEBIT_FIELDS) == ["kind"]


def test_naming_the_kind_is_allowed_and_changes_nothing():
    assert credits._compared({"kind", "amount"}, credits.GRANT_FIELDS) == ["kind", "amount"]


def test_the_webhook_grants_set_is_everything_but_the_expiry():
    fields = credits._compared(frozenset(credits.GRANT_FIELDS) - {"expires_at"}, credits.GRANT_FIELDS)

    assert "expires_at" not in fields and set(fields) == {"kind", *credits.GRANT_FIELDS} - {"expires_at"}


def test_an_unknown_field_is_refused_with_the_allowed_ones_listed():
    with pytest.raises(ValueError, match=r"cannot compare \['expiry'\].*amount.*source"):
        credits._compared({"amount", "expiry"}, credits.GRANT_FIELDS)


def test_a_debit_field_is_not_a_grant_field():
    with pytest.raises(ValueError, match="markup"):
        credits._compared({"markup"}, credits.GRANT_FIELDS)


# --- judging a reuse ---------------------------------------------------------------------------------------------------------------

STORED = {"kind": "grant", "amount": "500.000000", "source": "promo", "reason": "pilot", "actor": "operator:alice"}


def test_identical_requests_differ_in_nothing():
    assert credits._differences(STORED, dict(STORED), ["kind", "amount", "source", "reason", "actor"]) == {}


def test_a_differing_field_is_named_with_the_first_and_the_new_value():
    new = STORED | {"amount": "600.000000"}

    assert credits._differences(STORED, new, ["kind", "amount", "reason"]) == {"amount": ("500.000000", "600.000000")}


def test_a_field_outside_the_compared_set_is_ignored_even_if_it_differs():
    new = STORED | {"reason": "something else", "amount": "600.000000"}

    assert credits._differences(STORED, new, ["kind", "amount"]) == {"amount": ("500.000000", "600.000000")}


def test_the_kind_is_compared_even_when_nothing_else_is():
    assert credits._differences(STORED, STORED | {"kind": "debit"}, ["kind"]) == {"kind": ("grant", "debit")}


def test_a_field_the_stored_request_does_not_contain_is_skipped_so_a_newer_field_never_conflicts_with_an_older_row():
    assert credits._differences({"kind": "grant", "amount": "5.000000"}, STORED, ["kind", "amount", "source", "provider"]) == {
        "amount": ("5.000000", "500.000000")
    }


def test_a_value_of_none_differs_from_a_value():
    assert credits._differences(STORED | {"reason": None}, STORED, ["kind", "reason"]) == {"reason": (None, "pilot")}


# --- the error ---------------------------------------------------------------------------------------------------------------------------

def test_the_conflict_is_a_value_error_so_callers_that_turn_bad_input_into_an_error_keep_working():
    assert issubclass(credits.IdempotencyConflict, ValueError)


def test_the_conflict_message_names_each_field_and_says_nothing_changed_and_what_to_do():
    conflict = credits.IdempotencyConflict("K1", "acme", {"amount": ("500.000000", "600.000000"), "reason": ("a", None)})

    assert str(conflict) == (
        "idempotency key 'K1' was already used for a different request (amount: '500.000000' -> '600.000000'; reason: 'a' -> None); "
        "nothing changed. A retry must send exactly the same request; a different request needs a new key."
    )
    assert (conflict.key, conflict.tenant) == ("K1", "acme")
    assert conflict.differences == {"amount": ("500.000000", "600.000000"), "reason": ("a", None)}


def test_a_long_value_is_truncated_in_the_message_but_whole_in_the_attribute():
    long = "x" * 200

    conflict = credits.IdempotencyConflict("K", "t", {"reason": (long, "short")})

    assert "'" + "x" * 80 + "'…" in str(conflict) and "x" * 81 not in str(conflict), "80 characters, then an ellipsis outside the quotes"
    assert conflict.differences["reason"][0] == long


# --- _begin_transaction against a fake that models the unique key ----------------------------------------------------------------------------

class Cursor:
    def __init__(self, row=None):
        self._row = row

    async def fetchone(self):
        return self._row


class WalletConnection:
    """Only the two statements `_begin_transaction` sends: the insert that does nothing on a used key, and the read of the existing row."""

    def __init__(self, existing: dict | None = None):
        self.rows: dict[tuple[str, str], tuple[str, object]] = dict(existing or {})
        self.inserts: list[tuple] = []
        self.statements = 0

    async def execute(self, sql, params=None):
        self.statements += 1
        if sql.lstrip().startswith("INSERT INTO credit_transactions"):
            tenant, key = params[0], params[2]
            if (tenant, key) in self.rows:
                return Cursor(None)
            transaction_id = f"tx-{len(self.rows) + 1}"
            self.rows[(tenant, key)] = (transaction_id, params[-1].obj if isinstance(params[-1], Jsonb) else params[-1])
            self.inserts.append(params)
            return Cursor((transaction_id,))
        assert "SELECT id, request FROM credit_transactions WHERE tenant = %s AND idempotency_key = %s" in " ".join(sql.split())
        return Cursor(self.rows[(params[0], params[1])])


GRANT = {"kind": "grant", "amount": "500.000000", "source": "purchase", "provider": None, "external_ref": None, "expires_at": None, "reason": "r", "actor": "a"}
ALL = ["kind", *credits.GRANT_FIELDS]


async def begin(conn, request, fields=ALL, key="K1", tenant="acme"):
    return await credits._begin_transaction(conn, tenant, "grant", key, actor="a", reason="r", request=request, fields=fields)


async def test_a_first_use_inserts_the_row_with_the_request_as_json():
    conn = WalletConnection()

    new_id, existing = await begin(conn, GRANT)

    assert (new_id, existing) == ("tx-1", None)
    assert isinstance(conn.inserts[0][-1], Jsonb) and conn.inserts[0][-1].obj == GRANT


async def test_an_identical_replay_is_a_duplicate_pointing_at_the_original():
    conn = WalletConnection()
    await begin(conn, GRANT)

    assert await begin(conn, dict(GRANT)) == (None, "tx-1")
    assert len(conn.inserts) == 1


async def test_a_replay_with_a_different_request_raises_naming_the_field_and_inserts_nothing():
    conn = WalletConnection()
    await begin(conn, GRANT)

    with pytest.raises(credits.IdempotencyConflict) as raised:
        await begin(conn, GRANT | {"amount": "600.000000"})

    assert raised.value.differences == {"amount": ("500.000000", "600.000000")}
    assert len(conn.inserts) == 1, "the conflicting request wrote nothing"


async def test_a_replay_that_differs_only_outside_the_compared_fields_is_a_duplicate():
    conn = WalletConnection()
    await begin(conn, GRANT | {"expires_at": "2026-11-13T07:20:11.000000+00:00"})
    narrowed = [field for field in ALL if field != "expires_at"]

    assert await begin(conn, GRANT | {"expires_at": "2026-11-13T07:20:12.000000+00:00"}, fields=narrowed) == (None, "tx-1")


async def test_with_no_fields_but_the_kind_any_other_difference_is_a_duplicate_and_a_kind_difference_still_conflicts():
    conn = WalletConnection()
    await begin(conn, GRANT)

    assert await begin(conn, GRANT | {"amount": "1.000000", "reason": "other"}, fields=["kind"]) == (None, "tx-1")
    with pytest.raises(credits.IdempotencyConflict) as raised:
        await begin(conn, GRANT | {"kind": "debit"}, fields=["kind"])
    assert set(raised.value.differences) == {"kind"}


async def test_a_row_with_no_stored_request_is_a_plain_duplicate_whatever_is_asked():
    conn = WalletConnection({("acme", "K1"): ("tx-old", None)})

    assert await begin(conn, GRANT | {"amount": "999.000000"}) == (None, "tx-old")


async def test_the_same_key_in_another_tenant_is_a_new_transaction_not_a_conflict():
    conn = WalletConnection()
    await begin(conn, GRANT, tenant="acme")

    new_id, existing = await begin(conn, GRANT | {"amount": "1.000000"}, tenant="other")

    assert existing is None and new_id == "tx-2"


async def test_a_conflict_is_logged_with_the_tenant_the_key_and_field_names_but_no_values(caplog):
    conn = WalletConnection()
    await begin(conn, GRANT | {"reason": "secret-ticket-4412"})

    with caplog.at_level(logging.WARNING, logger=credits.logger.name), pytest.raises(credits.IdempotencyConflict):
        await begin(conn, GRANT | {"reason": "other"})

    record = next(r for r in caplog.records if r.getMessage() == "credit_idempotency_conflict")
    assert (record.tenant, record.key, record.fields) == ("acme", "K1", ["reason"])
    assert "secret-ticket-4412" not in repr(record.__dict__) and "other" not in repr(vars(record).get("fields"))


# --- the writes validate `compare` before anything runs ------------------------------------------------------------------------------------

class CountingConnection:
    def __init__(self):
        self.statements = []

    async def execute(self, sql, params=None):
        self.statements.append(sql)
        return Cursor((1,))


@pytest.mark.parametrize(
    "call",
    [
        lambda c: credits.grant_in(c, "acme", 5, source="manual", idempotency_key="k", actor="a", compare={"nope"}),
        lambda c: credits.debit_in(c, "acme", 5, idempotency_key="k", compare={"nope"}),
        lambda c: credits.clawback_in(c, "acme", 5, idempotency_key="k", actor="a", compare={"nope"}),
        lambda c: credits.adjust_in(c, "acme", 5, idempotency_key="k", actor="a", reason="why", compare={"nope"}),
        lambda c: credits.adjust_in(c, "acme", -5, idempotency_key="k", actor="a", reason="why", compare={"nope"}),
        lambda c: credits.debit_in(c, "acme", 0, idempotency_key="k", compare={"nope"}),
    ],
    ids=["grant", "debit", "clawback", "adjust+", "adjust-", "zero-debit"],
)
async def test_an_unknown_field_is_refused_before_a_single_statement_runs(call):
    conn = CountingConnection()

    with pytest.raises(ValueError, match="cannot compare"):
        await call(conn)

    assert conn.statements == []


async def test_a_grant_field_is_refused_on_a_debit_and_the_other_way_round():
    conn = CountingConnection()

    with pytest.raises(ValueError):
        await credits.debit_in(conn, "acme", 5, idempotency_key="k", compare={"source"})
    with pytest.raises(ValueError):
        await credits.grant_in(conn, "acme", 5, source="manual", idempotency_key="k", actor="a", compare={"markup"})
    assert conn.statements == []


# --- what each write hands to _begin_transaction ---------------------------------------------------------------------------------------------

class Stop(Exception):
    pass


@pytest.fixture
def seen(monkeypatch):
    captured = {}

    async def begin_transaction(conn, tenant, kind, key, **kwargs):
        captured.update(kind=kind, key=key, **kwargs)
        raise Stop

    monkeypatch.setattr(credits, "_begin_transaction", begin_transaction)
    return captured


async def test_grant_in_records_the_request_it_was_asked_for_and_compares_everything_by_default(seen):
    expires = datetime.now(UTC) + timedelta(days=3)

    with pytest.raises(Stop):
        await credits.grant_in(
            CountingConnection(), "acme", "12.5", source="promo", idempotency_key="k", actor="operator:a", reason="why", expires_at=expires,
            provider="stripe", external_ref="pi_1",
        )

    assert seen["request"] == credits._grant_request(credits.credits_amount("12.5"), "promo", "stripe", "pi_1", expires, "why", "operator:a")
    assert seen["request"]["amount"] == "12.500000", "the amount is the wallet's six-place figure, not what the caller typed"
    assert seen["fields"] == ["kind", *credits.GRANT_FIELDS]


async def test_grant_in_with_a_narrowed_set_passes_only_those_fields_to_compare(seen):
    with pytest.raises(Stop):
        await credits.grant_in(
            CountingConnection(), "acme", 5, source="purchase", idempotency_key="k", actor="a",
            compare=frozenset(credits.GRANT_FIELDS) - {"expires_at"},
        )

    assert "expires_at" not in seen["fields"] and "amount" in seen["fields"] and seen["fields"][0] == "kind"


async def test_debit_in_records_the_pricing_and_the_usage_event_and_the_kind(seen):
    pricing = credits.Pricing(D("0.01"), D("1000"), D("1"))

    with pytest.raises(Stop):
        await credits.debit_in(
            CountingConnection(), "acme", 10, idempotency_key="evt", usage_event_id="evt", reason="model call (chat)", pricing=pricing
        )

    assert seen["kind"] == "debit"
    assert seen["request"] == credits._debit_request("debit", credits.credits_amount(10), "evt", pricing, "model call (chat)", "system")


async def test_clawback_in_is_a_debit_of_kind_clawback_and_forwards_compare(seen):
    with pytest.raises(Stop):
        await credits.clawback_in(CountingConnection(), "acme", 7, idempotency_key="k", actor="webhook:stripe", reason="payment_refunded", compare=frozenset())

    assert seen["kind"] == "clawback" and seen["request"]["kind"] == "clawback" and seen["fields"] == ["kind"]


async def test_a_negative_adjustment_is_a_debit_of_kind_adjust_and_a_positive_one_a_grant_from_adjustment(seen):
    with pytest.raises(Stop):
        await credits.adjust_in(CountingConnection(), "acme", -3, idempotency_key="k", actor="a", reason="why")
    negative = dict(seen)
    with pytest.raises(Stop):
        await credits.adjust_in(CountingConnection(), "acme", 3, idempotency_key="k", actor="a", reason="why")

    assert negative["kind"] == "adjust" and negative["request"]["amount"] == "3.000000"
    assert seen["kind"] == "grant" and seen["request"]["source"] == "adjustment"


async def test_an_expiry_records_the_lot_it_expires(seen):
    class LotConnection(CountingConnection):
        async def execute(self, sql, params=None):
            await super().execute(sql, params)
            return Cursor((D("5"),))

    with pytest.raises(Stop):
        await credits._expire_lot_in(LotConnection(), "acme", "lot-1")

    assert seen["request"] == {"kind": "expire", "lot_id": "lot-1"} and seen["fields"] == ["kind", "lot_id"]

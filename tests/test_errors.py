"""Tests for app/errors.py's canonical error envelope."""
from app.errors import ErrorCode, ErrorEnvelope


class TestErrorEnvelope:
    def test_to_dict_is_json_safe(self):
        env = ErrorEnvelope(code=ErrorCode.TIMEOUT, message="took too long")
        d = env.to_dict()
        assert d == {"code": "timeout", "message": "took too long", "details": None}
        assert isinstance(d["code"], str)  # not the Enum member itself

    def test_details_round_trips(self):
        env = ErrorEnvelope(
            code=ErrorCode.COST_CEILING_EXCEEDED,
            message="over budget",
            details={"limit_usd": 1.0, "spent_usd": 1.5},
        )
        assert env.to_dict()["details"] == {"limit_usd": 1.0, "spent_usd": 1.5}

    def test_every_code_is_a_plain_string_value(self):
        """Codes are meant to be switched on by an integrating caller —
        each must be a stable, lowercase, plain string, not an
        implementation-detail repr."""
        for code in ErrorCode:
            assert code.value == code.value.lower()
            assert " " not in code.value

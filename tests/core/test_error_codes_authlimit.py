"""The error codes the front door shares with the API (specs/018, task T056): one table, two copies, held equal.

The authlimit service answers a refused request with the same envelope `{code, message, details}` the rest of this app uses (pattern 30), and
its codes are a short list in `services/authlimit/errorcodes.json`. The API answers with the same words when a request reaches it without a
valid identity. If the two drifted, a client would see one message from one layer and another from the next, or, worse, a code that this
registry does not know and so no caller can switch on. The Go side checks its handlers against the JSON; this checks the JSON against
`app/core/errors.py`. (A file both sides read, declared in the constitution's rule for non-Python services; the app's runtime code never opens it.)
"""
import json
from pathlib import Path

from app.core.errors import AUTH_ERRORS, INTERNAL_ERROR_MESSAGE, ErrorCode

SHARED = json.loads((Path(__file__).resolve().parents[2] / "services" / "authlimit" / "errorcodes.json").read_text())["codes"]


def test_every_code_the_front_door_can_send_is_a_code_this_registry_knows():
    unknown = sorted(set(SHARED) - {code.value for code in ErrorCode})

    assert not unknown, f"the front door sends codes the API's registry lacks: {unknown}"


def test_the_two_tables_agree_on_the_set_of_codes_and_on_each_status_and_message_word_for_word():
    here = {code.value: {"status": status, "message": message} for code, (status, message) in AUTH_ERRORS.items()}

    assert here == SHARED


def test_the_catch_all_says_the_same_thing_as_the_apps_own_internal_error():
    """The front door's `internal` and the app's INTERNAL_ERROR_MESSAGE are one sentence a person may see from either: pattern 30."""
    assert SHARED["internal"]["message"] == INTERNAL_ERROR_MESSAGE


def test_no_message_names_a_token_a_claim_or_an_exception():
    for code, (_status, message) in AUTH_ERRORS.items():
        lowered = message.lower()
        assert "jwt" not in lowered and "exception" not in lowered and "traceback" not in lowered, code

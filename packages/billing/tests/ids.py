"""Random ids for tests that plant a string in a payload and then search the stored form for it.

The webhook tests plant buyer details in a delivery ("4242", a card's last four digits, and the
999999999 credits it asserts) and assert none of it reached the stored payload, by searching the
stored JSON for the string. That JSON also holds the test's own random ids, and a random hex id
contains a four-digit string by chance: about one in ten thousand per id. A test failed that way in
CI once, for a reason that had nothing to do with what it checks. An id drawn here cannot contain
a planted string, so the search can only find a real leak.
"""
import uuid

# What `tests/contract.py` plants in a delivery and the tests then assert is absent. Keep in step with it.
PLANTED = ("4242", "999999999")


def hex_id(length: int) -> str:
    """`length` random hex characters that contain none of `PLANTED`."""
    while True:
        candidate = uuid.uuid4().hex[:length]
        if not any(planted in candidate for planted in PLANTED):
            return candidate

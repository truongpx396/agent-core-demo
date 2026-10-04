"""Tests for scripts/ai_review_providers.py: which providers the AI reviewer may try, in order.

The property that matters most is the key rule: one provider's API key must never be sent to another
provider's host. It is asserted directly (`test_no_fallback_ever_carries_the_primarys_key_to_another_host`)
over every combination of the settings, not just on the examples.
"""
import itertools

import pytest

from scripts import ai_review_providers as p

PRIMARY = p.Provider("primary", "https://llm.example/v1", "main-model", "KEY-PRIMARY")


def build(**env):
    return p.build_fallbacks(env, PRIMARY)


def test_with_no_fallback_variables_there_are_no_fallbacks():
    assert build() == ()


def test_a_model_alone_means_another_model_on_the_same_provider_with_the_same_key():
    (fallback,) = build(AI_REVIEW_FALLBACK1_MODEL="lite-model")
    assert fallback == p.Provider("fallback 1", "https://llm.example/v1", "lite-model", "KEY-PRIMARY")


def test_an_explicit_base_url_naming_the_same_host_still_inherits_the_key_whatever_its_trailing_slash():
    (fallback,) = build(AI_REVIEW_FALLBACK1_MODEL="m", AI_REVIEW_FALLBACK1_BASE_URL="https://llm.example/v1/")
    assert fallback.base_url == "https://llm.example/v1" and fallback.api_key == "KEY-PRIMARY"


def test_a_different_host_with_its_own_key_uses_that_key():
    (fallback,) = build(AI_REVIEW_FALLBACK1_MODEL="m", AI_REVIEW_FALLBACK1_BASE_URL="https://other.example/v1", AI_REVIEW_FALLBACK1_API_KEY="KEY-OTHER")
    assert (fallback.base_url, fallback.api_key) == ("https://other.example/v1", "KEY-OTHER")


def test_a_different_host_with_no_key_of_its_own_sends_none_never_the_primarys():
    (fallback,) = build(AI_REVIEW_FALLBACK1_MODEL="m", AI_REVIEW_FALLBACK1_BASE_URL="https://other.example/v1")
    assert fallback.api_key == ""


def test_no_fallback_ever_carries_the_primarys_key_to_another_host():
    bases = ["", "https://llm.example/v1", "https://llm.example/v1/", "https://other.example/v1", "http://localhost:8080/v1"]
    keys = ["", "KEY-OWN"]
    for base, key in itertools.product(bases, keys):
        env = {"AI_REVIEW_FALLBACK1_MODEL": "m", "AI_REVIEW_FALLBACK1_BASE_URL": base, "AI_REVIEW_FALLBACK1_API_KEY": key}
        for fallback in p.build_fallbacks(env, PRIMARY):
            if fallback.base_url != PRIMARY.base_url:
                assert fallback.api_key in ("", key), (base, key)  # only ever its own key, or none
                assert fallback.api_key != "KEY-PRIMARY"


def test_a_slot_without_a_model_is_off_even_if_it_has_a_url_and_a_key():
    assert build(AI_REVIEW_FALLBACK1_BASE_URL="https://other.example/v1", AI_REVIEW_FALLBACK1_API_KEY="k") == ()
    assert build(AI_REVIEW_FALLBACK1_MODEL="   ") == ()
    assert build(AI_REVIEW_FALLBACK1_MODEL="") == ()  # the workflow passes an unset variable as an empty string


def test_slots_keep_their_number_and_order_and_an_empty_first_slot_does_not_renumber_the_second():
    both = build(AI_REVIEW_FALLBACK1_MODEL="a", AI_REVIEW_FALLBACK2_MODEL="b")
    assert [(f.name, f.model) for f in both] == [("fallback 1", "a"), ("fallback 2", "b")]
    only_second = build(AI_REVIEW_FALLBACK2_MODEL="b")
    assert [(f.name, f.model) for f in only_second] == [("fallback 2", "b")]


def test_a_fallback_that_repeats_a_provider_already_in_the_chain_is_dropped():
    assert build(AI_REVIEW_FALLBACK1_MODEL="main-model") == ()  # same base URL and model as the primary
    chain = build(AI_REVIEW_FALLBACK1_MODEL="a", AI_REVIEW_FALLBACK2_MODEL="a")
    assert [f.name for f in chain] == ["fallback 1"]  # the second repeats the first
    assert len(build(AI_REVIEW_FALLBACK1_MODEL="a", AI_REVIEW_FALLBACK2_MODEL="a", AI_REVIEW_FALLBACK2_BASE_URL="https://other.example/v1")) == 2


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y", "other.example/v1", "javascript:alert(1)"])
def test_a_fallback_base_url_must_be_http_or_https_like_the_primarys(url):
    with pytest.raises(ValueError, match="AI_REVIEW_FALLBACK1_BASE_URL must be an http"):
        build(AI_REVIEW_FALLBACK1_MODEL="m", AI_REVIEW_FALLBACK1_BASE_URL=url)


def test_only_the_documented_number_of_slots_is_read():
    chain = build(**{f"AI_REVIEW_FALLBACK{n}_MODEL": f"m{n}" for n in range(1, 6)})
    assert [f.name for f in chain] == ["fallback 1", "fallback 2"] and p.MAX_FALLBACKS == 2

"""PricingMode — the enumerated billing mode (TOKWEIR-4, US3).

The story's acceptance: `pricing_mode` distinguishes `api_metered` vs `subscription`.
These tests pin the enumeration itself, its wire strings, normalization from either
form, and rejection of anything else.
"""

import pytest

from tokenweir import PricingMode, UsageRecord


def _record(**overrides) -> UsageRecord:
    base = dict(
        request_id="req-1",
        app_id="mado",
        endpoint="/v1/messages",
        model="claude-sonnet-4",
        status="ok",
    )
    base.update(overrides)
    return UsageRecord(**base)


def test_exactly_two_modes_with_the_contracted_wire_strings():
    assert {member.value for member in PricingMode} == {"api_metered", "subscription"}
    assert PricingMode.API_METERED.value == "api_metered"
    assert PricingMode.SUBSCRIPTION.value == "subscription"


def test_string_input_normalizes_to_the_enum_member():
    rec = _record(pricing_mode="api_metered")
    assert rec.pricing_mode is PricingMode.API_METERED

    rec = _record(pricing_mode="subscription")
    assert rec.pricing_mode is PricingMode.SUBSCRIPTION


def test_enum_input_is_kept_as_is():
    rec = _record(pricing_mode=PricingMode.SUBSCRIPTION)
    assert rec.pricing_mode is PricingMode.SUBSCRIPTION


def test_unset_pricing_mode_is_valid_and_stays_none():
    # A producer that does not know its billing mode must still be able to emit
    # raw counts.
    assert _record().pricing_mode is None


@pytest.mark.parametrize("bad", ["free_tier", "API_METERED", "", "flat", 1, object()])
def test_unrecognized_pricing_mode_is_rejected_naming_the_permitted_values(bad):
    with pytest.raises(ValueError) as excinfo:
        _record(pricing_mode=bad)

    message = str(excinfo.value)
    assert "api_metered" in message
    assert "subscription" in message


def test_pricing_mode_serializes_to_the_plain_wire_string():
    payload = _record(pricing_mode=PricingMode.API_METERED).to_dict()
    assert payload["pricing_mode"] == "api_metered"
    assert type(payload["pricing_mode"]) is str  # noqa: E721 — not an enum subclass

    assert _record().to_dict()["pricing_mode"] is None


def test_coerce_helper_round_trips_both_forms():
    assert PricingMode.coerce(None) is None
    assert PricingMode.coerce("subscription") is PricingMode.SUBSCRIPTION
    assert PricingMode.coerce(PricingMode.API_METERED) is PricingMode.API_METERED
    with pytest.raises(ValueError):
        PricingMode.coerce("nope")

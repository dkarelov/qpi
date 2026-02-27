from __future__ import annotations

import pytest

from services.bot_api.callback_data import CALLBACK_VERSION, build_callback, parse_callback


def test_callback_roundtrip() -> None:
    payload = build_callback(flow="seller", action="shop_open", entity_id="123")
    parsed = parse_callback(payload)

    assert payload == f"{CALLBACK_VERSION}:seller:shop_open:123"
    assert parsed.flow == "seller"
    assert parsed.action == "shop_open"
    assert parsed.entity_id == "123"


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "v2:seller:shop_open:1",
        "v1:seller:shop_open",
        "v1::shop_open:1",
        "v1:seller::1",
    ],
)
def test_parse_callback_rejects_invalid_payload(payload: str) -> None:
    with pytest.raises(ValueError):
        parse_callback(payload)


def test_build_callback_rejects_oversized_payload() -> None:
    with pytest.raises(ValueError):
        build_callback(flow="seller", action="shop_open", entity_id="x" * 80)

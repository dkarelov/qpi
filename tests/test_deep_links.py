from __future__ import annotations

from services.bot_api.deep_links import (
    build_listing_deep_link,
    build_listing_start_payload,
    build_shop_deep_link,
    build_shop_start_payload,
    parse_start_payload,
)


def test_shop_and_listing_deep_links_use_stable_start_payloads() -> None:
    assert build_shop_start_payload(slug="shop_tushenka") == "shop_shop_tushenka"
    assert build_listing_start_payload(listing_id=21) == "listing_21"
    assert build_shop_deep_link(bot_username="@qpilka_bot", slug="shop_tushenka") == (
        "https://t.me/qpilka_bot?start=shop_shop_tushenka"
    )
    assert build_listing_deep_link(bot_username="qpilka_bot", listing_id=21) == (
        "https://t.me/qpilka_bot?start=listing_21"
    )


def test_parse_start_payload_accepts_shop_and_listing_links() -> None:
    shop_payload = parse_start_payload("shop_shop_tushenka")
    listing_payload = parse_start_payload("listing_21")

    assert shop_payload is not None
    assert shop_payload.kind == "shop"
    assert shop_payload.value == "shop_tushenka"
    assert listing_payload is not None
    assert listing_payload.kind == "listing"
    assert listing_payload.value == 21
    assert parse_start_payload("listing_bad") is None
    assert parse_start_payload("unknown") is None

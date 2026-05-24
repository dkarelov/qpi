from __future__ import annotations

from dataclasses import dataclass

SHOP_START_PREFIX = "shop_"
LISTING_START_PREFIX = "listing_"


@dataclass(frozen=True)
class StartPayload:
    kind: str
    value: str | int


def build_shop_start_payload(*, slug: str) -> str:
    normalized_slug = slug.strip()
    if not normalized_slug:
        raise ValueError("shop slug must not be empty")
    return f"{SHOP_START_PREFIX}{normalized_slug}"


def build_listing_start_payload(*, listing_id: int) -> str:
    if listing_id < 1:
        raise ValueError("listing_id must be >= 1")
    return f"{LISTING_START_PREFIX}{listing_id}"


def build_shop_deep_link(*, bot_username: str, slug: str) -> str:
    return f"https://t.me/{_normalize_bot_username(bot_username)}?start={build_shop_start_payload(slug=slug)}"


def build_listing_deep_link(*, bot_username: str, listing_id: int) -> str:
    return f"https://t.me/{_normalize_bot_username(bot_username)}?start={build_listing_start_payload(listing_id=listing_id)}"


def parse_start_payload(raw: str) -> StartPayload | None:
    value = raw.strip()
    if value.startswith(SHOP_START_PREFIX):
        slug = value[len(SHOP_START_PREFIX) :].strip()
        if slug:
            return StartPayload(kind="shop", value=slug)
        return None
    if value.startswith(LISTING_START_PREFIX):
        listing_id_text = value[len(LISTING_START_PREFIX) :].strip()
        try:
            listing_id = int(listing_id_text)
        except ValueError:
            return None
        if listing_id >= 1:
            return StartPayload(kind="listing", value=listing_id)
    return None


def _normalize_bot_username(bot_username: str) -> str:
    normalized = bot_username.strip().lstrip("@")
    if not normalized:
        raise ValueError("bot_username must not be empty")
    return normalized

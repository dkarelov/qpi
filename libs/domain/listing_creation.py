from __future__ import annotations

import csv
import re
from decimal import Decimal


def parse_listing_create_csv(text: str) -> tuple[int, Decimal, int, str, list[str]]:
    try:
        rows = list(csv.reader([text], skipinitialspace=True))
    except csv.Error as exc:
        raise ValueError("invalid csv") from exc
    if len(rows) != 1 or len(rows[0]) < 4:
        raise ValueError("listing create input must contain at least 4 fields")

    fields = [field.strip() for field in rows[0]]
    wb_product_id = int(fields[0])
    cashback_rub = Decimal(fields[1])
    slots = int(fields[2])
    search_phrase = fields[3]
    review_phrases = [field for field in fields[4:] if field]
    if len(review_phrases) > 10:
        raise ValueError("review_phrases must contain at most 10 entries")
    return wb_product_id, cashback_rub, slots, search_phrase, review_phrases


def sanitize_buyer_display_title(
    *,
    wb_product_id: int,
    source_title: str,
    brand_name: str | None,
) -> str:
    title = source_title.strip()
    brand = (brand_name or "").strip()
    if brand:
        title = re.sub(re.escape(brand), "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s{2,}", " ", title).strip(" -|,;:/")
    if not title or _contains_brand_reference(text=title, brand_name=brand):
        return f"Товар {wb_product_id}"
    return title


def _contains_brand_reference(*, text: str, brand_name: str | None) -> bool:
    normalized_brand = _normalize_match_text(brand_name)
    normalized_text = _normalize_match_text(text)
    if not normalized_brand or not normalized_text:
        return False
    return normalized_brand in normalized_text


def _normalize_match_text(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", value).strip().lower()

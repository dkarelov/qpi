from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from libs.domain.errors import PayloadValidationError

_PURCHASE_PAYLOAD_VERSION = 4
_REVIEW_PAYLOAD_VERSION = 3
_PURCHASE_PAYLOAD_SOURCE = "plugin_base64"


@dataclass(frozen=True)
class DecodedPurchasePayload:
    payload_version: int
    task_uuid: UUID
    order_id: str
    ordered_at: datetime
    source: str
    raw_payload_json: list[Any]


@dataclass(frozen=True)
class DecodedReviewPayload:
    payload_version: int
    task_uuid: UUID
    reviewed_at: datetime
    rating: int
    review_text: str
    raw_payload_json: list[Any]
    legacy_wb_product_id: int | None = None


def decode_purchase_payload(payload_base64: str) -> DecodedPurchasePayload:
    normalized_payload = payload_base64.strip()
    if not normalized_payload:
        raise PayloadValidationError("payload must not be empty")

    parsed = _decode_base64_json_array(normalized_payload)
    if len(parsed) != 3:
        raise PayloadValidationError("payload must contain [task_uuid, order_id, ordered_at]")

    task_uuid = _require_uuid(parsed[0], field_name="task_uuid")

    order_id_raw = parsed[1]
    if not isinstance(order_id_raw, str) or not order_id_raw.strip():
        raise PayloadValidationError("payload field 'order_id' must be non-empty string")
    order_id = order_id_raw.strip()

    ordered_at_raw = parsed[2]
    if not isinstance(ordered_at_raw, str):
        raise PayloadValidationError("payload field 'ordered_at' must be ISO datetime string")
    ordered_at = _parse_iso_datetime_utc(ordered_at_raw, field_name="ordered_at")

    return DecodedPurchasePayload(
        payload_version=_PURCHASE_PAYLOAD_VERSION,
        task_uuid=task_uuid,
        order_id=order_id,
        ordered_at=ordered_at,
        source=_PURCHASE_PAYLOAD_SOURCE,
        raw_payload_json=parsed,
    )


def decode_review_payload(payload_base64: str) -> DecodedReviewPayload:
    normalized_payload = payload_base64.strip()
    if not normalized_payload:
        raise PayloadValidationError("payload must not be empty")

    parsed = _decode_base64_json_array(normalized_payload)
    legacy_wb_product_id: int | None = None
    if len(parsed) == 4:
        task_uuid_raw, reviewed_at_raw, rating_raw, review_text_raw = parsed
    elif len(parsed) == 5:
        task_uuid_raw, legacy_wb_product_id_raw, reviewed_at_raw, rating_raw, review_text_raw = parsed
        legacy_wb_product_id = _require_positive_int(legacy_wb_product_id_raw, field_name="wb_product_id")
    else:
        raise PayloadValidationError("payload must contain [task_uuid, reviewed_at, rating, review_text]")

    task_uuid = _require_uuid(task_uuid_raw, field_name="task_uuid")

    if not isinstance(reviewed_at_raw, str):
        raise PayloadValidationError("payload field 'reviewed_at' must be ISO datetime string")
    reviewed_at = _parse_iso_datetime_utc(reviewed_at_raw, field_name="reviewed_at")

    rating = _require_positive_int(rating_raw, field_name="rating")
    if rating > 5:
        raise PayloadValidationError("payload field 'rating' must be between 1 and 5")

    if not isinstance(review_text_raw, str) or not review_text_raw.strip():
        raise PayloadValidationError("payload field 'review_text' must be non-empty string")

    return DecodedReviewPayload(
        payload_version=_REVIEW_PAYLOAD_VERSION,
        task_uuid=task_uuid,
        reviewed_at=reviewed_at,
        rating=rating,
        review_text=review_text_raw.strip(),
        raw_payload_json=parsed,
        legacy_wb_product_id=legacy_wb_product_id,
    )


def _decode_base64_json_array(payload_base64: str) -> list[Any]:
    try:
        payload_bytes = base64.b64decode(payload_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PayloadValidationError("payload must be valid base64") from exc

    try:
        payload_text = payload_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PayloadValidationError("payload must be utf-8 encoded JSON") from exc

    try:
        parsed = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise PayloadValidationError("payload must be valid JSON array") from exc

    if not isinstance(parsed, list):
        raise PayloadValidationError("payload must be a JSON array")
    return parsed


def _parse_iso_datetime_utc(value: str, *, field_name: str) -> datetime:
    normalized = value.strip()
    if not normalized:
        raise PayloadValidationError(f"payload field '{field_name}' must not be empty")

    # Accept JS toISOString() payloads and normalize all timestamps to UTC.
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PayloadValidationError(f"payload field '{field_name}' is not valid ISO datetime") from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _require_positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise PayloadValidationError(f"payload field '{field_name}' must be positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise PayloadValidationError(f"payload field '{field_name}' must be positive integer") from exc
    if normalized < 1:
        raise PayloadValidationError(f"payload field '{field_name}' must be positive integer")
    return normalized


def _require_uuid(value: Any, *, field_name: str) -> UUID:
    if not isinstance(value, str) or not value.strip():
        raise PayloadValidationError(f"payload field '{field_name}' must be UUID string")
    try:
        return UUID(value.strip())
    except ValueError as exc:
        raise PayloadValidationError(f"payload field '{field_name}' must be UUID string") from exc

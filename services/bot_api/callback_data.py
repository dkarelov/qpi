from __future__ import annotations

from dataclasses import dataclass

CALLBACK_VERSION = "v1"
CALLBACK_MAX_BYTES = 64


@dataclass(frozen=True)
class CallbackPayload:
    flow: str
    action: str
    entity_id: str


def build_callback(*, flow: str, action: str, entity_id: str = "") -> str:
    normalized_flow = _normalize_part(flow, "flow")
    normalized_action = _normalize_part(action, "action")
    normalized_entity_id = _normalize_entity_id(entity_id)
    payload = f"{CALLBACK_VERSION}:{normalized_flow}:{normalized_action}:{normalized_entity_id}"
    if len(payload.encode("utf-8")) > CALLBACK_MAX_BYTES:
        raise ValueError("callback payload exceeds Telegram 64-byte limit")
    return payload


def parse_callback(raw: str) -> CallbackPayload:
    parts = raw.split(":")
    if len(parts) != 4:
        raise ValueError("invalid callback payload format")
    version, flow, action, entity_id = parts
    if version != CALLBACK_VERSION:
        raise ValueError("unsupported callback version")
    return CallbackPayload(
        flow=_normalize_part(flow, "flow"),
        action=_normalize_part(action, "action"),
        entity_id=_normalize_entity_id(entity_id),
    )


def _normalize_part(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"callback {field_name} must not be empty")
    if ":" in normalized:
        raise ValueError(f"callback {field_name} must not contain ':'")
    return normalized


def _normalize_entity_id(value: str) -> str:
    normalized = value.strip()
    if ":" in normalized:
        raise ValueError("callback entity_id must not contain ':'")
    return normalized

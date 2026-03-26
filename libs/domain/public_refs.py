from __future__ import annotations

import re
from collections.abc import Sequence

SUPPORT_START_PAYLOAD_MAX_BYTES = 64
_SUPPORT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]+$")


def format_shop_ref(shop_id: int) -> str:
    return _format_prefixed_ref("S", shop_id)


def format_listing_ref(listing_id: int) -> str:
    return _format_prefixed_ref("L", listing_id)


def format_assignment_ref(assignment_id: int) -> str:
    return _format_prefixed_ref("P", assignment_id)


def format_withdrawal_ref(withdrawal_request_id: int) -> str:
    return _format_prefixed_ref("W", withdrawal_request_id)


def format_deposit_ref(deposit_intent_id: int) -> str:
    return _format_prefixed_ref("D", deposit_intent_id)


def format_chain_tx_ref(chain_tx_id: int) -> str:
    return _format_prefixed_ref("TX", chain_tx_id)


def parse_shop_ref(value: str) -> int:
    return _parse_prefixed_ref(value, prefix="S")


def parse_listing_ref(value: str) -> int:
    return _parse_prefixed_ref(value, prefix="L")


def parse_assignment_ref(value: str) -> int:
    return _parse_prefixed_ref(value, prefix="P")


def parse_withdrawal_ref(value: str) -> int:
    return _parse_prefixed_ref(value, prefix="W")


def parse_deposit_ref(value: str) -> int:
    return _parse_prefixed_ref(value, prefix="D")


def parse_chain_tx_ref(value: str) -> int:
    return _parse_prefixed_ref(value, prefix="TX")


def build_support_start_payload(
    *,
    role: str,
    topic: str = "generic",
    refs: Sequence[str] | None = None,
) -> str:
    normalized_role = _normalize_support_token(role, force_lower=True)
    normalized_topic = _normalize_support_token(topic, force_lower=True)
    normalized_refs = [
        _normalize_support_token(ref, force_upper=True)
        for ref in refs or ()
        if str(ref).strip()
    ]
    payload = "_".join([normalized_role, normalized_topic, *normalized_refs])
    if len(payload.encode("utf-8")) <= SUPPORT_START_PAYLOAD_MAX_BYTES:
        return payload

    fallback = f"{normalized_role}_generic"
    if len(fallback.encode("utf-8")) > SUPPORT_START_PAYLOAD_MAX_BYTES:
        raise ValueError("support start payload fallback exceeds Telegram limit")
    return fallback


def build_support_deep_link(
    *,
    bot_username: str,
    role: str,
    topic: str = "generic",
    refs: Sequence[str] | None = None,
) -> str:
    normalized_username = bot_username.strip().lstrip("@")
    if not normalized_username:
        raise ValueError("support bot username must not be empty")
    payload = build_support_start_payload(role=role, topic=topic, refs=refs)
    return f"https://t.me/{normalized_username}?start={payload}"


def _format_prefixed_ref(prefix: str, entity_id: int) -> str:
    normalized_id = int(entity_id)
    if normalized_id < 1:
        raise ValueError("entity id must be >= 1")
    return f"{prefix}{normalized_id}"


def _parse_prefixed_ref(value: str, *, prefix: str) -> int:
    normalized = str(value).strip().upper().lstrip("#")
    if not normalized:
        raise ValueError("reference must not be empty")
    if normalized.isdigit():
        parsed = int(normalized)
    elif normalized.startswith(prefix) and normalized[len(prefix) :].isdigit():
        parsed = int(normalized[len(prefix) :])
    else:
        raise ValueError(f"invalid {prefix} reference")
    if parsed < 1:
        raise ValueError(f"{prefix} reference must be >= 1")
    return parsed


def _normalize_support_token(
    value: str,
    *,
    force_lower: bool = False,
    force_upper: bool = False,
) -> str:
    normalized = str(value).strip()
    if force_lower:
        normalized = normalized.lower()
    if force_upper:
        normalized = normalized.upper()
    if not normalized:
        raise ValueError("support token must not be empty")
    if not _SUPPORT_TOKEN_RE.fullmatch(normalized):
        raise ValueError("support token may contain only letters, digits, and underscores")
    return normalized

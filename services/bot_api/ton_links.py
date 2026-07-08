from __future__ import annotations

import urllib.parse
from decimal import ROUND_HALF_UP, Decimal

USDT_EXACT_QUANT = Decimal("0.000001")


def build_ton_usdt_transfer_link(
    *,
    destination_address: str,
    amount_usdt: Decimal | str,
    jetton_master: str,
    text: str | None = None,
) -> str:
    normalized_address = destination_address.strip()
    normalized_jetton = jetton_master.strip()
    if not normalized_jetton:
        raise ValueError("jetton_master must not be empty")
    amount = Decimal(str(amount_usdt)).quantize(USDT_EXACT_QUANT, rounding=ROUND_HALF_UP)
    base_units = int(amount * Decimal("1000000"))
    params = {"jetton": normalized_jetton, "amount": str(base_units)}
    if text:
        params["text"] = text.strip()
    query = urllib.parse.urlencode(params)
    encoded_address = urllib.parse.quote(normalized_address, safe="")
    return f"ton://transfer/{encoded_address}?{query}"

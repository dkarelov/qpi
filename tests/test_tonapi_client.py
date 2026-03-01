from __future__ import annotations

from libs.integrations.tonapi import TonapiClient


async def test_tonapi_history_keeps_transfer_with_empty_query_id(monkeypatch) -> None:
    client = TonapiClient(base_url="https://tonapi.io", unauth_min_interval_seconds=0)
    tx_hash = "cf6bf9aa4e67dd82ae7595c1c3154a1fc24dc621e6db51e511b6bff2695cf5eb"

    payload = {
        "operations": [
            {
                "operation": "transfer",
                "utime": 1772372933,
                "lt": 67472835000006,
                "transaction_hash": tx_hash,
                "source": {"address": "0:source"},
                "destination": {"address": "0:destination"},
                "amount": "1000200",
                "jetton": {"decimals": 6},
                "trace_id": "trace-1",
                "query_id": "",
                "payload": {},
            }
        ],
        "next_from": None,
    }

    def _stub_request_json(path: str, query: dict[str, str] | None):
        assert "/v2/jettons/" in path
        assert query == {"limit": "100"}
        return payload

    monkeypatch.setattr(client, "_request_json", _stub_request_json)

    page = await client.get_jetton_account_history(
        account_id="UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH",
        jetton_id="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        limit=100,
    )

    assert len(page.operations) == 1
    operation = page.operations[0]
    assert operation.query_id == ""
    assert operation.trace_id == "trace-1"
    assert str(operation.amount_usdt) == "1.0002"

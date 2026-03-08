from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from libs.integrations.wb_public import WbPublicApiError, WbPublicCatalogClient


@pytest.mark.asyncio
async def test_fetch_product_snapshot_extracts_card_fields() -> None:
    client = WbPublicCatalogClient()
    client._fetch_content_json_with_retries = AsyncMock(  # noqa: SLF001
        return_value={
            "cards": [
                {
                    "nmID": 835298449,
                    "subjectName": "Ручки мебельные",
                    "vendorCode": "6692/medmat32",
                    "brand": "Hugel",
                    "title": "Ручки мебельные скоба",
                    "description": "Белая бумага для офиса",
                    "photos": [
                        {
                            "c516x688": "https://basket-38.wbbasket.ru/vol8352/part835298/835298449/images/c516x688/1.webp"
                        }
                    ],
                    "sizes": [{"techSize": "0"}],
                    "characteristics": [
                        {"name": "Материал изделия", "value": ["алюминиевый сплав"]},
                        {"name": "Ширина предмета", "value": 2},
                    ],
                }
            ]
        }
    )

    snapshot = await client.fetch_product_snapshot(token="wb-token", wb_product_id=835298449)

    assert snapshot.wb_product_id == 835298449
    assert snapshot.subject_name == "Ручки мебельные"
    assert snapshot.vendor_code == "6692/medmat32"
    assert snapshot.brand == "Hugel"
    assert snapshot.name == "Ручки мебельные скоба"
    assert snapshot.photo_url.endswith("/c516x688/1.webp")
    assert snapshot.tech_sizes == ["0"]
    assert snapshot.characteristics == [
        {"name": "Материал изделия", "value": "алюминиевый сплав"},
        {"name": "Ширина предмета", "value": "2"},
    ]


@pytest.mark.asyncio
async def test_fetch_product_snapshot_rejects_missing_card() -> None:
    client = WbPublicCatalogClient()
    client._fetch_content_json_with_retries = AsyncMock(return_value={"cards": []})  # noqa: SLF001

    with pytest.raises(WbPublicApiError, match="not found"):
        await client.fetch_product_snapshot(token="wb-token", wb_product_id=225954014)


@pytest.mark.asyncio
async def test_lookup_buyer_price_uses_latest_non_cancelled_order() -> None:
    client = WbPublicCatalogClient()
    client._fetch_orders_json_with_retries = AsyncMock(  # noqa: SLF001
        return_value=[
            {
                "nmId": 835298449,
                "isCancel": True,
                "priceWithDisc": 500,
                "spp": 10,
                "lastChangeDate": "2026-03-01T12:00:00",
            },
            {
                "nmId": 835298449,
                "isCancel": False,
                "priceWithDisc": 425,
                "spp": 3,
                "lastChangeDate": "2026-03-02T12:00:00",
            },
        ]
    )

    price = await client.lookup_buyer_price(token="wb-token", wb_product_id=835298449)

    assert price is not None
    assert price.seller_price_rub == 425
    assert price.spp_percent == 3
    assert price.buyer_price_rub == 400
    assert price.observed_at == datetime(2026, 3, 2, 9, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_lookup_buyer_price_returns_none_when_product_has_no_orders() -> None:
    client = WbPublicCatalogClient()
    client._fetch_orders_json_with_retries = AsyncMock(return_value=[])  # noqa: SLF001

    price = await client.lookup_buyer_price(token="wb-token", wb_product_id=835298449)

    assert price is None

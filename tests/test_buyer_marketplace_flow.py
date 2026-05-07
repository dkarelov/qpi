from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from libs.domain.errors import InvalidStateError, NotFoundError
from services.bot_api.buyer_marketplace_flow import (
    BuyerMarketplaceFlow,
    BuyerMarketplaceFlowConfig,
)
from services.bot_api.transport_effects import ReplaceText, ReplyPhoto, SetUserData


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def _listing(listing_id: int, *, title: str | None = None) -> SimpleNamespace:
    return _ns(
        listing_id=listing_id,
        shop_id=11,
        wb_product_id=552892532 + listing_id,
        display_title=title or f"Товар {listing_id}",
        wb_source_title=f"WB source {listing_id}",
        wb_subject_name="Бумага офисная",
        wb_brand_name="Internal Brand",
        wb_description="Белая бумага для офиса",
        wb_photo_url="https://example.com/photo.webp",
        wb_tech_sizes=["0"],
        wb_characteristics=[{"name": "Плотность", "value": "80 г/м2"}],
        reference_price_rub=400,
        search_phrase="бумага а4 для принтера",
        reward_usdt=Decimal("0.250000"),
    )


@dataclass
class FakeBuyerMarketplaceAdapter:
    balance: Any = field(default_factory=lambda: _ns(buyer_available_usdt=Decimal("1.250000")))
    assignments: list[Any] = field(default_factory=list)
    saved_shops: list[Any] = field(default_factory=list)
    shop: Any = field(default_factory=lambda: _ns(shop_id=11, slug="shop_tushenka", title="Тушенка"))
    listings: list[Any] = field(default_factory=lambda: [_listing(21, title="Бумага A4 для принтера")])
    resolve_saved_shop_result: Any | None = None
    remove_result: Any = field(default_factory=lambda: _ns(changed=True))
    resolve_saved_side_effect: Exception | None = None
    remove_side_effect: Exception | None = None
    touch_calls: list[tuple[int, int]] = field(default_factory=list)

    async def get_buyer_balance_snapshot(self, *, buyer_user_id: int) -> Any:
        return self.balance

    async def list_buyer_assignments(self, *, buyer_user_id: int) -> list[Any]:
        return self.assignments

    async def list_saved_shops(self, *, buyer_user_id: int, limit: int = 20) -> list[Any]:
        return self.saved_shops[:limit]

    async def resolve_shop_by_slug(self, *, slug: str) -> Any:
        if slug != self.shop.slug:
            raise NotFoundError("shop not found")
        return self.shop

    async def list_active_listings_by_shop_slug(
        self,
        *,
        slug: str,
        buyer_user_id: int | None = None,
    ) -> list[Any]:
        if slug != self.shop.slug:
            raise NotFoundError("shop not found")
        return self.listings

    async def touch_saved_shop(self, *, buyer_user_id: int, shop_id: int) -> None:
        self.touch_calls.append((buyer_user_id, shop_id))

    async def resolve_saved_shop_for_buyer(self, *, buyer_user_id: int, shop_id: int) -> Any:
        if self.resolve_saved_side_effect is not None:
            raise self.resolve_saved_side_effect
        return self.resolve_saved_shop_result or self.shop

    async def remove_saved_shop(self, *, buyer_user_id: int, shop_id: int) -> Any:
        if self.remove_side_effect is not None:
            raise self.remove_side_effect
        return self.remove_result


def _flow(
    adapter: FakeBuyerMarketplaceAdapter | None = None,
    *,
    support_bot_username: str | None = "qpilka_support_bot",
) -> tuple[BuyerMarketplaceFlow, FakeBuyerMarketplaceAdapter]:
    adapter = adapter or FakeBuyerMarketplaceAdapter()
    return (
        BuyerMarketplaceFlow(
            adapter=adapter,
            config=BuyerMarketplaceFlowConfig(
                display_rub_per_usdt=Decimal("100"),
                support_bot_username=support_bot_username,
            ),
        ),
        adapter,
    )


@pytest.mark.asyncio
async def test_buyer_marketplace_flow_renders_dashboard_counts_and_balance() -> None:
    flow, _ = _flow(
        FakeBuyerMarketplaceAdapter(
            assignments=[
                _ns(status="reserved"),
                _ns(status="order_verified"),
                _ns(status="picked_up_wait_unlock"),
                _ns(status="expired_2h"),
            ],
            saved_shops=[_ns(shop_id=1), _ns(shop_id=2)],
        )
    )

    result = await flow.render_dashboard(buyer_user_id=202)

    screen = result.effects[0]
    assert isinstance(screen, ReplaceText)
    assert "ожидают заказа: 1 · заказаны: 1 · выкуплены: 1" in screen.text
    assert "<b>Баланс:</b> ~125 ₽" in screen.text
    labels = [button.text for row in screen.buttons for button in row]
    assert "🏪 Магазины · 2" in labels
    assert "📋 Покупки · 3" in labels
    assert "🆘 Поддержка" not in labels


def test_buyer_marketplace_flow_keeps_support_inside_guide_only() -> None:
    flow, _ = _flow()

    guide = flow.render_knowledge_screen(topic="guide").effects[0]
    shops = flow.render_knowledge_screen(topic="shops").effects[0]

    assert isinstance(guide, ReplaceText)
    assert "Инструкция покупателя" in guide.text
    guide_buttons = [button for row in guide.buttons for button in row]
    assert any(button.text == "🆘 Поддержка" and button.url for button in guide_buttons)
    assert isinstance(shops, ReplaceText)
    assert "🆘 Поддержка" not in [button.text for row in shops.buttons for button in row]


@pytest.mark.asyncio
async def test_buyer_marketplace_flow_renders_saved_shop_numbered_pagination() -> None:
    saved_shops = [
        _ns(shop_id=index, title=f"Магазин {index}", slug=f"shop_{index}", active_listings_count=index % 2)
        for index in range(1, 12)
    ]
    flow, _ = _flow(FakeBuyerMarketplaceAdapter(saved_shops=saved_shops))

    result = await flow.render_shops_section(buyer_user_id=202, page=2)

    screen = result.effects[0]
    assert isinstance(screen, ReplaceText)
    assert "11. 🟢 Магазин 11 (объявлений: 1)" in screen.text
    assert "\n<b>1. " not in screen.text
    labels = [button.text for row in screen.buttons for button in row]
    assert "11" in labels
    assert "⬅️" in labels


@pytest.mark.asyncio
async def test_buyer_marketplace_flow_open_saved_shop_stores_slug_and_renders_catalog() -> None:
    flow, adapter = _flow()

    result = await flow.open_saved_shop(buyer_user_id=202, shop_id=11)

    store, screen = result.effects
    assert isinstance(store, SetUserData)
    assert store.key == "last_buyer_shop_slug"
    assert store.value == "shop_tushenka"
    assert isinstance(screen, ReplaceText)
    assert "Магазин «Тушенка»" in screen.text
    assert "Бумага A4 для принтера" in screen.text
    assert adapter.touch_calls == [(202, 11)]


@pytest.mark.asyncio
async def test_buyer_marketplace_flow_blocks_saved_shop_removal_with_unfinished_purchase() -> None:
    flow, _ = _flow(FakeBuyerMarketplaceAdapter(remove_side_effect=InvalidStateError("unfinished purchase")))

    result = await flow.remove_saved_shop(buyer_user_id=202, shop_id=11)

    screen = result.effects[0]
    assert isinstance(screen, ReplaceText)
    assert "Удаление недоступно, пока в магазине есть незавершенная покупка." in screen.text
    labels = [button.text for row in screen.buttons for button in row]
    assert labels == ["📋 Покупки", "↩️ Назад к магазинам"]


@pytest.mark.asyncio
async def test_buyer_marketplace_flow_catalog_uses_numbered_listing_pagination_without_buy_button() -> None:
    flow, _ = _flow(FakeBuyerMarketplaceAdapter(listings=[_listing(index) for index in range(1, 13)]))

    result = await flow.render_shop_catalog(slug="shop_tushenka", buyer_user_id=202, replace=True, page=2)

    store, screen = result.effects
    assert isinstance(store, SetUserData)
    assert isinstance(screen, ReplaceText)
    assert "11. Товар 11" in screen.text
    assert "12. Товар 12" in screen.text
    assert "\n<b>1. Товар 1" not in screen.text
    labels = [button.text for row in screen.buttons for button in row]
    assert "11" in labels
    assert "12" in labels
    assert "⬅️" in labels
    assert "✅ Купить" not in labels
    assert "🔎 Просмотр" not in labels


@pytest.mark.asyncio
async def test_buyer_marketplace_flow_listing_detail_hides_internal_wb_fields() -> None:
    flow, _ = _flow()

    result = await flow.render_listing_detail(
        buyer_user_id=202,
        shop_slug="shop_tushenka",
        listing_id=21,
    )

    photo, screen = result.effects
    assert isinstance(photo, ReplyPhoto)
    assert photo.photo_url == "https://example.com/photo.webp"
    assert isinstance(screen, ReplaceText)
    assert "Цена:</b> 400 ₽" in screen.text
    assert "Характеристики" in screen.text
    assert "Артикул WB:</b>" not in screen.text
    assert "Бренд:</b>" not in screen.text
    assert "WB source" not in screen.text
    labels = [button.text for row in screen.buttons for button in row]
    assert "✅ Купить" in labels

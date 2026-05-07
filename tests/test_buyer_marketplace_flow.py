from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from libs.domain.errors import InvalidStateError, NoSlotsAvailableError, NotFoundError, PayloadValidationError
from services.bot_api.buyer_marketplace_flow import (
    BuyerMarketplaceFlow,
    BuyerMarketplaceFlowConfig,
)
from services.bot_api.transport_effects import (
    ClearPrompt,
    LogEvent,
    ReplaceText,
    ReplyPhoto,
    ReplyText,
    SetPrompt,
    SetUserData,
)


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


def _assignment(
    assignment_id: int = 31,
    *,
    status: str = "reserved",
    listing_id: int = 21,
    review_status: str | None = None,
    review_reason: str | None = None,
) -> SimpleNamespace:
    return _ns(
        assignment_id=assignment_id,
        listing_id=listing_id,
        task_uuid="11111111-1111-4111-8111-111111111111",
        shop_slug="shop_tushenka",
        shop_title="Тушенка",
        wb_product_id=552892532,
        status=status,
        display_title="Бумага A4 для принтера",
        search_phrase="бумага а4 для принтера",
        reward_usdt=Decimal("0.250000"),
        reference_price_rub=400,
        reservation_expires_at=datetime(2026, 3, 2, 14, 0, tzinfo=UTC),
        order_id="order-1" if status != "reserved" else None,
        wb_brand_name="BRAUBERG",
        review_phrases=["плотная бумага", "белая"],
        review_verification_status=review_status,
        review_verification_reason=review_reason,
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
    reserve_result: Any = field(default_factory=lambda: _ns(assignment_id=31, created=True))
    purchase_result: Any = field(
        default_factory=lambda: _ns(
            assignment_id=31,
            changed=True,
            order_id="order-1",
        )
    )
    review_result: Any = field(
        default_factory=lambda: _ns(
            assignment_id=31,
            changed=True,
            verification_status="verified_auto",
            verification_reason=None,
        )
    )
    cancel_result: Any = field(default_factory=lambda: _ns(changed=True))
    resolve_saved_side_effect: Exception | None = None
    remove_side_effect: Exception | None = None
    reserve_side_effect: Exception | None = None
    purchase_side_effect: Exception | None = None
    review_side_effect: Exception | None = None
    cancel_side_effect: Exception | None = None
    touch_calls: list[tuple[int, int]] = field(default_factory=list)
    reserve_calls: list[dict[str, Any]] = field(default_factory=list)
    purchase_calls: list[dict[str, Any]] = field(default_factory=list)
    review_calls: list[dict[str, Any]] = field(default_factory=list)
    cancel_calls: list[dict[str, Any]] = field(default_factory=list)

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

    async def reserve_listing_slot(
        self,
        *,
        buyer_user_id: int,
        listing_id: int,
        idempotency_key: str,
    ) -> Any:
        self.reserve_calls.append(
            {
                "buyer_user_id": buyer_user_id,
                "listing_id": listing_id,
                "idempotency_key": idempotency_key,
            }
        )
        if self.reserve_side_effect is not None:
            raise self.reserve_side_effect
        return self.reserve_result

    async def submit_purchase_payload(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int,
        payload_base64: str,
    ) -> Any:
        self.purchase_calls.append(
            {
                "buyer_user_id": buyer_user_id,
                "assignment_id": assignment_id,
                "payload_base64": payload_base64,
            }
        )
        if self.purchase_side_effect is not None:
            raise self.purchase_side_effect
        return self.purchase_result

    async def submit_review_payload(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int,
        payload_base64: str,
    ) -> Any:
        self.review_calls.append(
            {
                "buyer_user_id": buyer_user_id,
                "assignment_id": assignment_id,
                "payload_base64": payload_base64,
            }
        )
        if self.review_side_effect is not None:
            raise self.review_side_effect
        return self.review_result

    async def cancel_assignment_by_buyer(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int,
        idempotency_key: str,
    ) -> Any:
        self.cancel_calls.append(
            {
                "buyer_user_id": buyer_user_id,
                "assignment_id": assignment_id,
                "idempotency_key": idempotency_key,
            }
        )
        if self.cancel_side_effect is not None:
            raise self.cancel_side_effect
        return self.cancel_result


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


@pytest.mark.asyncio
async def test_buyer_marketplace_flow_reserves_purchase_with_instruction_and_idempotency_key() -> None:
    flow, adapter = _flow(FakeBuyerMarketplaceAdapter(assignments=[_assignment()]))

    result = await flow.reserve_listing(buyer_user_id=202, listing_id=21, callback_query_id="cbq-1")

    log, screen = result.effects
    assert isinstance(log, LogEvent)
    assert log.event_name == "buyer_slot_reserved"
    assert log.fields["listing_ref"] == "L21"
    assert adapter.reserve_calls == [
        {
            "buyer_user_id": 202,
            "listing_id": 21,
            "idempotency_key": "tg-reserve:202:21:cbq-1",
        }
    ]
    assert isinstance(screen, ReplaceText)
    assert "Покупка создана" in screen.text
    assert "Введите токен в " in screen.text
    labels = [button.text for row in screen.buttons for button in row]
    assert "Ввести токен-подтверждение" in labels
    assert "🚫 Отказаться от покупки" in labels


@pytest.mark.asyncio
async def test_buyer_marketplace_flow_maps_unavailable_and_duplicate_reservation_outcomes() -> None:
    unavailable_flow, _ = _flow(FakeBuyerMarketplaceAdapter(reserve_side_effect=NoSlotsAvailableError()))
    active_flow, _ = _flow(
        FakeBuyerMarketplaceAdapter(
            assignments=[_assignment(status="reserved")],
            reserve_side_effect=NoSlotsAvailableError(),
        )
    )
    purchased_flow, _ = _flow(
        FakeBuyerMarketplaceAdapter(reserve_side_effect=InvalidStateError("already purchased wb_product_id"))
    )

    unavailable = await unavailable_flow.reserve_listing(buyer_user_id=202, listing_id=21, callback_query_id="cbq-1")
    active = await active_flow.reserve_listing(buyer_user_id=202, listing_id=21, callback_query_id="cbq-1")
    purchased = await purchased_flow.reserve_listing(buyer_user_id=202, listing_id=21, callback_query_id="cbq-1")

    assert "Свободных покупок по этому товару нет." in unavailable.effects[0].text
    assert "У вас уже есть активная покупка по этому товару." in active.effects[0].text
    assert "Этот товар уже был куплен с вашего аккаунта." in purchased.effects[0].text


@pytest.mark.asyncio
async def test_buyer_marketplace_flow_purchase_list_shows_reserved_and_pending_manual_review_actions() -> None:
    flow, _ = _flow(
        FakeBuyerMarketplaceAdapter(
            assignments=[
                _assignment(status="reserved"),
                _assignment(
                    assignment_id=32,
                    status="picked_up_wait_review",
                    review_status="pending_manual",
                    review_reason="missing required phrase",
                ),
            ]
        )
    )

    result = await flow.render_assignments(buyer_user_id=202)

    screen = result.effects[0]
    assert isinstance(screen, ReplaceText)
    assert "Проверка отзыва:</b> missing required phrase" in screen.text
    assert "Исправьте отзыв или напишите в поддержку со скриншотом." in screen.text
    labels = [button.text for row in screen.buttons for button in row]
    assert "Ввести токен-подтверждение" in labels
    assert "🚫 Отказаться от покупки" in labels
    assert "✍️ Ввести токен отзыва" in labels


def test_buyer_marketplace_flow_starts_sensitive_purchase_and_review_prompts() -> None:
    flow, _ = _flow()

    purchase = flow.start_purchase_payload_prompt(assignment_id=31)
    review = flow.start_review_payload_prompt(assignment_id=31)

    purchase_prompt, purchase_screen = purchase.effects
    assert isinstance(purchase_prompt, SetPrompt)
    assert purchase_prompt.prompt_type == "buyer_submit_payload"
    assert purchase_prompt.sensitive is True
    assert purchase_prompt.data == {"assignment_id": 31}
    assert isinstance(purchase_screen, ReplaceText)
    assert "Токен-подтверждение" in purchase_screen.text

    review_prompt, review_screen = review.effects
    assert isinstance(review_prompt, SetPrompt)
    assert review_prompt.prompt_type == "buyer_submit_review_payload"
    assert review_prompt.sensitive is True
    assert review_prompt.data == {"assignment_id": 31}
    assert isinstance(review_screen, ReplaceText)
    assert "Токен отзыва" in review_screen.text


@pytest.mark.asyncio
async def test_buyer_marketplace_flow_submits_purchase_payload_and_preserves_retry_on_validation_error() -> None:
    success_flow, success_adapter = _flow()
    invalid_flow, invalid_adapter = _flow(
        FakeBuyerMarketplaceAdapter(purchase_side_effect=PayloadValidationError("task_uuid mismatch"))
    )

    success = await success_flow.submit_purchase_payload(
        prompt_state={"assignment_id": 31},
        text="payload",
        buyer_user_id=202,
        update_id=501,
    )
    invalid = await invalid_flow.submit_purchase_payload(
        prompt_state={"assignment_id": 31},
        text="bad",
        buyer_user_id=202,
        update_id=502,
    )

    assert success_adapter.purchase_calls[0]["payload_base64"] == "payload"
    assert isinstance(success.effects[0], ClearPrompt)
    assert isinstance(success.effects[1], LogEvent)
    assert isinstance(success.effects[2], ReplyText)
    assert "Токен-подтверждение принят." in success.effects[2].text
    assert invalid_adapter.purchase_calls[0]["payload_base64"] == "bad"
    assert len(invalid.effects) == 1
    assert isinstance(invalid.effects[0], ReplyText)
    assert "Похоже, токен относится к другой покупке" in invalid.effects[0].text


@pytest.mark.asyncio
async def test_buyer_marketplace_flow_submits_review_payload_with_pending_manual_followup() -> None:
    flow, adapter = _flow(
        FakeBuyerMarketplaceAdapter(
            review_result=_ns(
                assignment_id=31,
                changed=True,
                verification_status="pending_manual",
                verification_reason="required phrase missing",
            )
        )
    )

    result = await flow.submit_review_payload(
        prompt_state={"assignment_id": 31},
        text="review-payload",
        buyer_user_id=202,
        update_id=601,
    )

    assert adapter.review_calls[0]["payload_base64"] == "review-payload"
    clear_prompt, log, reply = result.effects
    assert isinstance(clear_prompt, ClearPrompt)
    assert isinstance(log, LogEvent)
    assert isinstance(reply, ReplyText)
    assert "Кэшбэк пока не будет выплачен." in reply.text
    assert "Причина: required phrase missing" in reply.text
    labels = [button.text for row in reply.buttons for button in row]
    assert "🆘 Поддержка" in labels
    assert "🏪 Магазины" in labels


@pytest.mark.asyncio
async def test_buyer_marketplace_flow_confirms_assignment_cancel_idempotently() -> None:
    flow, adapter = _flow(FakeBuyerMarketplaceAdapter(assignments=[_assignment()]))

    prompt = await flow.start_assignment_cancel_prompt(buyer_user_id=202, assignment_id=31)
    confirm = await flow.confirm_assignment_cancel(
        buyer_user_id=202,
        assignment_id=31,
        callback_query_id="cancel-1",
    )

    assert isinstance(prompt.effects[0], ReplaceText)
    assert "Бронь будет снята" in prompt.effects[0].text
    assert adapter.cancel_calls == [
        {
            "buyer_user_id": 202,
            "assignment_id": 31,
            "idempotency_key": "tg-assignment-cancel:202:31:cancel-1",
        }
    ]
    assert isinstance(confirm.effects[0], ReplaceText)
    assert "Покупка отменена." in confirm.effects[0].text

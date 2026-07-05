from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from libs.domain.errors import InsufficientFundsError, InvalidStateError, NotFoundError
from libs.integrations.wb import WbPingResult
from services.bot_api.seller_marketplace_flow import SellerMarketplaceFlow, SellerMarketplaceFlowConfig
from services.bot_api.transport_effects import (
    ClearPrompt,
    FlowResult,
    LogEvent,
    ReplaceText,
    ReplyPhoto,
    ReplyText,
    SetPrompt,
    SetUserData,
)


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def _shop(shop_id: int = 11, *, title: str = "Тушенка", token_status: str = "valid") -> SimpleNamespace:
    return _ns(shop_id=shop_id, title=title, slug="shop_tushenka", wb_token_status=token_status)


def _listing(listing_id: int = 21, *, status: str = "active") -> SimpleNamespace:
    return _ns(
        listing_id=listing_id,
        shop_id=11,
        display_title="Бумага A4 для принтера",
        reference_price_rub=400,
        reference_price_source="orders",
        wb_photo_url="https://example.com/photo.webp",
        wb_product_id=552892532,
        search_phrase="бумага а4 для принтера",
        status=status,
        reward_usdt=Decimal("1.000000"),
        available_slots=5,
        slot_count=5,
        in_progress_assignments_count=0,
        collateral_locked_usdt=Decimal("5.050000"),
        collateral_required_usdt=Decimal("5.050000"),
        reserved_slot_usdt=Decimal("0.000000"),
        wb_subject_name="Бумага офисная",
        wb_vendor_code="paper-001",
        wb_brand_name="BRAUBERG",
        wb_source_title="BRAUBERG Бумага A4 для принтера",
        wb_description="Белая бумага для офиса",
        wb_tech_sizes=["0"],
        wb_characteristics=[{"name": "Плотность", "value": "80 г/м2"}],
        review_phrases=["плотная бумага", "белая"],
    )


def _balance(*, available: str = "10.000000", collateral: str = "5.050000", pending: str = "0.000000"):
    return _ns(
        seller_available_usdt=Decimal(available),
        seller_collateral_usdt=Decimal(collateral),
        seller_withdraw_pending_usdt=Decimal(pending),
    )


def _labels(effect: ReplaceText | ReplyText) -> list[str]:
    return [button.text for row in effect.buttons for button in row]


def _actions(effect: ReplaceText | ReplyText) -> list[str]:
    return [button.action for row in effect.buttons for button in row]


class FakeSellerService:
    def __init__(self) -> None:
        self.shops = [_shop()]
        self.listings = [_listing()]
        self.balance = _balance()
        self.created_shop = _shop(12, title="Новый магазин")
        self.delete_result = _ns(
            changed=True,
            assignment_transferred_usdt=Decimal("1.000000"),
            unassigned_collateral_returned_usdt=Decimal("2.000000"),
        )

    async def list_shops(self, *, seller_user_id: int) -> list[Any]:
        return self.shops

    async def list_listing_collateral_views(self, *, seller_user_id: int) -> list[Any]:
        return self.listings

    async def get_seller_balance_snapshot(self, *, seller_user_id: int) -> Any:
        return self.balance

    async def get_seller_order_counters(self, *, seller_user_id: int) -> dict[str, int]:
        return {"awaiting_order": 1, "ordered": 2, "picked_up": 3}

    async def get_shop(self, *, seller_user_id: int, shop_id: int) -> Any:
        return next(item for item in self.shops if item.shop_id == shop_id)

    async def create_shop(self, *, seller_user_id: int, title: str) -> Any:
        self.created_shop = _shop(12, title=title)
        return self.created_shop

    async def save_validated_shop_token(self, *, seller_user_id: int, shop_id: int, token_ciphertext: str) -> Any:
        return _ns(changed=True)

    async def rename_shop(self, *, seller_user_id: int, shop_id: int, title: str) -> Any:
        return _shop(shop_id, title=title)

    async def get_shop_delete_preview(self, *, seller_user_id: int, shop_id: int) -> Any:
        return _ns(
            active_listings_count=1,
            open_assignments_count=2,
            assignment_linked_reserved_usdt=Decimal("1.000000"),
            unassigned_collateral_usdt=Decimal("2.000000"),
        )

    async def delete_shop(self, *, seller_user_id: int, shop_id: int, deleted_by_user_id: int, idempotency_key: str):
        return self.delete_result

    async def get_listing(self, *, seller_user_id: int, listing_id: int) -> Any:
        return next(item for item in self.listings if item.listing_id == listing_id)

    async def activate_listing(self, *, seller_user_id: int, listing_id: int, idempotency_key: str) -> Any:
        return _ns(changed=True)

    async def pause_listing(self, *, seller_user_id: int, listing_id: int, reason: str) -> Any:
        return _ns(changed=True)

    async def unpause_listing(self, *, seller_user_id: int, listing_id: int) -> Any:
        return _ns(changed=True)

    async def get_listing_delete_preview(self, *, seller_user_id: int, listing_id: int) -> Any:
        return _ns(
            open_assignments_count=1,
            assignment_linked_reserved_usdt=Decimal("1.000000"),
            unassigned_collateral_usdt=Decimal("2.000000"),
        )

    async def delete_listing(
        self,
        *,
        seller_user_id: int,
        listing_id: int,
        deleted_by_user_id: int,
        idempotency_key: str,
    ):
        return self.delete_result


class FakeFinanceService:
    def __init__(self) -> None:
        self.active_request = None
        self.history = []

    async def get_active_seller_withdrawal_request(self, *, seller_user_id: int) -> Any | None:
        return self.active_request

    async def list_seller_withdrawal_history(self, *, seller_user_id: int, limit: int) -> list[Any]:
        return self.history[:limit]


class FakeDepositService:
    def __init__(self) -> None:
        self.intents = []
        self.intent = _ns(
            deposit_intent_id=91,
            deposit_address="UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH",
            expected_amount_usdt=Decimal("1.200100"),
        )

    async def list_active_shards(self) -> list[Any]:
        return [_ns(shard_id=1, shard_key="mvp-1", deposit_address=self.intent.deposit_address)]

    async def create_seller_deposit_intent(
        self,
        *,
        seller_user_id: int,
        request_amount_usdt: Decimal,
        shard_id: int,
        idempotency_key: str,
    ) -> Any:
        return self.intent

    async def list_seller_deposit_intents(self, *, seller_user_id: int, limit: int) -> list[Any]:
        return self.intents[:limit]


class FakePingClient:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid

    async def validate_token(self, token: str) -> WbPingResult:
        return WbPingResult(valid=self.valid, status_code=200 if self.valid else 401, message="ok")


class FakeListingCreationFlow:
    def start_prompt(self, *, seller_user_id: int, shop_id: int, shop_title: str):
        return FlowResult(effects=(ReplyText(text=f"create {shop_title}", parse_mode=None),))


def _flow(
    *,
    seller: FakeSellerService | None = None,
    finance: FakeFinanceService | None = None,
    deposit: FakeDepositService | None = None,
    ping: FakePingClient | None = None,
) -> SellerMarketplaceFlow:
    return SellerMarketplaceFlow(
        seller_service=seller or FakeSellerService(),
        seller_workflow=None,
        finance_service=finance or FakeFinanceService(),
        deposit_service=deposit or FakeDepositService(),
        wb_ping_client=ping or FakePingClient(),
        listing_creation_flow=FakeListingCreationFlow(),  # type: ignore[arg-type]
        config=SellerMarketplaceFlowConfig(
            display_rub_per_usdt=Decimal("100"),
            telegram_bot_username="qpilka_bot",
            token_cipher_key="test-key",
            seller_collateral_shard_key="mvp-1",
            seller_collateral_invoice_ttl_hours=24,
            tonapi_usdt_jetton_master="jetton-master",
            telegram_wallet_open_url="https://t.me/wallet/start",
            support_bot_username="qpilka_support_bot",
        ),
    )


@pytest.mark.asyncio
async def test_seller_marketplace_flow_dashboard_uses_button_descriptors_and_counts() -> None:
    result = await _flow().render_dashboard(seller_user_id=101)

    screen = result.effects[0]
    assert isinstance(screen, ReplaceText)
    assert "<b>🧑‍💼 Кабинет продавца</b>" in screen.text
    assert "<b>Магазины:</b> 1 · 1 активно" in screen.text
    assert "<b>Покупки:</b> ожидают заказа: 1 · заказаны: 2 · выкуплены: 3" in screen.text
    labels = [button.text for row in screen.buttons for button in row]
    assert labels[:4] == ["📦 Объявления · 1", "🏬 Магазины · 1", "💰 Баланс", "📘 Инструкция"]
    assert "🆘 Поддержка" in labels


@pytest.mark.asyncio
async def test_seller_marketplace_flow_shop_create_token_prompt_and_success() -> None:
    flow = _flow()

    prompt = flow.start_shop_create_token_prompt(seller_user_id=101).effects
    assert isinstance(prompt[0], SetPrompt)
    assert prompt[0].prompt_type == "seller_shop_create_token"
    assert prompt[0].sensitive is True
    assert isinstance(prompt[1], ReplaceText)
    assert "Отправьте токен WB API" in prompt[1].text

    token_result = await flow.submit_shop_create_token(prompt_state={"seller_user_id": 101}, text="wb-token")
    assert isinstance(token_result.effects[0], SetPrompt)
    assert token_result.effects[0].prompt_type == "seller_shop_title_after_token"
    assert isinstance(token_result.effects[1], ReplyText)
    assert "Шаг 2/2" in token_result.effects[1].text

    created = await flow.submit_shop_title_after_token(
        prompt_state={"seller_user_id": 101, "validated_token_ciphertext": "ciphertext"},
        text="Новый магазин",
    )
    assert isinstance(created.effects[0], ClearPrompt)
    assert isinstance(created.effects[1], ReplyText)
    assert "Магазин «Новый магазин» создан." in created.effects[1].text


@pytest.mark.asyncio
async def test_seller_marketplace_flow_shop_list_detail_delete_preview_and_execute() -> None:
    seller = FakeSellerService()
    flow = _flow(seller=seller)

    shops = await flow.render_shops(seller_user_id=101)
    shops_screen = shops.effects[0]
    assert isinstance(shops_screen, ReplaceText)
    assert "Выберите магазин." in shops_screen.text
    assert "🏬 Тушенка · S11" in _labels(shops_screen)

    detail = await flow.render_shop_details(seller_user_id=101, shop_id=11)
    detail_screen = detail.effects[0]
    assert isinstance(detail_screen, ReplaceText)
    assert "Магазин «Тушенка»" in detail_screen.text
    assert "✅ Токен WB API" in _labels(detail_screen)

    preview = await flow.render_shop_delete_preview(seller_user_id=101, shop_id=11)
    preview_screen = preview.effects[0]
    assert isinstance(preview_screen, ReplaceText)
    assert "Удаление магазина «Тушенка» необратимо" in preview_screen.text
    assert "✅ Подтвердить удаление" in _labels(preview_screen)

    deleted = await flow.execute_shop_delete(seller_user_id=101, shop_id=11)
    assert isinstance(deleted.effects[0], LogEvent)
    assert deleted.effects[0].event_name == "seller_shop_deleted"
    deleted_screen = deleted.effects[1]
    assert isinstance(deleted_screen, ReplaceText)
    assert "Магазин удален." in deleted_screen.text


@pytest.mark.asyncio
async def test_seller_marketplace_flow_shop_prompt_error_mapping() -> None:
    class ConflictSellerService(FakeSellerService):
        async def rename_shop(self, *, seller_user_id: int, shop_id: int, title: str) -> Any:
            raise InvalidStateError("title already exists")

    invalid_token = await _flow(ping=FakePingClient(valid=False)).submit_shop_create_token(
        prompt_state={"seller_user_id": 101},
        text="bad-token",
    )
    assert isinstance(invalid_token.effects[0], ReplyText)
    assert "Токен не прошел проверку и не сохранен." in invalid_token.effects[0].text

    rename_conflict = await _flow(seller=ConflictSellerService()).submit_shop_rename(
        prompt_state={"seller_user_id": 101, "shop_id": 11, "token_is_valid": True},
        text="Тушенка",
    )
    assert isinstance(rename_conflict.effects[0], ReplyText)
    assert "Магазин с таким названием уже существует." in rename_conflict.effects[0].text


@pytest.mark.asyncio
async def test_seller_marketplace_flow_listing_detail_returns_photo_and_no_edit_button() -> None:
    result = await _flow().render_listing_detail(seller_user_id=101, listing_id=21)

    assert isinstance(result.effects[0], ReplyPhoto)
    screen = result.effects[1]
    assert isinstance(screen, ReplaceText)
    assert "<b>📦 🟢 Бумага A4 для принтера</b>" in screen.text
    assert "<b>Артикул WB:</b> 552892532" in screen.text
    labels = [button.text for row in screen.buttons for button in row]
    actions = [button.action for row in screen.buttons for button in row]
    assert "✏️ Редактировать" not in labels
    assert "⏸ Пауза" in labels
    assert "listing_pause" in actions


@pytest.mark.asyncio
async def test_seller_marketplace_flow_listing_list_pagination_and_create_picker() -> None:
    seller = FakeSellerService()
    seller.listings = [_listing(listing_id=index, status="active") for index in range(1, 13)]
    flow = _flow(seller=seller)

    listings = await flow.render_listings(seller_user_id=101, page=2)
    assert isinstance(listings.effects[0], SetUserData)
    assert listings.effects[0].key == "seller_listings_page"
    assert listings.effects[0].value == 2
    screen = listings.effects[1]
    assert isinstance(screen, ReplaceText)
    assert "Объявления · стр. 2/2" in screen.text
    assert "11." in screen.text
    assert "listing_open" in _actions(screen)

    picker = await flow.render_listing_create_shop_picker(seller_user_id=101)
    picker_screen = picker.effects[0]
    assert isinstance(picker_screen, ReplaceText)
    assert "Выберите магазин для нового объявления." in picker_screen.text
    assert "🏬 Тушенка · 12" in _labels(picker_screen)

    prompt = await flow.start_listing_create_prompt(seller_user_id=101, shop_id=11)
    assert isinstance(prompt.effects[0], ReplyText)
    assert prompt.effects[0].text == "create Тушенка"


@pytest.mark.asyncio
async def test_seller_marketplace_flow_listing_executors_and_delete_preview() -> None:
    seller = FakeSellerService()
    seller.listings = [_listing(status="draft")]
    flow = _flow(seller=seller)

    activated = await flow.execute_listing_activate(seller_user_id=101, listing_id=21)
    assert isinstance(activated.effects[0], LogEvent)
    assert activated.effects[0].event_name == "seller_listing_activated"
    activated_screen = next(effect for effect in activated.effects if isinstance(effect, ReplaceText))
    assert "Объявление активно." in activated_screen.text

    paused = await flow.execute_listing_pause(seller_user_id=101, listing_id=21)
    assert isinstance(paused.effects[0], LogEvent)
    assert paused.effects[0].event_name == "seller_listing_paused"
    paused_screen = next(effect for effect in paused.effects if isinstance(effect, ReplaceText))
    assert "Объявление поставлено на паузу." in paused_screen.text

    unpaused = await flow.execute_listing_unpause(seller_user_id=101, listing_id=21)
    assert isinstance(unpaused.effects[0], LogEvent)
    assert unpaused.effects[0].event_name == "seller_listing_unpaused"
    unpaused_screen = next(effect for effect in unpaused.effects if isinstance(effect, ReplaceText))
    assert "Объявление снова активно." in unpaused_screen.text

    preview = await flow.render_listing_delete_preview(seller_user_id=101, listing_id=21)
    preview_screen = preview.effects[0]
    assert isinstance(preview_screen, ReplaceText)
    assert "Удаление объявления необратимо" in preview_screen.text
    assert "✅ Подтвердить удаление" in _labels(preview_screen)

    deleted = await flow.execute_listing_delete(seller_user_id=101, listing_id=21, list_page=2)
    assert isinstance(deleted.effects[0], LogEvent)
    assert deleted.effects[0].event_name == "seller_listing_deleted"
    deleted_screen = next(effect for effect in deleted.effects if isinstance(effect, ReplaceText))
    assert "Объявление удалено." in deleted_screen.text


@pytest.mark.asyncio
async def test_seller_marketplace_flow_listing_error_mapping() -> None:
    class MissingListingSellerService(FakeSellerService):
        async def get_listing(self, *, seller_user_id: int, listing_id: int) -> Any:
            raise NotFoundError("missing listing")

    class InsufficientFundsSellerService(FakeSellerService):
        async def activate_listing(self, *, seller_user_id: int, listing_id: int, idempotency_key: str) -> Any:
            raise InsufficientFundsError("not enough collateral")

    missing = await _flow(seller=MissingListingSellerService()).render_listing_detail(
        seller_user_id=101,
        listing_id=21,
    )
    missing_screen = next(effect for effect in missing.effects if isinstance(effect, ReplaceText))
    assert "Объявление не найдено или уже удалено." in missing_screen.text

    insufficient = await _flow(seller=InsufficientFundsSellerService()).execute_listing_activate(
        seller_user_id=101,
        listing_id=21,
    )
    assert isinstance(insufficient.effects[0], ReplaceText)
    assert "Недостаточно средств для активации" in insufficient.effects[0].text


@pytest.mark.asyncio
async def test_seller_marketplace_flow_balance_active_request_hides_new_withdraw_actions() -> None:
    finance = FakeFinanceService()
    finance.active_request = _ns(
        withdrawal_request_id=41,
        amount_usdt=Decimal("5.000000"),
        status="withdraw_pending_admin",
        payout_address="UQ-seller-wallet",
        requested_at=datetime(2026, 3, 2, 12, 0, tzinfo=UTC),
    )

    result = await _flow(finance=finance).render_balance(seller_user_id=101)

    screen = result.effects[0]
    assert isinstance(screen, ReplaceText)
    assert "<b>Активная заявка</b> · <code>W41</code>" in screen.text
    labels = [button.text for row in screen.buttons for button in row]
    assert "🚫 Отменить заявку" in labels
    assert "💸 Вывести все доступное" not in labels
    assert "✍️ Указать сумму вручную" not in labels


@pytest.mark.asyncio
async def test_seller_marketplace_flow_topup_prompt_creates_invoice_links() -> None:
    result = await _flow().submit_topup_amount(
        prompt_state={"seller_user_id": 101},
        text="1.2",
        update_id=501,
    )

    assert isinstance(result.effects[0], ClearPrompt)
    screen = result.effects[1]
    assert isinstance(screen, ReplyText)
    assert "Счет на пополнение создан" in screen.text
    assert "<code>1.2001 USDT</code>" in screen.text
    urls = [button.url for row in screen.buttons for button in row if button.url]
    assert "https://t.me/wallet/start" in urls
    assert any(url.startswith("ton://transfer/") and "jetton=jetton-master" in url for url in urls)


@pytest.mark.asyncio
async def test_seller_marketplace_flow_transaction_history_merges_topups_and_withdrawals() -> None:
    deposit = FakeDepositService()
    deposit.intents = [
        _ns(
            deposit_intent_id=91,
            expected_amount_usdt=Decimal("1.200100"),
            status="credited",
            credited_amount_usdt=Decimal("1.200100"),
            created_at=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
            expires_at=datetime(2026, 3, 2, 12, 0, tzinfo=UTC),
        )
    ]
    finance = FakeFinanceService()
    finance.history = [
        _ns(
            withdrawal_request_id=41,
            amount_usdt=Decimal("5.000000"),
            status="withdraw_sent",
            payout_address="UQ-seller-wallet",
            requested_at=datetime(2026, 3, 1, 13, 0, tzinfo=UTC),
            processed_at=datetime(2026, 3, 1, 14, 0, tzinfo=UTC),
            sent_at=datetime(2026, 3, 1, 15, 0, tzinfo=UTC),
            note=None,
            tx_hash="abc",
        )
    ]

    result = await _flow(finance=finance, deposit=deposit).render_transaction_history(seller_user_id=101)

    screen = result.effects[0]
    assert isinstance(screen, ReplaceText)
    assert "<b>Вывод</b> · <code>W41</code>" in screen.text
    assert "<b>Счет на пополнение</b> · <code>D91</code>" in screen.text
    assert "<b>Зачислено:</b> 1.2001 USDT" in screen.text

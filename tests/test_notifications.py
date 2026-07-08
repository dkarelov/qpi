from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from psycopg.rows import dict_row
from telegram.error import BadRequest, NetworkError

from libs.config.settings import BotApiSettings
from libs.domain.buyer import BuyerService
from libs.domain.ledger import FinanceService
from libs.domain.models import NotificationButton, NotificationOutboxItem, RenderedTelegramNotification
from libs.domain.notifications import (
    EVENT_ASSIGNMENT_EARLY_PAYOUT_LISTING_DELETE_BUYER,
    EVENT_ASSIGNMENT_PICKED_UP_BUYER,
    EVENT_ASSIGNMENT_RESERVATION_EXPIRED_BUYER,
    EVENT_ASSIGNMENT_REVIEW_CONFIRMED_SELLER,
    EVENT_ASSIGNMENT_REWARD_UNLOCKED_BUYER,
    EVENT_ASSIGNMENT_REWARD_UNLOCKED_SELLER,
    EVENT_DEPOSIT_CANCELLED_SELLER,
    EVENT_DEPOSIT_EXPIRED_SELLER,
    EVENT_DEPOSIT_MANUAL_REVIEW_ADMIN,
    EVENT_MANUAL_BALANCE_CREDIT_TARGET,
    EVENT_SELLER_TOKEN_INVALIDATED,
    EVENT_WITHDRAW_CANCELLED_ADMIN,
    EVENT_WITHDRAW_CREATED_ADMIN,
    EVENT_WITHDRAW_REJECTED_REQUESTER,
    EVENT_WITHDRAW_SENT_ADMIN,
    EVENT_WITHDRAW_SENT_REQUESTER,
    OUTBOX_STATUS_FAILED_PERMANENT,
    OUTBOX_STATUS_PENDING,
    OUTBOX_STATUS_SENT,
    NotificationService,
)
from libs.domain.purchase_lifecycle import PurchaseLifecycleService
from services.bot_api.telegram_notifications import render_telegram_notification
from services.bot_api.telegram_runtime import TelegramWebhookRuntime
from tests.e2e_harness import FakeBot, FakeTransport
from tests.helpers import create_account, create_listing, create_shop, create_user


def _build_runtime(database_url: str) -> TelegramWebhookRuntime:
    settings = BotApiSettings.model_validate(
        {
            "DATABASE_URL": database_url,
            "TOKEN_CIPHER_KEY": "test-key",
            "ADMIN_TELEGRAM_IDS": [9003],
            "DISPLAY_RUB_PER_USDT": "100",
        }
    )
    return TelegramWebhookRuntime(settings=settings)


def _render_notification(
    item: NotificationOutboxItem,
    *,
    display_rub_per_usdt: Decimal | None = None,
    tonapi_usdt_jetton_master: str = "jetton-master",
):
    return render_telegram_notification(
        item,
        tonapi_usdt_jetton_master=tonapi_usdt_jetton_master,
        display_rub_per_usdt=display_rub_per_usdt,
    )


class _RaisingBot:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def send_message(self, **_kwargs) -> None:
        raise self._exc


async def _enqueue_dispatchable_notification(
    db_pool,
    notification_service: NotificationService,
    *,
    dedupe_key: str,
    attempt_count: int = 0,
    telegram_id: int = 9301,
) -> None:
    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await notification_service.enqueue_locked(
                    cur,
                    recipient_telegram_id=telegram_id,
                    recipient_scope="buyer",
                    event_type=EVENT_ASSIGNMENT_RESERVATION_EXPIRED_BUYER,
                    dedupe_key=dedupe_key,
                    payload_json={
                        "assignment_id": 1,
                        "listing_id": 2,
                        "shop_id": 3,
                        "display_title": "Товар",
                        "shop_title": "Магазин",
                        "reward_usdt": "3.000000",
                    },
                )
                if attempt_count:
                    await cur.execute(
                        """
                        UPDATE notification_outbox
                        SET attempt_count = %s
                        WHERE dedupe_key = %s
                        """,
                        (attempt_count, dedupe_key),
                    )


async def _load_notification_state(db_pool, *, dedupe_key: str) -> dict:
    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT status, attempt_count, last_error
                FROM notification_outbox
                WHERE dedupe_key = %s
                """,
                (dedupe_key,),
            )
            row = await cur.fetchone()
            assert row is not None
            return dict(row)


def test_render_assignment_reservation_expired_notification_has_buyer_cta() -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=1,
            recipient_telegram_id=1,
            recipient_scope="buyer",
            event_type=EVENT_ASSIGNMENT_RESERVATION_EXPIRED_BUYER,
            dedupe_key="assignment:1:expired_2h:buyer",
            payload_json={
                "assignment_id": 1,
                "listing_id": 2,
                "shop_id": 3,
                "display_title": "Товар",
                "shop_title": "Магазин",
                "reward_usdt": "3.000000",
            },
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    assert "Бронь истекла" in rendered.text
    assert rendered.parse_mode == "HTML"
    assert rendered.buttons == (
        NotificationButton(text="📋 Покупки", flow="buyer", action="assignments"),
    )


def test_render_assignment_picked_up_buyer_review_required_links_to_review_instruction() -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=12,
            recipient_telegram_id=1,
            recipient_scope="buyer",
            event_type=EVENT_ASSIGNMENT_PICKED_UP_BUYER,
            dedupe_key="assignment:31:picked_up_wait_unlock:buyer",
            payload_json={
                "assignment_id": 31,
                "listing_id": 2,
                "shop_id": 3,
                "display_title": "Товар",
                "shop_title": "Магазин",
                "reward_usdt": "3.000000",
                "review_required": True,
                "unlock_at": "2026-06-05T17:58:00+00:00",
            },
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    assert "Выкуп подтвержден" in rendered.text
    assert (
        "Следующий шаг:</b> Оставьте отзыв на 5 звезд на сайте ВБ. "
        "Кэшбэк разблокируется после отзыва, но не раньше 05.06.2026 20:58 МСК."
    ) in rendered.text
    assert rendered.buttons == (
        NotificationButton(
            text="✍️ Оставить отзыв",
            flow="buyer",
            action="submit_review_payload_prompt",
            entity_id="31",
        ),
    )


def test_render_assignment_review_confirmed_seller_notification_contains_rating_and_text() -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=11,
            recipient_telegram_id=5,
            recipient_scope="seller",
            event_type=EVENT_ASSIGNMENT_REVIEW_CONFIRMED_SELLER,
            dedupe_key="assignment:1:review_confirmed:seller",
            payload_json={
                "assignment_id": 1,
                "listing_id": 2,
                "shop_id": 3,
                "display_title": "Товар",
                "shop_title": "Магазин",
                "reward_usdt": "3.000000",
                "rating": 5,
                "review_text": "Отлично",
                "reviewed_at": "2026-03-18T10:30:00+00:00",
            },
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    assert "Отзыв подтвержден" in rendered.text
    assert "Оценка:</b> 5 / 5" in rendered.text
    assert "Текст отзыва:</b> Отлично" in rendered.text
    assert rendered.buttons == (
        NotificationButton(text="📦 Объявления", flow="seller", action="listing_open", entity_id="2"),
    )


def test_render_seller_token_invalidated_notification_uses_neutral_unauthorized_reason() -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=2,
            recipient_telegram_id=5,
            recipient_scope="seller",
            event_type=EVENT_SELLER_TOKEN_INVALIDATED,
            dedupe_key="shop:2:token_invalidated:scrapper_401_unauthorized:1",
            payload_json={
                "shop_id": 2,
                "shop_title": "Shop Test",
                "paused_listings_count": 1,
                "source": "scrapper_401_unauthorized",
            },
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    assert "WB отклонил авторизацию токена" in rendered.text
    assert "WB отозвал токен" not in rendered.text
    assert rendered.buttons == (
        NotificationButton(text="🏪 Магазины", flow="seller", action="shop_open", entity_id="2"),
    )


@pytest.mark.parametrize(
    ("event_type", "payload_json", "expected_amount"),
    [
        (
            EVENT_ASSIGNMENT_REWARD_UNLOCKED_BUYER,
            {
                "assignment_id": 1,
                "listing_id": 2,
                "shop_id": 3,
                "display_title": "Товар",
                "shop_title": "Магазин",
                "reward_usdt": "2.538393",
            },
            "~254 ₽",
        ),
        (
            EVENT_ASSIGNMENT_EARLY_PAYOUT_LISTING_DELETE_BUYER,
            {
                "assignment_id": 1,
                "listing_id": 2,
                "shop_id": 3,
                "shop_title": "Магазин",
                "item_count": 2,
                "total_reward_usdt": "5.500000",
            },
            "~550 ₽",
        ),
        (
            EVENT_MANUAL_BALANCE_CREDIT_TARGET,
            {
                "recipient_role": "buyer",
                "amount_usdt": "3.000000",
            },
            "~300 ₽",
        ),
    ],
)
def test_render_buyer_notifications_show_approx_rub_amounts(
    event_type: str,
    payload_json: dict[str, object],
    expected_amount: str,
) -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=3,
            recipient_telegram_id=1,
            recipient_scope="buyer",
            event_type=event_type,
            dedupe_key=f"buyer:{event_type}",
            payload_json=payload_json,
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        ),
        display_rub_per_usdt=Decimal("100"),
    )

    assert expected_amount in rendered.text
    assert "USDT" not in rendered.text


def test_render_seller_reward_unlock_notification_keeps_exact_usdt_amount() -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=4,
            recipient_telegram_id=2,
            recipient_scope="seller",
            event_type=EVENT_ASSIGNMENT_REWARD_UNLOCKED_SELLER,
            dedupe_key="seller:reward_unlocked",
            payload_json={
                "assignment_id": 1,
                "listing_id": 2,
                "shop_id": 3,
                "display_title": "Товар",
                "shop_title": "Магазин",
                "reward_usdt": "2.538393",
            },
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        ),
        display_rub_per_usdt=Decimal("100"),
    )

    assert "2.538393 USDT" in rendered.text
    assert "~254 ₽" not in rendered.text


def test_render_withdraw_created_admin_notification_uses_full_detail_body() -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=5,
            recipient_telegram_id=9003,
            recipient_scope="admin",
            event_type=EVENT_WITHDRAW_CREATED_ADMIN,
            dedupe_key="withdrawal:3:created:admin:9003",
            payload_json={
                "withdrawal_request_id": 3,
                "requester_role": "buyer",
                "requester_telegram_id": 2120394,
                "requester_username": "tech_banker",
                "amount_usdt": "2.538393",
                "status": "withdraw_pending_admin",
                "payout_address": "UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH",
                "requested_at": "2026-04-03T22:17:00+00:00",
                "processed_at": None,
                "sent_at": None,
            },
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    assert "<b>Новая заявка на вывод</b> · <code>W3</code>" in rendered.text
    assert "<b>Роль:</b> Покупатель" in rendered.text
    assert "<b>Telegram:</b> 2120394 (@tech_banker)" in rendered.text
    assert "<b>Сумма:</b> <code>2.538393 USDT</code>" in rendered.text
    assert "<b>Статус:</b> 🟡 На проверке" in rendered.text
    assert "<b>Кошелек:</b> <code>UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH</code>" in rendered.text
    assert "<b>Создана:</b> 04.04.2026 01:17 МСК" in rendered.text
    assert "<b>Обработана:</b> -" in rendered.text
    assert "<b>Отправлена:</b> -" in rendered.text
    assert len(rendered.buttons) == 2
    assert rendered.buttons[0] == NotificationButton(
        text="🔎 Открыть W3",
        flow="admin",
        action="withdrawal_detail",
        entity_id="3",
    )
    assert rendered.buttons[1].text == "🔗 Подготовить USDT TON"
    assert rendered.buttons[1].url is not None
    assert rendered.buttons[1].url.startswith("ton://transfer/UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH?")
    assert "jetton=jetton-master" in rendered.buttons[1].url
    assert "amount=2538393" in rendered.buttons[1].url
    assert "text=QPI+withdrawal+W3" in rendered.buttons[1].url
    assert "#" not in rendered.text


def test_render_withdraw_sent_admin_notification_uses_copyable_details() -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=6,
            recipient_telegram_id=9003,
            recipient_scope="admin",
            event_type=EVENT_WITHDRAW_SENT_ADMIN,
            dedupe_key="withdrawal:3:sent:admin:9003",
            payload_json={
                "withdrawal_request_id": 3,
                "requester_role": "seller",
                "requester_telegram_id": 2120394,
                "requester_username": "seller",
                "amount_usdt": "2.538393",
                "status": "withdraw_sent",
                "payout_address": "UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH",
                "requested_at": "2026-04-03T22:17:00+00:00",
                "processed_at": "2026-04-03T22:20:00+00:00",
                "sent_at": "2026-04-03T22:20:00+00:00",
                "tx_hash": "0xabc",
            },
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    assert "<b>Вывод отправлен</b> · <code>W3</code>" in rendered.text
    assert "<b>Роль:</b> Продавец" in rendered.text
    assert "<b>Сумма:</b> <code>2.538393 USDT</code>" in rendered.text
    assert "<b>Кошелек:</b> <code>UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH</code>" in rendered.text
    assert "<b>Хэш перевода:</b> <code>0xabc</code>" in rendered.text
    assert rendered.buttons == (
        NotificationButton(
            text="🔎 Открыть W3",
            flow="admin",
            action="withdrawal_detail",
            entity_id="3",
        ),
    )


def test_runtime_notification_markup_supports_callback_and_url_buttons() -> None:
    runtime = _build_runtime("postgresql://user:pass@127.0.0.1:5432/qpi_test")
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=7,
            recipient_telegram_id=9003,
            recipient_scope="admin",
            event_type=EVENT_WITHDRAW_CREATED_ADMIN,
            dedupe_key="withdrawal:4:created:admin:9003",
            payload_json={
                "withdrawal_request_id": 4,
                "requester_role": "buyer",
                "requester_telegram_id": 2120394,
                "requester_username": "buyer",
                "amount_usdt": "1.200100",
                "status": "withdraw_pending_admin",
                "payout_address": "UQTEST",
                "requested_at": "2026-04-03T22:17:00+00:00",
                "processed_at": None,
                "sent_at": None,
            },
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    markup = runtime._notification_markup(rendered)

    assert markup is not None
    assert markup.inline_keyboard[0][0].callback_data == "v1:admin:withdrawal_detail:4"
    assert markup.inline_keyboard[1][0].url == rendered.buttons[1].url


def test_runtime_notification_markup_iterates_all_notification_buttons() -> None:
    runtime = _build_runtime("postgresql://user:pass@127.0.0.1:5432/qpi_test")
    rendered = RenderedTelegramNotification(
        text="test",
        parse_mode=None,
        buttons=(
            NotificationButton(text="Open", flow="admin", action="withdrawals_section"),
            NotificationButton(text="First URL", url="https://example.test/first"),
            NotificationButton(text="Second URL", url="https://example.test/second"),
        ),
    )

    markup = runtime._notification_markup(rendered)

    assert markup is not None
    assert markup.inline_keyboard[0][0].callback_data == "v1:admin:withdrawals_section:"
    assert markup.inline_keyboard[1][0].url == "https://example.test/first"
    assert markup.inline_keyboard[2][0].url == "https://example.test/second"


def test_render_telegram_notification_requires_configured_usdt_jetton_master() -> None:
    item = NotificationOutboxItem(
        notification_id=8,
        recipient_telegram_id=9003,
        recipient_scope="admin",
        event_type=EVENT_WITHDRAW_CREATED_ADMIN,
        dedupe_key="withdrawal:8:created:admin:9003",
        payload_json={
            "withdrawal_request_id": 8,
            "requester_role": "buyer",
            "requester_telegram_id": 2120394,
            "requester_username": "buyer",
            "amount_usdt": "1.200100",
            "status": "withdraw_pending_admin",
            "payout_address": "UQTEST",
            "requested_at": "2026-04-03T22:17:00+00:00",
            "processed_at": None,
            "sent_at": None,
        },
        status="pending",
        attempt_count=0,
        next_attempt_at=datetime.now(tz=UTC),
        last_error=None,
        sent_at=None,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )

    with pytest.raises(TypeError):
        render_telegram_notification(item)


@pytest.mark.parametrize(
    ("event_type", "payload_json", "expected_refs"),
    [
        (
            EVENT_DEPOSIT_MANUAL_REVIEW_ADMIN,
            {
                "chain_tx_id": 11,
                "deposit_intent_id": 22,
                "amount_usdt": "1.200100",
                "reason": "late_payment",
            },
            ["<code>TX11</code>", "<code>D22</code>"],
        ),
        (
            EVENT_DEPOSIT_EXPIRED_SELLER,
            {
                "deposit_intent_id": 22,
                "expected_amount_usdt": "1.200100",
            },
            ["<code>D22</code>"],
        ),
        (
            EVENT_DEPOSIT_CANCELLED_SELLER,
            {
                "deposit_intent_id": 22,
                "reason": "cancelled",
            },
            ["<code>D22</code>"],
        ),
        (
            EVENT_WITHDRAW_CANCELLED_ADMIN,
            {
                "withdrawal_request_id": 3,
                "requester_role": "buyer",
                "requester_telegram_id": 2120394,
                "requester_username": "tech_banker",
                "amount_usdt": "2.538393",
            },
            ["<code>W3</code>"],
        ),
        (
            EVENT_WITHDRAW_SENT_ADMIN,
            {
                "withdrawal_request_id": 3,
                "requester_role": "buyer",
                "requester_telegram_id": 2120394,
                "requester_username": "tech_banker",
                "amount_usdt": "2.538393",
                "payout_address": "UQ_TEST_ADDRESS",
                "sent_at": "2026-04-03T22:17:00+00:00",
                "tx_hash": "0xabc",
            },
            ["<code>W3</code>"],
        ),
        (
            EVENT_WITHDRAW_REJECTED_REQUESTER,
            {
                "withdrawal_request_id": 3,
                "requester_role": "buyer",
                "note": "bad address",
            },
            ["<code>W3</code>"],
        ),
        (
            EVENT_WITHDRAW_SENT_REQUESTER,
            {
                "withdrawal_request_id": 3,
                "requester_role": "seller",
                "tx_hash": "0xabc",
            },
            ["<code>W3</code>"],
        ),
    ],
)
def test_render_notifications_use_code_formatted_public_refs(
    event_type: str,
    payload_json: dict[str, object],
    expected_refs: list[str],
) -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=6,
            recipient_telegram_id=1,
            recipient_scope="admin",
            event_type=event_type,
            dedupe_key=f"render:{event_type}",
            payload_json=payload_json,
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    assert "#" not in rendered.text
    for expected_ref in expected_refs:
        assert expected_ref in rendered.text


async def _prepare_assignment(
    db_pool,
    *,
    seller_telegram_id: int,
    buyer_telegram_id: int,
    reward_usdt: Decimal,
    wb_product_id: int = 777,
) -> tuple[BuyerService, PurchaseLifecycleService, int, int, int, int, int]:
    buyer_service = BuyerService(db_pool)
    finance_service = FinanceService(db_pool)
    purchase_lifecycle = PurchaseLifecycleService(db_pool, finance_service=finance_service)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_user_id = await create_user(
                conn,
                telegram_id=seller_telegram_id,
                role="seller",
                username="seller_test",
            )
            shop_id = await create_shop(
                conn,
                seller_user_id=seller_user_id,
                slug="shop-test",
                title="Shop Test",
            )
            listing_id = await create_listing(
                conn,
                shop_id=shop_id,
                seller_user_id=seller_user_id,
                wb_product_id=wb_product_id,
                reward_usdt=reward_usdt,
                slot_count=1,
                available_slots=1,
                status="active",
                display_title="Тестовый товар",
            )
            seller_collateral_account_id = await create_account(
                conn,
                owner_user_id=seller_user_id,
                account_code=f"user:{seller_user_id}:seller_collateral",
                account_kind="seller_collateral",
                balance=reward_usdt,
            )
            reward_reserved_account_id = await create_account(
                conn,
                owner_user_id=None,
                account_code="system:reward_reserved",
                account_kind="reward_reserved",
                balance=Decimal("0.000000"),
            )
            buyer_user_id = await create_user(
                conn,
                telegram_id=buyer_telegram_id,
                role="buyer",
                username="buyer_test",
            )
            buyer_available_account_id = await create_account(
                conn,
                owner_user_id=buyer_user_id,
                account_code=f"user:{buyer_user_id}:buyer_available",
                account_kind="buyer_available",
                balance=Decimal("0.000000"),
            )

    reservation = await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer_user_id,
        listing_id=listing_id,
        idempotency_key=f"reserve:{buyer_user_id}:{listing_id}",
    )
    return (
        buyer_service,
        purchase_lifecycle,
        reservation.assignment_id,
        seller_collateral_account_id,
        reward_reserved_account_id,
        buyer_available_account_id,
        listing_id,
    )


@pytest.mark.asyncio
async def test_create_withdrawal_request_enqueues_admin_notification(db_pool) -> None:
    service = FinanceService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            buyer_id = await create_user(conn, telegram_id=9001, role="buyer", username="buyer")
            await create_user(conn, telegram_id=9003, role="admin", username="admin")
            buyer_available_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_available",
                account_kind="buyer_available",
                balance=Decimal("5.000000"),
            )
            buyer_pending_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_withdraw_pending",
                account_kind="buyer_withdraw_pending",
                balance=Decimal("0.000000"),
            )

    result = await service.create_withdrawal_request(
        requester_user_id=buyer_id,
        requester_role="buyer",
        from_account_id=buyer_available_account_id,
        pending_account_id=buyer_pending_account_id,
        amount_usdt=Decimal("5.000000"),
        payout_address="UQ_TEST_ADDRESS",
        idempotency_key="withdraw-create-notify",
    )

    assert result.created is True

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT recipient_telegram_id, event_type
                FROM notification_outbox
                ORDER BY id ASC
                """
            )
            rows = await cur.fetchall()

    assert rows == [
        {
            "recipient_telegram_id": 9003,
            "event_type": EVENT_WITHDRAW_CREATED_ADMIN,
        }
    ]


@pytest.mark.asyncio
async def test_complete_withdrawal_request_enqueues_requester_and_admin_notifications(db_pool) -> None:
    service = FinanceService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            buyer_id = await create_user(conn, telegram_id=9101, role="buyer", username="buyer")
            admin_id = await create_user(conn, telegram_id=9103, role="admin", username="admin")
            buyer_available_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_available",
                account_kind="buyer_available",
                balance=Decimal("5.000000"),
            )
            buyer_pending_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_withdraw_pending",
                account_kind="buyer_withdraw_pending",
                balance=Decimal("0.000000"),
            )
            system_payout_account_id = await create_account(
                conn,
                owner_user_id=None,
                account_code="system:payout",
                account_kind="system_payout",
                balance=Decimal("0.000000"),
            )

    request = await service.create_withdrawal_request(
        requester_user_id=buyer_id,
        requester_role="buyer",
        from_account_id=buyer_available_account_id,
        pending_account_id=buyer_pending_account_id,
        amount_usdt=Decimal("5.000000"),
        payout_address="UQ_TEST_ADDRESS",
        idempotency_key="withdraw-complete-notify-create",
    )
    load_count = 0
    original_load_context = service._notifications._load_withdraw_request_context_locked

    async def counted_load_context(cur, *, request_id: int):
        nonlocal load_count
        load_count += 1
        return await original_load_context(cur, request_id=request_id)

    service._notifications._load_withdraw_request_context_locked = counted_load_context

    completed = await service.complete_withdrawal_request(
        request_id=request.withdrawal_request_id,
        admin_user_id=admin_id,
        system_payout_account_id=system_payout_account_id,
        tx_hash="0xabc",
        idempotency_key="withdraw-complete-notify",
    )
    duplicate = await service.complete_withdrawal_request(
        request_id=request.withdrawal_request_id,
        admin_user_id=admin_id,
        system_payout_account_id=system_payout_account_id,
        tx_hash="0xabc",
        idempotency_key="withdraw-complete-notify-duplicate",
    )

    assert completed.changed is True
    assert duplicate.changed is False
    assert load_count == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT recipient_telegram_id, event_type
                FROM notification_outbox
                ORDER BY id ASC
                """
            )
            rows = await cur.fetchall()

    assert rows == [
        {"recipient_telegram_id": 9103, "event_type": EVENT_WITHDRAW_CREATED_ADMIN},
        {"recipient_telegram_id": 9101, "event_type": EVENT_WITHDRAW_SENT_REQUESTER},
        {"recipient_telegram_id": 9103, "event_type": EVENT_WITHDRAW_SENT_ADMIN},
    ]


@pytest.mark.asyncio
async def test_reservation_expiry_enqueues_buyer_only(db_pool) -> None:
    buyer_service, _, assignment_id, _, _, _, _ = await _prepare_assignment(
        db_pool,
        seller_telegram_id=9101,
        buyer_telegram_id=9102,
        reward_usdt=Decimal("4.000000"),
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE assignments
                    SET reservation_expires_at = %s
                    WHERE id = %s
                    """,
                    (datetime.now(tz=UTC) - timedelta(minutes=1), assignment_id),
                )

    result = await buyer_service.process_expired_reservations(batch_size=10)
    assert result.expired_count == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT recipient_scope, event_type
                FROM notification_outbox
                ORDER BY id ASC
                """
            )
            rows = await cur.fetchall()

    assert rows == [
        {
            "recipient_scope": "buyer",
            "event_type": EVENT_ASSIGNMENT_RESERVATION_EXPIRED_BUYER,
        }
    ]


@pytest.mark.asyncio
async def test_reward_unlock_enqueues_buyer_and_seller_notifications(db_pool) -> None:
    (
        _,
        purchase_lifecycle,
        assignment_id,
        _,
        _,
        _,
        _,
    ) = await _prepare_assignment(
        db_pool,
        seller_telegram_id=9201,
        buyer_telegram_id=9202,
        reward_usdt=Decimal("7.500000"),
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE assignments
                    SET status = 'picked_up_wait_unlock',
                        unlock_at = timezone('utc', now()) - interval '1 minute'
                    WHERE id = %s
                    """,
                    (assignment_id,),
                )

    result = await purchase_lifecycle.unlock_cashback(
        purchase_id=assignment_id,
        idempotency_seed="unlock-notify",
    )
    assert result.changed is True

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT recipient_scope, event_type
                FROM notification_outbox
                ORDER BY recipient_scope ASC
                """
            )
            rows = await cur.fetchall()

    assert rows == [
        {"recipient_scope": "buyer", "event_type": EVENT_ASSIGNMENT_REWARD_UNLOCKED_BUYER},
        {"recipient_scope": "seller", "event_type": EVENT_ASSIGNMENT_REWARD_UNLOCKED_SELLER},
    ]


@pytest.mark.asyncio
async def test_runtime_dispatch_sends_and_marks_notification_sent(db_pool, isolated_database: str) -> None:
    runtime = _build_runtime(isolated_database)
    runtime._notification_service = NotificationService(db_pool)
    transport = FakeTransport()
    bot = FakeBot(transport=transport)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                buyer_user_id = await create_user(
                    conn,
                    telegram_id=9301,
                    role="buyer",
                    username="buyer",
                )
                seller_user_id = await create_user(
                    conn,
                    telegram_id=9302,
                    role="seller",
                    username="seller",
                )
                shop_id = await create_shop(
                    conn,
                    seller_user_id=seller_user_id,
                    slug="dispatch-shop",
                    title="Dispatch",
                )
                listing_id = await create_listing(
                    conn,
                    shop_id=shop_id,
                    seller_user_id=seller_user_id,
                    wb_product_id=9303,
                    reward_usdt=Decimal("3.000000"),
                    slot_count=1,
                    available_slots=1,
                    status="active",
                    display_title="Товар для уведомления",
                )
                await cur.execute(
                    """
                    INSERT INTO assignments (
                        listing_id,
                        buyer_user_id,
                        task_uuid,
                        wb_product_id,
                        status,
                        reward_usdt,
                        reservation_expires_at,
                        idempotency_key
                    )
                    VALUES (%s, %s, %s, %s, 'reserved', %s, timezone('utc', now()), %s)
                    RETURNING id
                    """,
                    (
                        listing_id,
                        buyer_user_id,
                        "11111111-1111-4111-8111-000000000001",
                        9303,
                        Decimal("3.000000"),
                        "dispatch-assignment",
                    ),
                )
                assignment_id = int((await cur.fetchone())["id"])
                await runtime._notification_service.enqueue_assignment_reservation_expired_for_buyer_locked(
                    cur,
                    assignment_id=assignment_id,
                )

    await runtime._dispatch_notifications_once(bot=bot)

    bot_events = transport.find("bot_send")
    assert len(bot_events) == 1
    assert "Бронь истекла" in (bot_events[0].text or "")
    assert bot_events[0].parse_mode == "HTML"
    assert bot_events[0].reply_markup is not None

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT status
                FROM notification_outbox
                """
            )
            row = await cur.fetchone()

    assert row["status"] == OUTBOX_STATUS_SENT


def test_runtime_notification_error_classifier_marks_bad_request_permanent() -> None:
    assert TelegramWebhookRuntime._is_permanent_notification_error(BadRequest("bad button url")) is True
    assert TelegramWebhookRuntime._is_permanent_notification_error(NetworkError("Timed out")) is False


@pytest.mark.asyncio
async def test_runtime_dispatch_marks_bad_request_notification_failed_permanent(
    db_pool,
    isolated_database: str,
) -> None:
    runtime = _build_runtime(isolated_database)
    runtime._notification_service = NotificationService(db_pool)
    dedupe_key = "dispatch-bad-request"
    await _enqueue_dispatchable_notification(db_pool, runtime._notification_service, dedupe_key=dedupe_key)

    await runtime._dispatch_notifications_once(bot=_RaisingBot(BadRequest("bad button url")))

    row = await _load_notification_state(db_pool, dedupe_key=dedupe_key)
    assert row["status"] == OUTBOX_STATUS_FAILED_PERMANENT
    assert row["attempt_count"] == 1
    assert row["last_error"] == "bad button url"


@pytest.mark.asyncio
async def test_runtime_dispatch_retries_transient_notification_failure(
    db_pool,
    isolated_database: str,
) -> None:
    runtime = _build_runtime(isolated_database)
    runtime._notification_service = NotificationService(db_pool)
    dedupe_key = "dispatch-transient-retry"
    await _enqueue_dispatchable_notification(db_pool, runtime._notification_service, dedupe_key=dedupe_key)

    await runtime._dispatch_notifications_once(bot=_RaisingBot(NetworkError("Timed out")))

    row = await _load_notification_state(db_pool, dedupe_key=dedupe_key)
    assert row["status"] == OUTBOX_STATUS_PENDING
    assert row["attempt_count"] == 1
    assert row["last_error"] == "Timed out"


@pytest.mark.asyncio
async def test_runtime_dispatch_dead_letters_transient_notification_after_attempt_cap(
    db_pool,
    isolated_database: str,
) -> None:
    runtime = _build_runtime(isolated_database)
    runtime._notification_service = NotificationService(db_pool)
    dedupe_key = "dispatch-transient-exhausted"
    await _enqueue_dispatchable_notification(
        db_pool,
        runtime._notification_service,
        dedupe_key=dedupe_key,
        attempt_count=23,
    )

    await runtime._dispatch_notifications_once(bot=_RaisingBot(NetworkError("Timed out")))

    row = await _load_notification_state(db_pool, dedupe_key=dedupe_key)
    assert row["status"] == OUTBOX_STATUS_FAILED_PERMANENT
    assert row["attempt_count"] == 24
    assert row["last_error"] == "Timed out"


@pytest.mark.asyncio
async def test_runtime_dispatch_formats_buyer_reward_notification_in_rub(db_pool, isolated_database: str) -> None:
    runtime = _build_runtime(isolated_database)
    runtime._notification_service = NotificationService(db_pool)
    transport = FakeTransport()
    bot = FakeBot(transport=transport)

    (
        _,
        purchase_lifecycle,
        assignment_id,
        _,
        _,
        _,
        _,
    ) = await _prepare_assignment(
        db_pool,
        seller_telegram_id=9401,
        buyer_telegram_id=9402,
        reward_usdt=Decimal("2.538393"),
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE assignments
                    SET status = 'picked_up_wait_unlock',
                        unlock_at = timezone('utc', now()) - interval '1 minute'
                    WHERE id = %s
                    """,
                    (assignment_id,),
                )

    result = await purchase_lifecycle.unlock_cashback(
        purchase_id=assignment_id,
        idempotency_seed="unlock-notify-rub",
    )
    assert result.changed is True

    await runtime._dispatch_notifications_once(bot=bot)

    buyer_events = [event for event in transport.find("bot_send") if event.chat_id == 9402]
    assert len(buyer_events) == 1
    assert "Кэшбэк зачислен" in (buyer_events[0].text or "")
    assert "~254 ₽" in (buyer_events[0].text or "")
    assert "USDT" not in (buyer_events[0].text or "")

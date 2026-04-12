from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from libs.config.settings import BotApiSettings
from libs.domain.errors import InvalidStateError
from libs.integrations.wb import WbPingResult
from libs.integrations.wb_public import WbPublicApiError
from services.bot_api.telegram_runtime import TelegramWebhookRuntime
from tests.e2e_harness import TelegramRuntimeHarness

_TASK_UUID = "11111111-1111-4111-8111-111111111111"


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def _build_runtime(*, admin_ids: list[int] | None = None):
    settings = BotApiSettings.model_validate(
        {
            "DATABASE_URL": "postgresql://user:pass@127.0.0.1:5432/qpi_test",
            "TOKEN_CIPHER_KEY": "phase10-test-key",
            "ADMIN_TELEGRAM_IDS": admin_ids or [9001],
            "TELEGRAM_BOT_USERNAME": "qpilka_bot",
            "SUPPORT_BOT_USERNAME": "qpilka_support_bot",
            "DISPLAY_RUB_PER_USDT": "100",
            "SELLER_COLLATERAL_SHARD_KEY": "mvp-1",
            "SELLER_COLLATERAL_INVOICE_TTL_HOURS": 24,
        }
    )
    runtime = TelegramWebhookRuntime(settings=settings)

    seller_service = _ns(
        bootstrap_seller=AsyncMock(
            return_value=_ns(
                user_id=101,
                seller_available_account_id=301,
                seller_collateral_account_id=302,
                seller_withdraw_pending_account_id=303,
            )
        ),
        list_shops=AsyncMock(return_value=[]),
        list_listing_collateral_views=AsyncMock(return_value=[]),
        get_seller_balance_snapshot=AsyncMock(
            return_value=_ns(
                seller_available_usdt=Decimal("0.000000"),
                seller_collateral_usdt=Decimal("0.000000"),
                seller_withdraw_pending_usdt=Decimal("0.000000"),
            )
        ),
        create_shop=AsyncMock(return_value=_ns(shop_id=11, title="Тушенка", slug="shop_tushenka")),
        save_validated_shop_token=AsyncMock(return_value=None),
        get_shop=AsyncMock(
            return_value=_ns(shop_id=11, title="Тушенка", slug="shop_tushenka", wb_token_status="valid")
        ),
        get_validated_shop_token_ciphertext=AsyncMock(return_value="ciphertext"),
        rename_shop=AsyncMock(
            return_value=_ns(shop_id=11, title="Тушенка", slug="shop_tushenka", wb_token_status="valid")
        ),
        create_listing_draft=AsyncMock(
            return_value=_ns(
                listing_id=21,
                display_title="Бумага A4 для принтера",
                wb_product_id=552892532,
                wb_subject_name="Бумага офисная",
                wb_vendor_code="paper-001",
                wb_source_title="BRAUBERG Бумага A4 для принтера",
                wb_brand_name="BRAUBERG",
                wb_description="Белая бумага для офиса",
                wb_photo_url="https://example.com/photo.webp",
                wb_tech_sizes=["0"],
                wb_characteristics=[{"name": "Плотность", "value": "80 г/м2"}],
                review_phrases=["в размер", "не садятся после стирки"],
                reference_price_rub=400,
                reference_price_source="orders",
                search_phrase="бумага а4 для принтера",
                reward_usdt=Decimal("1.000000"),
                slot_count=5,
                collateral_required_usdt=Decimal("5.050000"),
                status="draft",
            )
        ),
        get_listing=AsyncMock(
            return_value=_ns(
                listing_id=21,
                shop_id=11,
                display_title="Бумага A4 для принтера",
                wb_product_id=552892532,
                wb_subject_name="Бумага офисная",
                wb_vendor_code="paper-001",
                wb_source_title="BRAUBERG Бумага A4 для принтера",
                wb_brand_name="BRAUBERG",
                wb_description="Белая бумага для офиса",
                wb_photo_url="https://example.com/photo.webp",
                wb_tech_sizes=["0"],
                wb_characteristics=[{"name": "Плотность", "value": "80 г/м2"}],
                review_phrases=["в размер", "не садятся после стирки"],
                reference_price_rub=400,
                reference_price_source="orders",
                reward_usdt=Decimal("1.000000"),
                slot_count=5,
                available_slots=5,
                collateral_required_usdt=Decimal("5.050000"),
                status="active",
                search_phrase="бумага а4 для принтера",
            )
        ),
        update_listing=AsyncMock(
            return_value=_ns(
                listing_id=21,
                shop_id=11,
                display_title="Бумага A4 для принтера Обновлено",
                wb_product_id=552892532,
                wb_subject_name="Бумага офисная",
                wb_vendor_code="paper-001",
                wb_source_title="BRAUBERG Бумага A4 для принтера",
                wb_brand_name="BRAUBERG",
                wb_description="Белая бумага для офиса",
                wb_photo_url="https://example.com/photo.webp",
                wb_tech_sizes=["0"],
                wb_characteristics=[{"name": "Плотность", "value": "80 г/м2"}],
                review_phrases=["в размер", "не садятся после стирки"],
                reference_price_rub=400,
                reference_price_source="orders",
                reward_usdt=Decimal("1.200000"),
                slot_count=6,
                available_slots=6,
                collateral_required_usdt=Decimal("7.272000"),
                status="active",
                search_phrase="бумага а4 для принтера обновлено",
            )
        ),
        activate_listing=AsyncMock(return_value=_ns(changed=True)),
        pause_listing=AsyncMock(return_value=_ns(changed=True)),
        unpause_listing=AsyncMock(return_value=_ns(changed=True)),
        get_listing_delete_preview=AsyncMock(
            return_value=_ns(
                listing_id=21,
                active_assignments_count=0,
                assignment_linked_reserved_usdt=Decimal("0.000000"),
                unassigned_collateral_usdt=Decimal("0.000000"),
            )
        ),
        delete_listing=AsyncMock(
            return_value=_ns(
                changed=True,
                assignment_transferred_usdt=Decimal("0.000000"),
                unassigned_collateral_returned_usdt=Decimal("0.000000"),
            )
        ),
        get_shop_delete_preview=AsyncMock(
            return_value=_ns(
                shop_id=11,
                active_listings_count=0,
                open_assignments_count=0,
                assignment_linked_reserved_usdt=Decimal("0.000000"),
                unassigned_collateral_usdt=Decimal("0.000000"),
            )
        ),
        delete_shop=AsyncMock(
            return_value=_ns(
                changed=True,
                assignment_transferred_usdt=Decimal("0.000000"),
                unassigned_collateral_returned_usdt=Decimal("0.000000"),
            )
        ),
    )

    buyer_service = _ns(
        bootstrap_buyer=AsyncMock(
            return_value=_ns(
                user_id=202,
                buyer_available_account_id=401,
                buyer_withdraw_pending_account_id=402,
            )
        ),
        resolve_shop_by_slug=AsyncMock(return_value=_ns(shop_id=11, title="Тушенка", slug="shop_tushenka")),
        list_active_listings_by_shop_slug=AsyncMock(
            return_value=[
                _ns(
                    listing_id=21,
                    wb_product_id=552892532,
                    display_title="Бумага A4 для принтера",
                    wb_source_title="BRAUBERG Бумага A4 для принтера",
                    wb_subject_name="Бумага офисная",
                    wb_brand_name="BRAUBERG",
                    wb_description="Белая бумага для офиса",
                    wb_photo_url="https://example.com/photo.webp",
                    wb_tech_sizes=["0"],
                    wb_characteristics=[{"name": "Плотность", "value": "80 г/м2"}],
                    reference_price_rub=400,
                    search_phrase="бумага а4 для принтера",
                    reward_usdt=Decimal("0.250000"),
                )
            ]
        ),
        touch_saved_shop=AsyncMock(return_value=None),
        list_saved_shops=AsyncMock(return_value=[]),
        resolve_saved_shop_for_buyer=AsyncMock(return_value=_ns(shop_id=11, title="Тушенка", slug="shop_tushenka")),
        remove_saved_shop=AsyncMock(return_value=_ns(changed=True)),
        reserve_listing_slot=AsyncMock(return_value=_ns(assignment_id=31, created=True, task_uuid=_TASK_UUID)),
        list_buyer_assignments=AsyncMock(
            return_value=[
                _ns(
                    assignment_id=31,
                    listing_id=21,
                    task_uuid=_TASK_UUID,
                    shop_slug="shop_tushenka",
                    shop_title="Тушенка",
                    status="reserved",
                    display_title="Бумага A4 для принтера",
                    wb_source_title="BRAUBERG Бумага A4 для принтера",
                    wb_subject_name="Бумага офисная",
                    wb_brand_name="BRAUBERG",
                    wb_description="Белая бумага для офиса",
                    wb_photo_url="https://example.com/photo.webp",
                    wb_tech_sizes=["0"],
                    wb_characteristics=[{"name": "Плотность", "value": "80 г/м2"}],
                    reference_price_rub=400,
                    reward_usdt=Decimal("0.250000"),
                    order_id=None,
                    search_phrase="бумага а4 для принтера",
                    wb_product_id=552892532,
                    review_phrases=[],
                    reservation_expires_at=datetime(2026, 3, 2, 14, 0, 0),
                )
            ]
        ),
        submit_purchase_payload=AsyncMock(
            return_value=_ns(
                assignment_id=31,
                changed=True,
                order_id="ORDER-1",
            )
        ),
        submit_review_payload=AsyncMock(
            return_value=_ns(
                assignment_id=31,
                changed=True,
                verification_status="verified_auto",
                verification_reason=None,
            )
        ),
        list_admin_pending_review_confirmations=AsyncMock(return_value=[]),
        admin_verify_review_payload=AsyncMock(
            return_value=_ns(
                assignment_id=31,
                changed=True,
                verification_status="verified_admin",
            )
        ),
        cancel_assignment_by_buyer=AsyncMock(return_value=_ns(changed=True)),
    )

    finance_service = _ns(
        get_buyer_balance_snapshot=AsyncMock(
            return_value=_ns(
                buyer_available_usdt=Decimal("5.000000"),
                buyer_withdraw_pending_usdt=Decimal("0.000000"),
            )
        ),
        get_active_buyer_withdrawal_request=AsyncMock(return_value=None),
        get_active_seller_withdrawal_request=AsyncMock(return_value=None),
        count_buyer_withdrawal_history=AsyncMock(return_value=0),
        list_buyer_withdrawal_history=AsyncMock(return_value=[]),
        list_seller_withdrawal_history=AsyncMock(return_value=[]),
        create_withdrawal_request=AsyncMock(
            return_value=_ns(
                withdrawal_request_id=77,
                amount_usdt=Decimal("5.000000"),
                created=True,
            )
        ),
        cancel_withdrawal_request=AsyncMock(return_value=_ns(changed=True)),
        list_pending_withdrawals=AsyncMock(
            return_value=[
                _ns(
                    withdrawal_request_id=77,
                    requester_user_id=202,
                    requester_role="buyer",
                    requester_telegram_id=777001,
                    requester_username="buyer1",
                    amount_usdt=Decimal("5.000000"),
                    payout_address="UQ-test-wallet",
                )
            ]
        ),
        get_withdrawal_request_detail=AsyncMock(
            return_value=_ns(
                withdrawal_request_id=77,
                requester_user_id=202,
                requester_role="buyer",
                requester_telegram_id=777001,
                requester_username="buyer1",
                amount_usdt=Decimal("5.000000"),
                status="withdraw_pending_admin",
                payout_address="UQ-test-wallet",
                requested_at=datetime(2026, 3, 2, 12, 0, 0),
                processed_at=None,
                sent_at=None,
                to_account_id=501,
                from_account_id=401,
                tx_hash=None,
                note=None,
            )
        ),
        count_processed_withdrawals=AsyncMock(return_value=0),
        list_processed_withdrawals=AsyncMock(return_value=[]),
        reject_withdrawal_request=AsyncMock(return_value=_ns(changed=True)),
        complete_withdrawal_request=AsyncMock(return_value=_ns(changed=True)),
        manual_deposit_credit=AsyncMock(return_value=_ns(created=True, ledger_entry_id=901)),
    )

    deposit_service = _ns(
        list_active_shards=AsyncMock(
            return_value=[
                _ns(
                    shard_id=1,
                    shard_key="mvp-1",
                    deposit_address="UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH",
                )
            ]
        ),
        create_seller_deposit_intent=AsyncMock(
            return_value=_ns(
                deposit_intent_id=91,
                deposit_address="UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH",
                expected_amount_usdt=Decimal("1.200100"),
            )
        ),
        list_seller_deposit_intents=AsyncMock(return_value=[]),
        list_admin_review_txs=AsyncMock(return_value=[]),
        list_admin_expired_intents=AsyncMock(return_value=[]),
        credit_intent_from_chain_tx=AsyncMock(return_value=_ns(changed=True, ledger_entry_id=801)),
        cancel_deposit_intent=AsyncMock(return_value=True),
    )

    runtime._seller_service = seller_service
    runtime._buyer_service = buyer_service
    runtime._finance_service = finance_service
    runtime._deposit_service = deposit_service
    runtime._wb_ping_client = _ns(
        validate_token=AsyncMock(return_value=WbPingResult(valid=True, status_code=200, message="ok"))
    )
    runtime._wb_public_client = _ns(
        fetch_product_snapshot=AsyncMock(
            return_value=_ns(
                wb_product_id=552892532,
                subject_name="Бумага офисная",
                vendor_code="paper-001",
                name="BRAUBERG Бумага A4 для принтера",
                brand="BRAUBERG",
                description="Белая бумага для офиса",
                photo_url="https://example.com/photo.webp",
                tech_sizes=["0"],
                characteristics=[{"name": "Плотность", "value": "80 г/м2"}],
            )
        ),
        lookup_buyer_price=AsyncMock(
            return_value=_ns(
                buyer_price_rub=400,
                seller_price_rub=425,
                spp_percent=3,
                observed_at=datetime(2026, 3, 2, 12, 0, 0),
                source="orders",
            )
        ),
    )
    runtime._load_shop_wb_token = AsyncMock(return_value="wb-valid")
    runtime._fx_rate_service = None
    runtime._load_seller_order_counters = AsyncMock(return_value={"awaiting_order": 0, "ordered": 0, "picked_up": 0})
    runtime._refresh_display_rub_per_usdt = AsyncMock(return_value=None)
    runtime._ensure_admin_user = AsyncMock(return_value=90011)
    runtime._ensure_system_payout_account_id = AsyncMock(return_value=701)
    runtime._payout_wallet_raw_form = "0:payout-wallet"
    runtime._tonapi_client = _ns(
        parse_address=AsyncMock(side_effect=lambda account_id: _ns(raw_form="0:dest-wallet")),
        get_jetton_account_history=AsyncMock(return_value=_ns(operations=[], next_from=None)),
    )

    return runtime, _ns(
        seller=seller_service,
        buyer=buyer_service,
        finance=finance_service,
        deposit=deposit_service,
        wb_public=runtime._wb_public_client,
    )


def _event_texts(events) -> list[str]:
    return [event.text or "" for event in events]


def _markup_labels(event) -> list[str]:
    markup = event.reply_markup
    if markup is None:
        return []
    return [button.text for row in markup.inline_keyboard for button in row]


def _markup_urls(event) -> list[str]:
    markup = event.reply_markup
    if markup is None:
        return []
    return [button.url for row in markup.inline_keyboard for button in row if getattr(button, "url", None)]


@pytest.mark.asyncio
async def test_phase10_e2e_seller_shop_create_token_first_flow() -> None:
    runtime, deps = _build_runtime()
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    start_events = await harness.start()
    assert any("Выберите роль:" in text for text in _event_texts(start_events))

    role_events = await harness.callback(flow="root", action="role", entity_id="seller")
    assert any("<b>Магазины:</b>" in text for text in _event_texts(role_events))
    assert any("<b>Объявления:</b>" in text for text in _event_texts(role_events))

    create_prompt_events = await harness.callback(flow="seller", action="shop_create_token_prompt")
    assert any("Шаг 1 из 2." in text for text in _event_texts(create_prompt_events))
    assert any("Контент, Статистика, Вопросы и отзывы" in text for text in _event_texts(create_prompt_events))
    assert any("Только для чтения" in text for text in _event_texts(create_prompt_events))
    assert all("➕ Создать магазин" not in _markup_labels(event) for event in create_prompt_events)

    token_events = await harness.text("wb_valid_token")
    assert any("Токен валиден." in text for text in _event_texts(token_events))
    assert any("Название увидят покупатели" in text for text in _event_texts(token_events))
    assert any(event.kind == "delete" for event in token_events)

    title_events = await harness.text("Тушенка для всех")
    assert any("Магазин «Тушенка» создан." in text for text in _event_texts(title_events))
    assert any(
        "Ссылка для покупателей:\nhttps://t.me/qpilka_bot?start=shop_shop_tushenka" in text
        for text in _event_texts(title_events)
    )

    deps.seller.create_shop.assert_awaited_once()
    deps.seller.save_validated_shop_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase10_e2e_seller_listing_create_and_activate_flow() -> None:
    runtime, deps = _build_runtime()
    deps.seller.list_shops = AsyncMock(
        return_value=[_ns(shop_id=11, title="Тушенка", slug="shop_tushenka", wb_token_status="valid")]
    )
    deps.seller.list_listing_collateral_views = AsyncMock(
        return_value=[
            _ns(
                listing_id=21,
                shop_id=11,
                display_title="Бумага A4 для принтера",
                reference_price_rub=400,
                wb_product_id=552892532,
                search_phrase="бумага а4 для принтера",
                status="active",
                reward_usdt=Decimal("1.000000"),
                available_slots=5,
                slot_count=5,
                in_progress_assignments_count=0,
                collateral_locked_usdt=Decimal("5.050000"),
                collateral_required_usdt=Decimal("5.050000"),
                reserved_slot_usdt=Decimal("0.000000"),
            )
        ]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    pick_events = await harness.callback(flow="seller", action="listing_create_pick_shop")
    assert any("Выберите магазин для нового объявления." in text for text in _event_texts(pick_events))

    prompt_events = await harness.callback(flow="seller", action="listing_create_prompt", entity_id="11")
    assert any("Создание объявления для магазина" in text for text in _event_texts(prompt_events))

    preview_events = await harness.text("552892532, 100, 5, бумага а4 для принтера, в размер, не садятся после стирки")
    assert any("Проверьте объявление" in text for text in _event_texts(preview_events))
    assert any("Название для покупателей:</b> Бумага A4 для принтера" in text for text in _event_texts(preview_events))
    assert any(event.kind == "reply_photo" for event in preview_events)
    assert any(event.photo == "https://example.com/photo.webp" for event in preview_events)
    assert any("✅ Сохранить текущее название" in _markup_labels(event) for event in preview_events)

    create_events = await harness.callback(flow="seller", action="listing_title_keep")
    assert any("Активировать объявление сейчас?" in text for text in _event_texts(create_events))

    activate_events = await harness.callback(flow="seller", action="listing_activate", entity_id="21")
    assert any("Объявление активно." in text for text in _event_texts(activate_events))

    deps.seller.create_listing_draft.assert_awaited_once()
    deps.seller.activate_listing.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase10_e2e_seller_listing_create_asks_manual_price_when_no_orders_found() -> None:
    runtime, deps = _build_runtime()
    runtime._wb_public_client.lookup_buyer_price = AsyncMock(return_value=None)
    deps.seller.list_shops = AsyncMock(
        return_value=[_ns(shop_id=11, title="Тушенка", slug="shop_tushenka", wb_token_status="valid")]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    await harness.callback(flow="seller", action="listing_create_prompt", entity_id="11")
    manual_price_events = await harness.text(
        "552892532, 100, 5, бумага а4 для принтера, в размер, не садятся после стирки"
    )
    manual_price_text = "\n".join(_event_texts(manual_price_events))
    assert "Введите текущую цену покупателя" in manual_price_text

    confirm_title_events = await harness.text("392")
    assert any("Цена покупателя:</b> 392 ₽" in text for text in _event_texts(confirm_title_events))
    assert any(event.kind == "reply_photo" for event in confirm_title_events)
    assert any(event.photo == "https://example.com/photo.webp" for event in confirm_title_events)

    created_events = await harness.callback(flow="seller", action="listing_title_keep")
    assert any("Активировать объявление сейчас?" in text for text in _event_texts(created_events))


@pytest.mark.asyncio
async def test_phase10_e2e_seller_listing_create_allows_explicit_title_edit() -> None:
    runtime, deps = _build_runtime()
    deps.seller.list_shops = AsyncMock(
        return_value=[_ns(shop_id=11, title="Тушенка", slug="shop_tushenka", wb_token_status="valid")]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    await harness.callback(flow="seller", action="listing_create_prompt", entity_id="11")
    preview_events = await harness.text("552892532, 100, 5, бумага а4 для принтера, в размер, не садятся после стирки")
    assert any("✏️ Изменить название" in _markup_labels(event) for event in preview_events)

    edit_prompt_events = await harness.callback(flow="seller", action="listing_title_edit_prompt")
    assert any("Отправьте новое название" in text for text in _event_texts(edit_prompt_events))

    renamed_review_events = await harness.text("Бумага для офиса")
    renamed_review_text = "\n".join(_event_texts(renamed_review_events))
    assert "Название для покупателей:</b> Бумага для офиса" in renamed_review_text

    await harness.callback(flow="seller", action="listing_title_keep")
    create_call = deps.seller.create_listing_draft.await_args.kwargs
    assert create_call["display_title"] == "Бумага для офиса"
    assert create_call["review_phrases"] == ["в размер", "не садятся после стирки"]


@pytest.mark.asyncio
async def test_phase10_e2e_seller_topup_and_transactions_flow() -> None:
    runtime, deps = _build_runtime()
    deps.deposit.list_seller_deposit_intents = AsyncMock(
        return_value=[
            _ns(
                expected_amount_usdt=Decimal("1.200100"),
                status="credited",
                created_at=datetime(2026, 3, 2, 12, 0, 0),
                expires_at=datetime(2026, 3, 3, 12, 0, 0),
                credited_amount_usdt=Decimal("1.200100"),
            ),
            _ns(
                expected_amount_usdt=Decimal("0.500200"),
                status="manual_review",
                created_at=datetime(2026, 3, 2, 11, 0, 0),
                expires_at=datetime(2026, 3, 3, 11, 0, 0),
                credited_amount_usdt=None,
            ),
        ]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    topup_prompt_events = await harness.callback(flow="seller", action="topup_prompt")
    assert any("Введите сумму пополнения в USDT" in text for text in _event_texts(topup_prompt_events))
    assert any("❓ Как перевести?" in _markup_labels(event) for event in topup_prompt_events)

    topup_create_events = await harness.text("1.2")
    assert any("Счет на пополнение создан" in text for text in _event_texts(topup_create_events))
    assert any("Сумма (должна полностью совпадать):" in text for text in _event_texts(topup_create_events))
    assert any("<code>1.2001 USDT</code>" in text for text in _event_texts(topup_create_events))
    assert any(
        "<code>UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH</code>" in text
        for text in _event_texts(topup_create_events)
    )
    assert any("👛 Открыть Телеграм Кошелек" in _markup_labels(event) for event in topup_create_events)
    assert any("🔗 Ссылка (другие кошельки)" in _markup_labels(event) for event in topup_create_events)
    assert any("❓ Как перевести?" in _markup_labels(event) for event in topup_create_events)
    wallet_urls = [url for event in topup_create_events for url in _markup_urls(event)]
    assert "https://t.me/wallet/start" in wallet_urls
    assert any(url.startswith("ton://transfer/") for url in wallet_urls)
    assert any("amount=1200100" in url for url in wallet_urls)

    topup_help_events = await harness.callback(flow="seller", action="topup_help")
    topup_help_text = "\n".join(_event_texts(topup_help_events))
    assert "Как перевести USDT" in topup_help_text
    assert 'href="https://help.ru.wallet.tg/article/60-znakomstvo-s-wallet"' in topup_help_text
    assert 'href="https://t.me/wallet"' in topup_help_text
    assert 'href="https://help.ru.wallet.tg/article/80-kak-kupit-kriptovalutu-na-p2p-markete"' in topup_help_text
    assert "Рекомендуем делать перевод на несколько объявлений сразу" in topup_help_text
    assert "1. Зайдите" in topup_help_text
    assert "2. Пополните" in topup_help_text
    assert "3. Выведите" in topup_help_text
    assert "Сеть TON." in topup_help_text

    history_events = await harness.callback(flow="seller", action="topup_history")
    history_text = "\n".join(_event_texts(history_events))
    assert "<b>Сумма:</b> 1.2001 USDT" in history_text
    assert "Перевод найден, но нужна проверка администратором." in history_text

    deps.deposit.create_seller_deposit_intent.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase10_e2e_seller_balance_shows_active_request_and_hides_new_actions() -> None:
    runtime, deps = _build_runtime()
    deps.seller.get_seller_balance_snapshot = AsyncMock(
        return_value=_ns(
            seller_available_usdt=Decimal("4.000000"),
            seller_collateral_usdt=Decimal("2.500000"),
            seller_withdraw_pending_usdt=Decimal("1.000000"),
        )
    )
    deps.finance.get_active_seller_withdrawal_request = AsyncMock(
        return_value=_ns(
            withdrawal_request_id=88,
            requester_user_id=101,
            requester_role="seller",
            amount_usdt=Decimal("1.000000"),
            status="withdraw_pending_admin",
            payout_address="UQ-seller-wallet",
            requested_at=datetime(2026, 3, 2, 12, 0, 0),
            processed_at=None,
            sent_at=None,
            note=None,
            tx_hash=None,
        )
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    events = await harness.callback(flow="seller", action="balance")
    text = "\n".join(_event_texts(events))
    labels = []
    for event in events:
        labels.extend(_markup_labels(event))

    assert "<b>Свободно для новых объявлений:</b> $4.0" in text
    assert "<b>Уже выделено под объявления:</b> $2.5" in text
    assert "<b>В процессе вывода:</b> $1.0" in text
    assert "<b>Активная заявка</b> · <code>W88</code>" in text
    assert "<b>Всего:</b>" not in text
    assert "💸 Вывести все доступное" not in labels
    assert "✍️ Указать сумму вручную" not in labels
    assert "🚫 Отменить заявку" in labels


@pytest.mark.asyncio
async def test_phase10_e2e_seller_can_cancel_pending_withdrawal() -> None:
    runtime, deps = _build_runtime()
    deps.finance.get_withdrawal_request_detail = AsyncMock(
        return_value=_ns(
            withdrawal_request_id=77,
            requester_user_id=101,
            requester_role="seller",
            requester_telegram_id=10001,
            requester_username="seller",
            amount_usdt=Decimal("1.000000"),
            status="withdraw_pending_admin",
            payout_address="UQ-seller-wallet",
            requested_at=datetime(2026, 3, 2, 12, 0, 0),
            processed_at=None,
            sent_at=None,
            to_account_id=303,
            from_account_id=301,
            tx_hash=None,
            note=None,
        )
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    prompt_events = await harness.callback(
        flow="seller",
        action="withdraw_cancel_prompt",
        entity_id="77",
    )
    assert any("Отмена вывода" in text for text in _event_texts(prompt_events))

    confirm_events = await harness.callback(
        flow="seller",
        action="withdraw_cancel_confirm",
        entity_id="77",
    )
    assert any(
        "Заявка на вывод отменена. Средства вернулись в доступный баланс продавца." in text
        for text in _event_texts(confirm_events)
    )
    deps.finance.cancel_withdrawal_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase10_e2e_seller_withdraw_amount_validates_early() -> None:
    runtime, deps = _build_runtime()
    deps.seller.get_seller_balance_snapshot = AsyncMock(
        return_value=_ns(
            seller_available_usdt=Decimal("1.000000"),
            seller_collateral_usdt=Decimal("5.050000"),
            seller_withdraw_pending_usdt=Decimal("0.000000"),
        )
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    await harness.callback(flow="seller", action="withdraw_prompt_amount")
    events = await harness.text("2.0")

    assert any("Сумма превышает доступный баланс." in text for text in _event_texts(events))
    deps.finance.create_withdrawal_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_phase10_e2e_seller_withdraw_request_submits_request() -> None:
    runtime, deps = _build_runtime(admin_ids=[9001])
    deps.seller.get_seller_balance_snapshot = AsyncMock(
        return_value=_ns(
            seller_available_usdt=Decimal("1.500000"),
            seller_collateral_usdt=Decimal("0.000000"),
            seller_withdraw_pending_usdt=Decimal("0.000000"),
        )
    )
    runtime._tonapi_client.parse_address = AsyncMock(return_value=_ns(raw_form="0:seller-wallet"))
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    await harness.callback(flow="seller", action="withdraw_prompt_amount")
    await harness.text("1.5")
    events = await harness.text("UQ-seller-wallet")

    assert any("Заявка на вывод создана." in text for text in _event_texts(events))
    create_call = deps.finance.create_withdrawal_request.await_args.kwargs
    assert create_call["requester_role"] == "seller"
    assert create_call["requester_user_id"] == 101


@pytest.mark.asyncio
async def test_phase10_e2e_seller_transactions_history_shows_topups_and_withdrawals() -> None:
    runtime, deps = _build_runtime()
    deps.deposit.list_seller_deposit_intents = AsyncMock(
        return_value=[
            _ns(
                deposit_intent_id=91,
                expected_amount_usdt=Decimal("1.200100"),
                status="credited",
                created_at=datetime(2026, 3, 2, 12, 0, 0),
                expires_at=datetime(2026, 3, 3, 12, 0, 0),
                credited_amount_usdt=Decimal("1.200100"),
            )
        ]
    )
    deps.finance.list_seller_withdrawal_history = AsyncMock(
        return_value=[
            _ns(
                withdrawal_request_id=88,
                amount_usdt=Decimal("2.000000"),
                status="rejected",
                payout_address="UQ-seller-wallet",
                requested_at=datetime(2026, 3, 2, 13, 0, 0),
                processed_at=datetime(2026, 3, 2, 13, 5, 0),
                sent_at=None,
                note="Неверный адрес",
                tx_hash=None,
            )
        ]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    events = await harness.callback(flow="seller", action="topup_history")
    text = "\n".join(_event_texts(events))

    assert "<b>Вывод</b> · <code>W88</code>" in text
    assert "<b>Статус:</b> 🔴 Отклонено" in text
    assert "<b>Комментарий:</b> Неверный адрес" in text
    assert "<b>Счет на пополнение</b> · <code>D91</code>" in text
    assert "<b>Зачислено:</b> 1.2001 USDT" in text


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_deeplink_reserve_submit_payload_flow() -> None:
    runtime, deps = _build_runtime()
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    deeplink_events = await harness.start(start_arg="shop_shop_tushenka")
    deeplink_text = "\n".join(_event_texts(deeplink_events))
    assert "Магазин «Тушенка»" in deeplink_text
    assert "Бумага A4 для принтера" in deeplink_text
    deeplink_labels = []
    for event in deeplink_events:
        deeplink_labels.extend(_markup_labels(event))
    assert "1" in deeplink_labels
    assert "✅ Купить" not in deeplink_labels
    assert "🔎 Просмотр" not in deeplink_labels

    detail_events = await harness.callback(
        flow="buyer",
        action="listing_open",
        entity_id="21",
    )
    assert any("✅ Купить" in _markup_labels(event) for event in detail_events)

    reserve_events = await harness.callback(
        flow="buyer",
        action="reserve",
        entity_id="21",
        query_id="reserve-1",
    )
    reserve_text = "\n".join(_event_texts(reserve_events))
    assert "Покупка создана" in reserve_text
    assert "Введите токен в " in reserve_text
    assert (
        '<a href="https://chromewebstore.google.com/detail/qpilka/joefinmgneknnaejambgbaclobeedaga">'
        "расширении для браузера Chrome / Яндекс Qpilka</a>"
    ) in reserve_text
    assert "до 02.03.2026 17:00 MSK (по истечении срока бронь отменится)." in reserve_text
    assert "<b>Срок заказа:</b>" not in reserve_text
    assert any("Ввести токен-подтверждение" in _markup_labels(event) for event in reserve_events)

    submit_prompt_events = await harness.callback(
        flow="buyer",
        action="submit_payload_prompt",
        entity_id="31",
    )
    assert any(
        "Вставьте токен из расширения следующим сообщением ниже." in text for text in _event_texts(submit_prompt_events)
    )

    payload_events = await harness.text("WyJPUkRFUi0xIiwiMjAyNi0wMy0wMlQxMjozMDowMCJd")
    assert any("Токен-подтверждение принят." in text for text in _event_texts(payload_events))
    assert any(event.kind == "delete" for event in payload_events)

    deps.buyer.reserve_listing_slot.assert_awaited_once()
    deps.buyer.submit_purchase_payload.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_review_prompt_and_submit_flow() -> None:
    runtime, deps = _build_runtime()
    deps.buyer.list_buyer_assignments = AsyncMock(
        return_value=[
            _ns(
                assignment_id=31,
                listing_id=21,
                task_uuid=_TASK_UUID,
                shop_slug="shop_tushenka",
                shop_title="Тушенка",
                status="picked_up_wait_review",
                display_title="Бумага A4 для принтера",
                wb_source_title="BRAUBERG Бумага A4 для принтера",
                wb_subject_name="Бумага офисная",
                wb_brand_name="BRAUBERG",
                wb_description="Белая бумага для офиса",
                wb_photo_url="https://example.com/photo.webp",
                wb_tech_sizes=["0"],
                wb_characteristics=[{"name": "Плотность", "value": "80 г/м2"}],
                reference_price_rub=400,
                reward_usdt=Decimal("0.250000"),
                order_id="ORDER-1",
                search_phrase="бумага а4 для принтера",
                wb_product_id=552892532,
                review_phrases=["в размер", "не садятся после стирки"],
                reservation_expires_at=datetime(2026, 3, 2, 14, 0, 0),
            )
        ]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    purchase_events = await harness.callback(flow="buyer", action="assignments")
    purchase_text = "\n".join(_event_texts(purchase_events))
    assert "Нужно оставить отзыв" in purchase_text
    assert any("✍️ Ввести токен отзыва" in _markup_labels(event) for event in purchase_events)

    review_prompt_events = await harness.callback(
        flow="buyer",
        action="submit_review_payload_prompt",
        entity_id="31",
    )
    assert any(
        "Вставьте токен из расширения следующим сообщением ниже." in text for text in _event_texts(review_prompt_events)
    )

    payload_events = await harness.text("WzU1Mjg5MjUzMiwiMjAyNi0wMy0xOFQxMDozMDowMFoiLDUsImdyZWF0Il0=")
    assert any(
        "Отзыв подтвержден. Ожидайте начисления кэшбэка через 15 дней после выкупа товара." in text
        for text in _event_texts(payload_events)
    )
    assert any(event.kind == "delete" for event in payload_events)

    deps.buyer.submit_review_payload.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_shops_screen_uses_numbered_shop_list() -> None:
    runtime, deps = _build_runtime()
    deps.buyer.list_saved_shops = AsyncMock(
        return_value=[
            _ns(
                shop_id=11,
                slug="shop_tushenka",
                title="Тушенка",
                active_listings_count=0,
            ),
            _ns(
                shop_id=12,
                slug="shop_empty",
                title="Пустой магазин",
                active_listings_count=0,
            ),
        ]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    events = await harness.callback(flow="buyer", action="shops")
    text = "\n".join(_event_texts(events))
    labels = []
    for event in events:
        labels.extend(_markup_labels(event))

    assert "<i>Выберите номер магазина.</i>" in text
    assert "1. 🔴 Тушенка (объявлений: 0)" in text
    assert "2. 🔴 Пустой магазин (объявлений: 0)" in text
    assert "Сохраненные магазины:" not in text
    assert "Открыть последний магазин" not in labels
    assert "Открыть магазин по коду" not in labels
    assert "1" in labels
    assert "2" in labels


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_support_button_is_inside_instruction() -> None:
    runtime, deps = _build_runtime()
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    dashboard_events = await harness.callback(flow="buyer", action="menu")
    dashboard_labels = []
    for event in dashboard_events:
        dashboard_labels.extend(_markup_labels(event))
    assert "🆘 Поддержка" not in dashboard_labels

    guide_events = await harness.callback(flow="buyer", action="kb_guide")
    guide_labels = []
    for event in guide_events:
        guide_labels.extend(_markup_labels(event))

    assert any("Инструкция покупателя" in text for text in _event_texts(guide_events))
    assert "🆘 Поддержка" in guide_labels


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_shop_screen_shows_purchases_button_when_no_other_listings() -> None:
    runtime, deps = _build_runtime()
    deps.buyer.list_active_listings_by_shop_slug = AsyncMock(return_value=[])
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    events = await harness.start(start_arg="shop_shop_tushenka")
    text = "\n".join(_event_texts(events))
    labels = []
    for event in events:
        labels.extend(_markup_labels(event))

    assert ("У вас уже есть активная покупка в этом магазине. Других объявлений здесь пока нет.") in text
    assert "📋 Покупки · 1" in labels


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_listing_open_shows_photo_and_detail_card() -> None:
    runtime, deps = _build_runtime()
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    await harness.start(start_arg="shop_shop_tushenka")
    detail_events = await harness.callback(flow="buyer", action="listing_open", entity_id="21")

    assert any(event.kind == "edit_markup" for event in detail_events)
    assert any(event.kind == "reply_photo" for event in detail_events)
    assert not any(event.kind == "edit" for event in detail_events)
    detail_text = "\n".join(_event_texts(detail_events))
    assert "Цена:</b> 400 ₽" in detail_text
    assert "Характеристики" in detail_text
    assert "Размеры:</b>" not in detail_text
    assert "Артикул WB:</b>" not in detail_text
    assert "Бренд:</b>" not in detail_text
    assert "Название WB:</b>" not in detail_text
    assert any("✅ Купить" in _markup_labels(event) for event in detail_events)


@pytest.mark.asyncio
async def test_phase10_e2e_seller_listing_open_shows_photo_and_detail_card() -> None:
    runtime, deps = _build_runtime()
    deps.seller.list_shops = AsyncMock(
        return_value=[_ns(shop_id=11, title="Тушенка", slug="shop_tushenka", wb_token_status="valid")]
    )
    deps.seller.list_listing_collateral_views = AsyncMock(
        return_value=[
            _ns(
                listing_id=21,
                shop_id=11,
                display_title="Бумага A4 для принтера",
                reference_price_rub=400,
                wb_photo_url="https://example.com/photo.webp",
                wb_product_id=552892532,
                search_phrase="бумага а4 для принтера",
                status="active",
                reward_usdt=Decimal("1.000000"),
                available_slots=5,
                slot_count=5,
                in_progress_assignments_count=0,
                collateral_locked_usdt=Decimal("5.050000"),
                collateral_required_usdt=Decimal("5.050000"),
                reserved_slot_usdt=Decimal("0.000000"),
            )
        ]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    detail_events = await harness.callback(flow="seller", action="listing_open", entity_id="21")

    assert any(event.kind == "edit_markup" for event in detail_events)
    assert any(event.kind == "reply_photo" for event in detail_events)
    assert not any(event.kind == "edit" for event in detail_events)
    assert all("✏️ Редактировать" not in _markup_labels(event) for event in detail_events)
    detail_text = "\n".join(_event_texts(detail_events))
    assert "🟢 Бумага A4 для принтера" in detail_text
    assert "План покупок / В процессе:</b> 5 / 0" in detail_text
    assert "Параметры" in detail_text
    assert "Ссылка на магазин:" in detail_text


@pytest.mark.asyncio
async def test_phase10_e2e_seller_draft_listing_uses_available_balance_for_activation() -> None:
    runtime, deps = _build_runtime()
    deps.seller.list_shops = AsyncMock(
        return_value=[_ns(shop_id=11, title="Тушенка", slug="shop_tushenka", wb_token_status="valid")]
    )
    deps.seller.get_seller_balance_snapshot = AsyncMock(
        return_value=_ns(
            seller_available_usdt=Decimal("5.050000"),
            seller_collateral_usdt=Decimal("0.000000"),
            seller_withdraw_pending_usdt=Decimal("0.000000"),
        )
    )
    deps.seller.get_listing = AsyncMock(
        return_value=_ns(
            listing_id=21,
            shop_id=11,
            display_title="Бумага A4 для принтера",
            wb_product_id=552892532,
            wb_subject_name="Бумага офисная",
            wb_vendor_code="paper-001",
            wb_source_title="BRAUBERG Бумага A4 для принтера",
            wb_brand_name="BRAUBERG",
            wb_description="Белая бумага для офиса",
            wb_photo_url="https://example.com/photo.webp",
            wb_tech_sizes=["0"],
            wb_characteristics=[{"name": "Плотность", "value": "80 г/м2"}],
            reference_price_rub=400,
            reference_price_source="orders",
            reward_usdt=Decimal("1.000000"),
            slot_count=5,
            available_slots=5,
            collateral_required_usdt=Decimal("5.050000"),
            status="draft",
            search_phrase="бумага а4 для принтера",
        )
    )
    deps.seller.list_listing_collateral_views = AsyncMock(
        return_value=[
            _ns(
                listing_id=21,
                shop_id=11,
                display_title="Бумага A4 для принтера",
                reference_price_rub=400,
                wb_photo_url="https://example.com/photo.webp",
                wb_product_id=552892532,
                search_phrase="бумага а4 для принтера",
                status="draft",
                reward_usdt=Decimal("1.000000"),
                available_slots=5,
                slot_count=5,
                in_progress_assignments_count=0,
                collateral_locked_usdt=Decimal("0.000000"),
                collateral_required_usdt=Decimal("5.050000"),
                reserved_slot_usdt=Decimal("0.000000"),
            )
        ]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    list_events = await harness.callback(flow="seller", action="listings")
    list_text = "\n".join(_event_texts(list_events))
    list_labels = {label for event in list_events for label in _markup_labels(event)}

    assert "(недостаточно средств)" not in list_text
    assert "🟢 $5.1 (~505 ₽)" in list_text
    assert "1" in list_labels

    detail_events = await harness.callback(flow="seller", action="listing_open", entity_id="21")
    detail_text = "\n".join(_event_texts(detail_events))
    detail_labels = {label for event in detail_events for label in _markup_labels(event)}

    assert "(недостаточно средств)" not in detail_text
    assert "✅ Активировать" in detail_labels
    assert "⛔ Недостаточно средств" not in detail_labels


@pytest.mark.asyncio
async def test_phase10_e2e_seller_listings_are_numbered_and_paginated() -> None:
    runtime, deps = _build_runtime()
    deps.seller.list_shops = AsyncMock(
        return_value=[_ns(shop_id=11, title="Тушенка", slug="shop_tushenka", wb_token_status="valid")]
    )
    deps.seller.list_listing_collateral_views = AsyncMock(
        return_value=[
            _ns(
                listing_id=100 + index,
                shop_id=11,
                display_title=f"Товар {index}",
                reference_price_rub=300 + index,
                wb_product_id=500000 + index,
                search_phrase=f"товар {index}",
                status="active",
                reward_usdt=Decimal("1.000000"),
                available_slots=5,
                slot_count=5,
                in_progress_assignments_count=0,
                collateral_locked_usdt=Decimal("5.050000"),
                collateral_required_usdt=Decimal("5.050000"),
                reserved_slot_usdt=Decimal("0.000000"),
            )
            for index in range(1, 13)
        ]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    page_one_events = await harness.callback(flow="seller", action="listings")
    page_one_text = "\n".join(_event_texts(page_one_events))
    page_one_labels = {label for event in page_one_events for label in _markup_labels(event)}

    assert "<b>1. Товар 1</b>" in page_one_text
    assert "<b>10. Товар 10</b>" in page_one_text
    assert "<b>11. Товар 11</b>" not in page_one_text
    assert {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10"} <= page_one_labels
    assert "➡️" in page_one_labels
    assert "📄 Карточка" not in page_one_labels

    page_two_events = await harness.callback(flow="seller", action="listings", entity_id="2")
    page_two_text = "\n".join(_event_texts(page_two_events))
    page_two_labels = {label for event in page_two_events for label in _markup_labels(event)}

    assert "<b>11. Товар 11</b>" in page_two_text
    assert "<b>12. Товар 12</b>" in page_two_text
    assert "⬅️" in page_two_labels
    assert {"11", "12"} <= page_two_labels


@pytest.mark.asyncio
async def test_phase10_e2e_seller_listing_edit_flow_is_disabled() -> None:
    runtime, deps = _build_runtime()
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    edit_events = await harness.callback(flow="seller", action="listing_edit", entity_id="21")
    assert any("Редактирование отключено" in text for text in _event_texts(edit_events))
    deps.seller.update_listing.assert_not_awaited()


@pytest.mark.asyncio
async def test_phase10_e2e_seller_activation_insufficient_funds_shows_topup_cta() -> None:
    runtime, deps = _build_runtime()
    deps.seller.activate_listing = AsyncMock(side_effect=Exception())  # placeholder
    from libs.domain.errors import InsufficientFundsError  # local import for test only

    deps.seller.activate_listing = AsyncMock(side_effect=InsufficientFundsError())

    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")
    events = await harness.callback(flow="seller", action="listing_activate", entity_id="21")

    assert any("Недостаточно средств для активации" in text for text in _event_texts(events))
    assert any("➕ Пополнить" in _markup_labels(event) for event in events)


@pytest.mark.asyncio
async def test_phase10_e2e_same_telegram_user_can_open_seller_and_buyer_dashboards() -> None:
    runtime, deps = _build_runtime()
    harness = TelegramRuntimeHarness(runtime, telegram_id=55501, username="dualmode")

    await harness.start()
    seller_events = await harness.callback(flow="root", action="role", entity_id="seller")
    buyer_events = await harness.callback(flow="root", action="role", entity_id="buyer")

    assert any("<b>Магазины:</b>" in text for text in _event_texts(seller_events))
    assert any("<b>Покупки:</b>" in text for text in _event_texts(buyer_events))
    assert any("<b>Баланс:</b> ~500 ₽" in text for text in _event_texts(buyer_events))
    assert not any("<b>На выводе:</b>" in text for text in _event_texts(buyer_events))
    deps.seller.bootstrap_seller.assert_awaited()
    deps.buyer.bootstrap_buyer.assert_awaited()


@pytest.mark.asyncio
async def test_phase10_e2e_listing_activation_is_blocked_when_product_card_is_unavailable() -> None:
    runtime, deps = _build_runtime()
    runtime._wb_public_client.fetch_product_snapshot = AsyncMock(
        side_effect=WbPublicApiError(status_code=404, message="not found")
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    blocked_events = await harness.callback(
        flow="seller",
        action="listing_activate",
        entity_id="21",
    )
    blocked_text = "\n".join(_event_texts(blocked_events))

    assert "недоступен на WB" in blocked_text
    deps.seller.activate_listing.assert_not_awaited()


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_cancel_task_flow() -> None:
    runtime, deps = _build_runtime()
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    prompt_events = await harness.callback(
        flow="buyer",
        action="assignment_cancel_prompt",
        entity_id="31",
    )
    assert any("Отмена покупки" in text for text in _event_texts(prompt_events))

    confirm_events = await harness.callback(
        flow="buyer",
        action="assignment_cancel_confirm",
        entity_id="31",
        query_id="cancel-1",
    )
    assert any(
        "Покупка отменена. Она снова доступна другим покупателям." in text for text in _event_texts(confirm_events)
    )
    deps.buyer.cancel_assignment_by_buyer.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_purchases_screen_uses_shop_title_and_hides_expired() -> None:
    runtime, deps = _build_runtime()
    deps.buyer.list_buyer_assignments = AsyncMock(
        return_value=[
            _ns(
                assignment_id=31,
                listing_id=21,
                task_uuid=_TASK_UUID,
                shop_slug="shop_tushenka",
                shop_title="Тушенка",
                status="reserved",
                display_title="Бумага A4 для принтера",
                wb_source_title="BRAUBERG Бумага A4 для принтера",
                wb_subject_name="Бумага офисная",
                wb_brand_name="BRAUBERG",
                wb_description="Белая бумага для офиса",
                wb_photo_url="https://example.com/photo.webp",
                wb_tech_sizes=["0"],
                wb_characteristics=[{"name": "Плотность", "value": "80 г/м2"}],
                reference_price_rub=400,
                reward_usdt=Decimal("0.250000"),
                order_id=None,
                search_phrase="бумага а4 для принтера",
                wb_product_id=552892532,
                reservation_expires_at=datetime(2026, 3, 2, 14, 0, 0),
            ),
            _ns(
                assignment_id=32,
                listing_id=22,
                shop_slug="shop_mug",
                shop_title="Термокружки",
                status="withdraw_sent",
                display_title="Термокружка",
                wb_source_title="Термокружка",
                wb_subject_name="Посуда",
                wb_brand_name=None,
                wb_description="Термокружка 400 мл",
                wb_photo_url=None,
                wb_tech_sizes=[],
                wb_characteristics=[],
                reference_price_rub=990,
                reward_usdt=Decimal("1.100000"),
                order_id="ORDER-2",
                search_phrase="термокружка",
                wb_product_id=552892533,
                reservation_expires_at=datetime(2026, 3, 2, 14, 0, 0),
            ),
            _ns(
                assignment_id=33,
                listing_id=23,
                shop_slug="shop_paid",
                shop_title="Выплаченные",
                status="withdraw_sent",
                display_title="Оплаченный товар",
                wb_source_title="Оплаченный товар",
                wb_subject_name="Тест",
                wb_brand_name=None,
                wb_description="",
                wb_photo_url=None,
                wb_tech_sizes=[],
                wb_characteristics=[],
                reference_price_rub=100,
                reward_usdt=Decimal("0.100000"),
                order_id="ORDER-PAID",
                search_phrase="тест",
                wb_product_id=552892534,
                reservation_expires_at=datetime(2026, 3, 2, 14, 0, 0),
            ),
        ]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    events = await harness.callback(flow="buyer", action="assignments")
    text = "\n".join(_event_texts(events))
    first_block = text.split("<b>Товар:</b> Термокружка", maxsplit=1)[0]

    assert "<b>📋 Покупки</b>" in text
    assert "<b>Магазин:</b> Тушенка" in text
    assert "<b>Магазин:</b> Термокружки" in text
    assert "<b>Магазин:</b> Выплаченные" in text
    assert "shop_tushenka" not in text
    assert text.count("<b>Номер заказа:</b>") == 2
    assert first_block.index("<b>Товар:</b> Бумага A4 для принтера") < first_block.index("<b>Магазин:</b> Тушенка")
    assert first_block.index("<b>Магазин:</b> Тушенка") < first_block.index("<b>Кэшбэк:</b>")
    assert first_block.index("<b>Кэшбэк:</b>") < first_block.index("<b>Статус:</b> 🔴 Ожидает заказа")
    assert "Введите токен в " in first_block
    assert "<b>Срок заказа:</b>" not in first_block
    assert "\n\n<b>Покупка</b> · <code>P32</code>" in text
    assert text.count("<b>Статус:</b> 🟢 Выплачен") >= 2


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_balance_hides_withdraw_actions_when_zero() -> None:
    runtime, deps = _build_runtime()
    deps.finance.get_buyer_balance_snapshot = AsyncMock(
        return_value=_ns(
            buyer_available_usdt=Decimal("0.000000"),
            buyer_withdraw_pending_usdt=Decimal("0.000000"),
        )
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    events = await harness.callback(flow="buyer", action="balance")
    labels = []
    for event in events:
        labels.extend(_markup_labels(event))
    text = "\n".join(_event_texts(events))

    assert "<b>Доступно для вывода:</b> ~0 ₽" in text
    assert "<b>В процессе вывода:</b> ~0 ₽" in text
    assert "💸 Вывести все доступное" not in labels
    assert "✍️ Указать сумму вручную" not in labels
    assert "🧾 Транзакции" in labels


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_balance_shows_active_request_and_cancel() -> None:
    runtime, deps = _build_runtime()
    deps.finance.get_buyer_balance_snapshot = AsyncMock(
        return_value=_ns(
            buyer_available_usdt=Decimal("4.000000"),
            buyer_withdraw_pending_usdt=Decimal("1.000000"),
        )
    )
    deps.finance.get_active_buyer_withdrawal_request = AsyncMock(
        return_value=_ns(
            withdrawal_request_id=77,
            amount_usdt=Decimal("1.000000"),
            status="withdraw_pending_admin",
            payout_address="UQ-test-wallet",
            requested_at=datetime(2026, 3, 2, 12, 0, 0),
            processed_at=None,
            sent_at=None,
            note=None,
            tx_hash=None,
        )
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    events = await harness.callback(flow="buyer", action="balance")
    text = "\n".join(_event_texts(events))
    labels = []
    for event in events:
        labels.extend(_markup_labels(event))

    assert "<b>Активная заявка</b> · <code>W77</code>" in text
    assert "UQ-test-wallet" in text
    assert "💸 Вывести все доступное" not in labels
    assert "✍️ Указать сумму вручную" not in labels
    assert "🚫 Отменить заявку" in labels


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_can_cancel_pending_withdrawal() -> None:
    runtime, deps = _build_runtime()
    deps.finance.get_active_buyer_withdrawal_request = AsyncMock(
        return_value=_ns(
            withdrawal_request_id=77,
            amount_usdt=Decimal("1.000000"),
            status="withdraw_pending_admin",
            payout_address="UQ-test-wallet",
            requested_at=datetime(2026, 3, 2, 12, 0, 0),
            processed_at=None,
            sent_at=None,
            note=None,
            tx_hash=None,
        )
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    prompt_events = await harness.callback(
        flow="buyer",
        action="withdraw_cancel_prompt",
        entity_id="77",
    )
    assert any("Отмена вывода" in text for text in _event_texts(prompt_events))

    confirm_events = await harness.callback(
        flow="buyer",
        action="withdraw_cancel_confirm",
        entity_id="77",
    )
    assert any(
        "Заявка на вывод отменена. Средства вернулись в доступный баланс." in text
        for text in _event_texts(confirm_events)
    )
    deps.finance.cancel_withdrawal_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_withdraw_amount_validates_early() -> None:
    runtime, deps = _build_runtime()
    deps.finance.get_buyer_balance_snapshot = AsyncMock(
        return_value=_ns(
            buyer_available_usdt=Decimal("1.000000"),
            buyer_withdraw_pending_usdt=Decimal("0.000000"),
        )
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    await harness.callback(flow="buyer", action="withdraw_prompt_amount")
    events = await harness.text("2.0")

    assert any("Сумма превышает доступный баланс." in text for text in _event_texts(events))
    deps.finance.create_withdrawal_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_withdraw_request_submits_request() -> None:
    runtime, deps = _build_runtime(admin_ids=[9001])
    deps.finance.get_buyer_balance_snapshot = AsyncMock(
        return_value=_ns(
            buyer_available_usdt=Decimal("1.500000"),
            buyer_withdraw_pending_usdt=Decimal("0.000000"),
        )
    )
    runtime._tonapi_client.parse_address = AsyncMock(return_value=_ns(raw_form="0:buyer-wallet"))
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    await harness.callback(flow="buyer", action="withdraw_prompt_amount")
    await harness.text("1.5")
    events = await harness.text("UQ-buyer-wallet")

    assert any("Заявка на вывод создана." in text for text in _event_texts(events))
    create_call = deps.finance.create_withdrawal_request.await_args.kwargs
    assert create_call["requester_role"] == "buyer"
    assert create_call["requester_user_id"] == 202


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_withdraw_request_handles_unexpected_create_failure() -> None:
    runtime, deps = _build_runtime()
    deps.finance.get_buyer_balance_snapshot = AsyncMock(
        return_value=_ns(
            buyer_available_usdt=Decimal("1.500000"),
            buyer_withdraw_pending_usdt=Decimal("0.000000"),
        )
    )
    deps.finance.create_withdrawal_request = AsyncMock(side_effect=RuntimeError("boom"))
    runtime._tonapi_client.parse_address = AsyncMock(return_value=_ns(raw_form="0:buyer-wallet"))
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    await harness.callback(flow="buyer", action="withdraw_prompt_amount")
    await harness.text("1.5")
    events = await harness.text("UQ-buyer-wallet")

    assert any(
        "Техническая ошибка при создании заявки на вывод. Баланс не изменен." in text for text in _event_texts(events)
    )


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_withdraw_history_shows_timestamps_and_note() -> None:
    runtime, deps = _build_runtime()
    deps.finance.count_buyer_withdrawal_history = AsyncMock(return_value=1)
    deps.finance.list_buyer_withdrawal_history = AsyncMock(
        return_value=[
            _ns(
                withdrawal_request_id=77,
                amount_usdt=Decimal("5.000000"),
                status="rejected",
                payout_address="UQ-test-wallet",
                requested_at=datetime(2026, 3, 2, 12, 0, 0),
                processed_at=datetime(2026, 3, 2, 12, 5, 0),
                sent_at=None,
                note="Неверный адрес",
                tx_hash=None,
            )
        ]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    events = await harness.callback(flow="buyer", action="withdraw_history")
    text = "\n".join(_event_texts(events))

    assert "<b>Вывод</b> · <code>W77</code>" in text
    assert "<b>Создана:</b> 02.03.2026 15:00 MSK" in text
    assert "<b>Обработана:</b> 02.03.2026 15:05 MSK" in text
    assert "<b>Комментарий:</b> Неверный адрес" in text
    assert "<b>Статус:</b> 🔴 Отклонено" in text


@pytest.mark.asyncio
async def test_phase10_e2e_buyer_cannot_reserve_already_purchased_item() -> None:
    runtime, deps = _build_runtime()
    deps.buyer.reserve_listing_slot = AsyncMock(side_effect=InvalidStateError("already purchased wb_product_id"))
    harness = TelegramRuntimeHarness(runtime, telegram_id=20001, username="buyer")

    events = await harness.callback(flow="buyer", action="reserve", entity_id="21")
    assert any("Этот товар уже был куплен с вашего аккаунта." in text for text in _event_texts(events))


@pytest.mark.asyncio
async def test_phase10_e2e_admin_withdrawal_flow() -> None:
    runtime, deps = _build_runtime(admin_ids=[9001])
    detail_pending = _ns(
        withdrawal_request_id=77,
        requester_user_id=501,
        requester_role="buyer",
        requester_telegram_id=777001,
        requester_username="buyer1",
        amount_usdt=Decimal("5.000000"),
        status="withdraw_pending_admin",
        payout_address="UQ-test-wallet",
        requested_at=datetime(2026, 3, 2, 12, 0, 0),
        processed_at=None,
        sent_at=None,
        to_account_id=501,
        from_account_id=401,
        tx_hash=None,
        note=None,
    )
    detail_sent = _ns(
        withdrawal_request_id=77,
        requester_user_id=501,
        requester_role="buyer",
        requester_telegram_id=777001,
        requester_username="buyer1",
        amount_usdt=Decimal("5.000000"),
        status="withdraw_sent",
        payout_address="UQ-test-wallet",
        requested_at=datetime(2026, 3, 2, 12, 0, 0),
        processed_at=datetime(2026, 3, 2, 12, 5, 0),
        sent_at=datetime(2026, 3, 2, 12, 15, 0),
        to_account_id=501,
        from_account_id=401,
        tx_hash="0xabc",
        note=None,
    )
    detail_calls = {"count": 0}

    def detail_side_effect(*, request_id: int):
        assert request_id == 77
        detail_calls["count"] += 1
        if detail_calls["count"] <= 2:
            return detail_pending
        return detail_sent

    deps.finance.get_withdrawal_request_detail = AsyncMock(side_effect=detail_side_effect)
    runtime._tonapi_client.parse_address = AsyncMock(return_value=_ns(raw_form="0:dest-wallet"))
    runtime._tonapi_client.get_jetton_account_history = AsyncMock(
        return_value=_ns(
            operations=[
                _ns(
                    transaction_hash="0xabc",
                    source_address="0:payout-wallet",
                    destination_address="0:dest-wallet",
                    amount_usdt=Decimal("5.000000"),
                )
            ],
            next_from=None,
        )
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=9001, username="admin")

    open_admin_events = await harness.callback(flow="root", action="role", entity_id="admin")
    assert any("<b>Выводы в очереди:</b>" in text for text in _event_texts(open_admin_events))

    detail_events = await harness.callback(flow="admin", action="withdrawal_detail", entity_id="77")
    assert any("<b>Заявка</b> · <code>W77</code>" in text for text in _event_texts(detail_events))
    assert all("<b>Код:</b>" not in text for text in _event_texts(detail_events))
    assert any("<b>Роль:</b> Покупатель" in text for text in _event_texts(detail_events))

    prompt_sent_events = await harness.callback(
        flow="admin",
        action="withdrawal_complete_prompt",
        entity_id="77",
    )
    assert any("Введите хэш перевода для заявки W77." in text for text in _event_texts(prompt_sent_events))

    sent_events = await harness.text("0xabc")
    sent_text = "\n".join(_event_texts(sent_events))
    assert "Хэш перевода:</b> 0xabc" in sent_text

    deps.finance.complete_withdrawal_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase10_e2e_admin_pending_withdrawals_labels_seller_requests() -> None:
    runtime, deps = _build_runtime(admin_ids=[9001])
    deps.finance.list_pending_withdrawals = AsyncMock(
        return_value=[
            _ns(
                withdrawal_request_id=88,
                requester_user_id=101,
                requester_role="seller",
                requester_telegram_id=10001,
                requester_username="seller",
                amount_usdt=Decimal("2.000000"),
                payout_address="UQ-seller-wallet",
                requested_at=datetime(2026, 3, 2, 12, 0, 0),
            )
        ]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=9001, username="admin")

    events = await harness.callback(flow="admin", action="withdrawals")
    text = "\n".join(_event_texts(events))

    assert "<b>Заявка</b> · <code>W88</code>" in text
    assert "Код: W88" not in text
    assert "Роль: Продавец" in text
    assert "Telegram: 10001 (@seller)" in text
    assert "UQ-seller-wallet" in text


@pytest.mark.asyncio
async def test_phase10_e2e_admin_processed_withdrawals_use_header_ref_without_code_row() -> None:
    runtime, deps = _build_runtime(admin_ids=[9001])
    deps.finance.count_processed_withdrawals = AsyncMock(return_value=1)
    deps.finance.list_processed_withdrawals = AsyncMock(
        return_value=[
            _ns(
                withdrawal_request_id=77,
                requester_role="buyer",
                requester_telegram_id=20001,
                requester_username="buyer",
                amount_usdt=Decimal("2.500000"),
                status="rejected",
                payout_address="UQ-test-wallet",
                requested_at=datetime(2026, 3, 2, 12, 0, 0),
                processed_at=datetime(2026, 3, 2, 12, 5, 0),
                sent_at=None,
                note="Неверный адрес",
                tx_hash=None,
            )
        ]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=9001, username="admin")

    events = await harness.callback(flow="admin", action="withdrawals_history")
    text = "\n".join(_event_texts(events))

    assert "<b>Заявка</b> · <code>W77</code>" in text
    assert "Код: W77" not in text
    assert "Комментарий: Неверный адрес" in text


@pytest.mark.asyncio
async def test_phase10_e2e_admin_withdrawal_completion_rejects_unknown_tx_hash() -> None:
    runtime, deps = _build_runtime(admin_ids=[9001])
    deps.finance.get_withdrawal_request_detail = AsyncMock(
        return_value=_ns(
            withdrawal_request_id=77,
            requester_user_id=501,
            requester_role="buyer",
            requester_telegram_id=777001,
            requester_username="buyer1",
            amount_usdt=Decimal("5.000000"),
            status="withdraw_pending_admin",
            payout_address="UQ-test-wallet",
            requested_at=datetime(2026, 3, 2, 12, 0, 0),
            processed_at=None,
            sent_at=None,
            to_account_id=501,
            from_account_id=401,
            tx_hash=None,
            note=None,
        )
    )
    runtime._tonapi_client.parse_address = AsyncMock(return_value=_ns(raw_form="0:dest-wallet"))
    runtime._tonapi_client.get_jetton_account_history = AsyncMock(return_value=_ns(operations=[], next_from=None))
    harness = TelegramRuntimeHarness(runtime, telegram_id=9001, username="admin")

    await harness.callback(flow="admin", action="withdrawal_complete_prompt", entity_id="77")
    events = await harness.text("0xmissing")

    assert any("Транзакция с таким хэшем пока не найдена" in text for text in _event_texts(events))
    deps.finance.complete_withdrawal_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_phase10_e2e_admin_deposit_exceptions_flow() -> None:
    runtime, deps = _build_runtime(admin_ids=[9001])
    deps.deposit.list_admin_review_txs = AsyncMock(
        return_value=[
            _ns(
                chain_tx_id=11,
                tx_hash="0xtx11",
                amount_usdt=Decimal("1.200100"),
                from_address="addr_from",
                to_address="addr_to",
                review_reason="amount_mismatch",
                suffix_code=123,
                matched_intent_id=22,
            )
        ]
    )
    deps.deposit.list_admin_expired_intents = AsyncMock(
        return_value=[
            _ns(
                deposit_intent_id=22,
                seller_telegram_id=10001,
                expected_amount_usdt=Decimal("1.200100"),
                suffix_code=123,
                expires_at=datetime(2026, 3, 1, 12, 0, 0),
            )
        ]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=9001, username="admin")

    section_events = await harness.callback(flow="admin", action="exceptions_section")
    assert any("⚠️ Пополнения, требующие проверки:" in text for text in _event_texts(section_events))

    attach_prompt = await harness.callback(flow="admin", action="deposit_attach_prompt")
    assert any("Введите: <код_транзакции> <код_счета>." in text for text in _event_texts(attach_prompt))

    attach_result = await harness.text("TX11 D22")
    assert any("Платеж привязан к счету и зачислен." in text for text in _event_texts(attach_result))

    cancel_prompt = await harness.callback(flow="admin", action="deposit_cancel_prompt")
    assert any("Введите: <код_счета> <причина>." in text for text in _event_texts(cancel_prompt))

    cancel_result = await harness.text("D22 late_payment")
    assert any("Счет D22 отменен." in text for text in _event_texts(cancel_result))

    deps.deposit.credit_intent_from_chain_tx.assert_awaited_once()
    deps.deposit.cancel_deposit_intent.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase10_e2e_admin_review_verification_flow() -> None:
    runtime, deps = _build_runtime(admin_ids=[9001])
    deps.buyer.list_admin_pending_review_confirmations = AsyncMock(
        return_value=[
            _ns(
                assignment_id=31,
                task_uuid=_TASK_UUID,
                listing_id=21,
                buyer_user_id=202,
                buyer_telegram_id=777001,
                buyer_username="buyer1",
                shop_title="Тушенка",
                display_title="Бумага A4 для принтера",
                wb_product_id=552892532,
                reviewed_at=datetime(2026, 3, 18, 10, 30, 0),
                rating=4,
                review_text="Очень понравились, в размер.",
                review_phrases=["в размер", "не садятся после стирки"],
                verification_reason="Нужна оценка 5 из 5.",
            )
        ]
    )
    harness = TelegramRuntimeHarness(runtime, telegram_id=9001, username="admin")

    section_events = await harness.callback(flow="admin", action="exceptions_section")
    section_text = "\n".join(_event_texts(section_events))
    assert "Отзывы, требующие проверки:" in section_text
    assert "Покупка P31" in section_text
    assert any("✅ Проверить отзыв · 1" in _markup_labels(event) for event in section_events)

    prompt_events = await harness.callback(flow="admin", action="review_verify_prompt")
    assert any("Введите: <код_покупки> <base64_review_token>." in text for text in _event_texts(prompt_events))

    result_events = await harness.text("P31 eyJ...==")
    assert any("Отзыв подтвержден вручную." in text for text in _event_texts(result_events))
    deps.buyer.admin_verify_review_payload.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase10_e2e_non_admin_blocked_from_admin_mode() -> None:
    runtime, _deps = _build_runtime(admin_ids=[9001])
    harness = TelegramRuntimeHarness(runtime, telegram_id=9002, username="user")

    events = await harness.callback(flow="root", action="role", entity_id="admin")
    assert any("Доступ запрещен: вы не администратор." in text for text in _event_texts(events))


@pytest.mark.asyncio
async def test_phase10_e2e_callback_without_message_returns_alert() -> None:
    runtime, _deps = _build_runtime()
    harness = TelegramRuntimeHarness(runtime, telegram_id=10001, username="seller")

    events = await harness.callback(
        flow="seller",
        action="menu",
        with_message=False,
    )
    assert any(
        event.kind == "callback_answer"
        and (event.text or "").startswith("⚠️ Не удалось обновить экран.")
        and event.show_alert is True
        for event in events
    )

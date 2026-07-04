from __future__ import annotations

import asyncio
import base64
import html
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from libs.domain.errors import (
    DomainError,
    DuplicateOrderError,
    InvalidStateError,
    NoSlotsAvailableError,
    NotFoundError,
    PayloadValidationError,
)
from libs.domain.public_refs import (
    build_support_deep_link,
    format_assignment_ref,
    format_listing_ref,
    format_shop_ref,
)
from libs.domain.purchase_tokens import decode_purchase_payload, decode_review_payload
from services.bot_api.buyer_listing_copy import (
    ACTIVE_PURCHASE_LISTING_NOTICE,
    ALREADY_PURCHASED_LISTING_NOTICE,
    repeat_purchase_listing_notice,
)
from services.bot_api.presentation import (
    button_label_with_count,
    buyer_listing_detail_html,
    entity_block_heading_with_ref,
    format_buyer_balance_amount,
    format_buyer_cashback_with_percent,
    format_datetime_msk,
    format_price_optional_rub,
    format_review_phrases_text,
    listing_display_title,
    normalize_review_phrases,
    numbered_page_buttons,
    page_nav_row,
    resolve_numbered_page,
    screen_text,
    status_badge,
    title_ref_suffix,
    withdrawal_history_block_html,
    withdrawal_request_block_html,
)
from services.bot_api.transport_effects import (
    ButtonSpec,
    ClearPrompt,
    DeleteSourceMessage,
    FlowResult,
    LogEvent,
    ReplaceText,
    ReplyPhoto,
    ReplyText,
    SetPrompt,
    SetUserData,
)

_ROLE_BUYER = "buyer"
_QPILKA_EXTENSION_URL = "https://chromewebstore.google.com/detail/qpilka/joefinmgneknnaejambgbaclobeedaga"
_BUYER_TASK_COMPANION_PRODUCTS = 1


class BuyerMarketplaceAdapter(Protocol):
    async def get_buyer_balance_snapshot(self, *, buyer_user_id: int) -> Any: ...

    async def get_active_buyer_withdrawal_request(self, *, buyer_user_id: int) -> Any | None: ...

    async def count_buyer_withdrawal_history(self, *, buyer_user_id: int) -> int: ...

    async def list_buyer_withdrawal_history(self, *, buyer_user_id: int, limit: int, offset: int) -> list[Any]: ...

    async def list_buyer_assignments(self, *, buyer_user_id: int) -> list[Any]: ...

    async def list_saved_shops(self, *, buyer_user_id: int, limit: int = 20) -> list[Any]: ...

    async def resolve_shop_by_slug(self, *, slug: str) -> Any: ...

    async def list_active_listings_by_shop_slug(
        self,
        *,
        slug: str,
        buyer_user_id: int | None = None,
    ) -> list[Any]: ...

    async def resolve_active_listing_deep_link(
        self,
        *,
        listing_id: int,
        buyer_user_id: int | None = None,
    ) -> Any: ...

    async def touch_saved_shop(self, *, buyer_user_id: int, shop_id: int) -> None: ...

    async def resolve_saved_shop_for_buyer(self, *, buyer_user_id: int, shop_id: int) -> Any: ...

    async def remove_saved_shop(self, *, buyer_user_id: int, shop_id: int) -> Any: ...

    async def reserve_listing_slot(
        self,
        *,
        buyer_user_id: int,
        listing_id: int,
        idempotency_key: str,
    ) -> Any: ...

    async def submit_purchase_payload(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int,
        payload_base64: str,
    ) -> Any: ...

    async def submit_purchase_payload_by_task_uuid(
        self,
        *,
        buyer_user_id: int,
        payload_base64: str,
    ) -> Any: ...

    async def submit_review_payload(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int,
        payload_base64: str,
    ) -> Any: ...

    async def submit_review_payload_by_task_uuid(
        self,
        *,
        buyer_user_id: int,
        payload_base64: str,
    ) -> Any: ...

    async def cancel_assignment_by_buyer(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int,
        idempotency_key: str,
    ) -> Any: ...


@dataclass(frozen=True)
class BuyerMarketplaceFlowConfig:
    display_rub_per_usdt: Decimal
    support_bot_username: str | None = None
    last_shop_slug_key: str = "last_buyer_shop_slug"


class BuyerMarketplaceFlow:
    def __init__(
        self,
        *,
        adapter: BuyerMarketplaceAdapter,
        config: BuyerMarketplaceFlowConfig,
    ) -> None:
        self._adapter = adapter
        self._config = config

    async def render_dashboard(self, *, buyer_user_id: int) -> FlowResult:
        assignments = _buyer_visible_assignments(
            await self._adapter.list_buyer_assignments(buyer_user_id=buyer_user_id)
        )
        saved_shops = await self._adapter.list_saved_shops(buyer_user_id=buyer_user_id, limit=1000)
        snapshot = await self._adapter.get_buyer_balance_snapshot(buyer_user_id=buyer_user_id)
        bucket_counts = {
            "awaiting_order": 0,
            "ordered": 0,
            "picked_up": 0,
        }
        for item in assignments:
            bucket = buyer_dashboard_status_bucket(item.status)
            if bucket is not None:
                bucket_counts[bucket] += 1
        buyer_balance_text = format_buyer_balance_amount(
            snapshot.buyer_available_usdt,
            display_rub_per_usdt=self._config.display_rub_per_usdt,
        )
        text = screen_text(
            title="Кабинет покупателя",
            lines=[
                (
                    "<b>Покупки:</b> "
                    f"ожидают заказа: {bucket_counts['awaiting_order']} · "
                    f"заказаны: {bucket_counts['ordered']} · "
                    f"выкуплены: {bucket_counts['picked_up']}"
                ),
                f"<b>Баланс:</b> {buyer_balance_text}",
            ],
            separate_blocks=True,
        )
        return FlowResult(
            effects=(
                ReplaceText(
                    text=text,
                    buttons=_buyer_menu_buttons(
                        shops_count=len(saved_shops),
                        purchases_count=len(assignments),
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    async def render_balance(self, *, buyer_user_id: int) -> FlowResult:
        snapshot, active_request = await asyncio.gather(
            self._adapter.get_buyer_balance_snapshot(buyer_user_id=buyer_user_id),
            self._adapter.get_active_buyer_withdrawal_request(buyer_user_id=buyer_user_id),
        )
        available_text = format_buyer_balance_amount(
            snapshot.buyer_available_usdt,
            display_rub_per_usdt=self._config.display_rub_per_usdt,
        )
        pending_text = format_buyer_balance_amount(
            snapshot.buyer_withdraw_pending_usdt,
            display_rub_per_usdt=self._config.display_rub_per_usdt,
        )
        lines = [
            f"<b>Доступно для вывода:</b> {available_text}",
            f"<b>В процессе вывода:</b> {pending_text}",
        ]
        if active_request is not None:
            lines.append(withdrawal_request_block_html(active_request))

        keyboard_rows: list[list[ButtonSpec]] = []
        if active_request is not None:
            keyboard_rows.append(
                [
                    _button(
                        "🚫 Отменить заявку",
                        action="withdraw_cancel_prompt",
                        entity_id=active_request.withdrawal_request_id,
                    )
                ]
            )
        elif snapshot.buyer_available_usdt > Decimal("0.000000"):
            keyboard_rows.extend(
                [
                    [_button("💸 Вывести все доступное", action="withdraw_full")],
                    [_button("✍️ Указать сумму вручную", action="withdraw_prompt_amount")],
                ]
            )
        keyboard_rows.extend(
            [
                [_button("🧾 Транзакции", action="withdraw_history")],
                [_button("↩️ Назад", action="menu")],
                [_knowledge_button(topic="balance")],
            ]
        )
        return FlowResult(
            effects=(
                ReplaceText(
                    text=screen_text(
                        title="Баланс покупателя",
                        lines=lines,
                        separate_blocks=True,
                    ),
                    buttons=_rows(keyboard_rows),
                    parse_mode="HTML",
                ),
            )
        )

    async def render_withdrawal_history(self, *, buyer_user_id: int, page: int = 1) -> FlowResult:
        total_items = await self._adapter.count_buyer_withdrawal_history(buyer_user_id=buyer_user_id)
        if total_items < 1:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text=screen_text(
                            title="Транзакции покупателя",
                            lines=["Транзакций пока нет."],
                            note="Когда появятся заявки на вывод, они будут видны здесь.",
                        ),
                        buttons=_rows(
                            [
                                [_button("↩️ Назад к балансу", action="balance")],
                                [_knowledge_button(topic="balance")],
                            ]
                        ),
                        parse_mode="HTML",
                    ),
                )
            )

        resolved_page, total_pages, start_index, end_index = resolve_numbered_page(
            total_items=total_items,
            requested_page=page,
            page_size=8,
        )
        history = await self._adapter.list_buyer_withdrawal_history(
            buyer_user_id=buyer_user_id,
            limit=end_index - start_index,
            offset=start_index,
        )
        lines = [withdrawal_history_block_html(item) for item in history]

        keyboard_rows: list[list[ButtonSpec]] = []
        nav_row = page_nav_row(
            flow=_ROLE_BUYER,
            page_action="withdraw_history",
            page=resolved_page,
            total_pages=total_pages,
            previous_label="<",
            next_label=">",
        )
        if nav_row:
            keyboard_rows.append(list(nav_row))
        keyboard_rows.extend(
            [
                [_button("↩️ Назад к балансу", action="balance")],
                [_knowledge_button(topic="balance")],
            ]
        )
        return FlowResult(
            effects=(
                ReplaceText(
                    text=screen_text(
                        title=(
                            f"Транзакции покупателя · стр. {resolved_page}/{total_pages}"
                            if total_pages > 1
                            else "Транзакции покупателя"
                        ),
                        lines=lines,
                        note=(
                            "Если вывод отклонен или задержан, проверьте статус "
                            "и при необходимости оформите новую заявку."
                        ),
                        separate_blocks=True,
                    ),
                    buttons=_rows(keyboard_rows),
                    parse_mode="HTML",
                ),
            )
        )

    def render_knowledge_screen(self, *, topic: str) -> FlowResult:
        if topic == "guide":
            text = screen_text(
                title="Инструкция покупателя",
                lines=[
                    (
                        "Купилка позволяет просто и безопасно покупать товары на Wildberries "
                        "и получать за это кэшбэк на криптокошелек."
                    ),
                    (
                        "<b>Как пользоваться ботом</b>\n"
                        "1. Установите расширение для браузера Chrome / Яндекс Qpilka "
                        "(обязательно):\n"
                        f'<a href="{_QPILKA_EXTENSION_URL}">{_QPILKA_EXTENSION_URL}</a>\n'
                        "2. Откройте ссылку на товар или магазин и проверьте карточку товара.\n"
                        "3. Нажмите «Купить» (произойдет бронирование товара) и скопируйте токен заявки на покупку.\n"
                        "4. Вставьте полученный токен в расширение браузера Qpilka и следуйте подсказкам "
                        "для совершения заказа товара. Важно это сделать в течение 4 часов, иначе "
                        "бронирование аннулируется.\n"
                        "5. Если вы все сделали правильно, то после заказа товара вы получите "
                        "токен-подтверждение. Отправьте его в бот.\n"
                        "6. Выкупите товар.\n"
                        "7. Отправьте (с использованием расширения для браузера) отзыв о товаре на 5 "
                        "звезд c упоминанием 1-2 характеристик (при наличии).\n"
                        "8. Дождитесь разблокировки кэшбэка через 15 дней после выкупа.\n"
                        "9. После начисления кэшбэка оформите вывод."
                    ),
                    (
                        "<b>FAQ</b>\n"
                        "1. <b>Где найти товар?</b>\n"
                        "Ссылки на конкретные товары публикуются в профильных телеграм группах.\n\n"
                        "2. <b>Что, если заказ не был сделан в течение 4 часов (бронь отменена)?</b>\n"
                        "Оформите новую покупку.\n\n"
                        "3. <b>Почему кэшбэк разблокируется только через 15 дней?</b>\n"
                        "В течение 14 дней покупатель может сделать возврат товара на маркетплейсе.\n\n"
                        "4. <b>Что будет, если я не выкуплю товар или верну его в течение 14 дней?</b>\n"
                        "Кэшбэк выплачен не будет.\n\n"
                        "5. <b>Где гарантия, что продавец выплатит кэшбэк?</b>\n"
                        "Сервис обеспечивает выплату: деньги замораживаются на счету продавца."
                    ),
                    (
                        "<b>Про суммы</b>\n"
                        "На экранах покупателя суммы обычно показаны как приблизительные значения в ₽. "
                        "Фактические расчеты и перевод в системе идут в USDT."
                    ),
                ],
                separate_blocks=True,
            )
            keyboard_rows = [
                [_knowledge_button(topic="shops"), _knowledge_button(topic="purchases")],
                [_knowledge_button(topic="balance")],
                [_button("↩️ Назад", action="menu")],
            ]
            support_button = self._support_button()
            if support_button is not None:
                keyboard_rows.append([support_button])
        elif topic == "shops":
            text = screen_text(
                title="Про магазины",
                lines=[
                    "Магазин — это подборка доступных объявлений одного продавца.",
                    (
                        "Магазины сохраняются в вашем профиле, и вы всегда можете к ним вернуться "
                        "позднее. Ссылка на товар откроет нужное объявление сразу, а ссылка на магазин "
                        "откроет общий каталог продавца."
                    ),
                    (
                        "<b>Полезно знать</b>\n"
                        "1. Зеленая точка означает, что в магазине есть доступные объявления.\n"
                        "2. Удалить магазин нельзя, пока в нем есть незавершенная покупка.\n"
                        "3. Если активных объявлений нет, но покупка уже идет, пользуйтесь разделом «Покупки»."
                    ),
                ],
                separate_blocks=True,
            )
            keyboard_rows = [
                [_knowledge_button(topic="guide"), _knowledge_button(topic="purchases")],
                [_knowledge_button(topic="balance")],
                [_button("↩️ К магазинам", action="shops")],
            ]
        elif topic == "purchases":
            text = screen_text(
                title="Про покупки",
                lines=[
                    "Покупка появляется после бронирования товара и проходит несколько статусов.",
                    (
                        "Для получения кэшбэка на покупку необходимо:\n"
                        "- оформить заказ (с использованием расширения для браузера)\n"
                        "- сделать выкуп\n"
                        "- отправить отзыв о товаре на 5 звезд с упоминанием 1-2 характеристик "
                        "(с использованием расширения для браузера)\n"
                        "- подождать 15 дней после выкупа для разблокировки кэшбэка"
                    ),
                    (
                        "<b>Полезно знать</b>\n"
                        "1. Токен-подтверждение нужно отправить в течение 4 часов после брони.\n"
                        "3. От покупки можно отказаться, пока она еще не подтверждена.\n"
                        "4. Один и тот же товар нельзя брать повторно с одного аккаунта."
                    ),
                ],
                separate_blocks=True,
            )
            keyboard_rows = [
                [_knowledge_button(topic="guide"), _knowledge_button(topic="shops")],
                [_knowledge_button(topic="balance")],
                [_button("↩️ К покупкам", action="assignments")],
            ]
        else:
            text = screen_text(
                title="Про баланс и вывод",
                lines=[
                    (
                        "На балансе покупателя отображается сумма, доступная к выводу, "
                        "а также сумма, ожидающая разблокировки кэшбэка."
                    ),
                    (
                        "Суммы обычно показываются как приблизительные значения в ₽ для удобства. "
                        "Точная сумма вывода и все операции внутри системы ведутся в USDT."
                    ),
                    (
                        "<b>Полезно знать</b>\n"
                        "1. Вывод доступен только после разблокировки покупки.\n"
                        "2. Может быть только одна активная заявка на вывод.\n"
                        "3. Для вывода потребуется адрес кошелька в валюте USDT в сети TON."
                    ),
                ],
                separate_blocks=True,
            )
            keyboard_rows = [
                [_knowledge_button(topic="guide"), _knowledge_button(topic="shops")],
                [_knowledge_button(topic="purchases")],
                [_button("↩️ К балансу", action="balance")],
            ]

        return FlowResult(effects=(ReplaceText(text=text, buttons=_rows(keyboard_rows), parse_mode="HTML"),))

    async def render_shops_section(
        self,
        *,
        buyer_user_id: int,
        page: int = 1,
        notice: str | None = None,
    ) -> FlowResult:
        lines: list[str] = []
        saved_shops = await self._adapter.list_saved_shops(buyer_user_id=buyer_user_id, limit=100)
        if notice:
            lines.append(html.escape(notice))
        if not saved_shops:
            lines.append("Сохраненных магазинов пока нет.")
            text = screen_text(
                title="Магазины",
                lines=lines,
                separate_blocks=True,
            )
            return FlowResult(
                effects=(
                    ReplaceText(
                        text=text,
                        buttons=_rows(
                            [
                                [_button("↩️ Назад", action="menu")],
                                [_knowledge_button(topic="shops")],
                            ]
                        ),
                        parse_mode="HTML",
                    ),
                )
            )

        resolved_page, total_pages, start_index, end_index = resolve_numbered_page(
            total_items=len(saved_shops),
            requested_page=page,
        )
        shops_page = saved_shops[start_index:end_index]
        for idx, shop in enumerate(shops_page, start=start_index + 1):
            badge = buyer_shop_activity_badge(shop.active_listings_count)
            title = html.escape(shop.title)
            lines.append(f"<b>{idx}. {badge} {title} (объявлений: {shop.active_listings_count})</b>")

        text = screen_text(
            title="Магазины",
            lines=lines,
            separate_blocks=True,
        )
        return FlowResult(
            effects=(
                ReplaceText(
                    text=text,
                    buttons=numbered_page_buttons(
                        flow=_ROLE_BUYER,
                        open_action="open_saved_shop",
                        page_action="shops",
                        item_ids=[shop.shop_id for shop in shops_page],
                        start_number=start_index + 1,
                        page=resolved_page,
                        total_pages=total_pages,
                        extra_rows=[
                            [_button("↩️ Назад", action="menu")],
                            [_knowledge_button(topic="shops")],
                        ],
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    def start_shop_slug_prompt(self) -> FlowResult:
        return FlowResult(
            effects=(
                SetPrompt(
                    role=_ROLE_BUYER,
                    prompt_type="buyer_shop_slug",
                    sensitive=False,
                    data={},
                ),
                ReplaceText(
                    text="Введите код магазина из ссылки.\nЭто часть после shop_ в ссылке.",
                    buttons=_rows([[_button("↩️ Назад к магазинам", action="shops")]]),
                    parse_mode=None,
                ),
            )
        )

    async def open_shop_page(
        self,
        *,
        buyer_user_id: int,
        last_shop_slug: str,
        page: int = 1,
    ) -> FlowResult:
        slug = last_shop_slug.strip()
        if not slug:
            return await self.render_shops_section(
                buyer_user_id=buyer_user_id,
                notice="Магазин не найден. Выберите его из списка заново.",
            )
        return await self.render_shop_catalog(
            slug=slug,
            buyer_user_id=buyer_user_id,
            replace=True,
            page=page,
        )

    async def open_last_shop(
        self,
        *,
        buyer_user_id: int,
        last_shop_slug: str,
    ) -> FlowResult:
        slug = last_shop_slug.strip()
        if not slug:
            saved_shops = await self._adapter.list_saved_shops(buyer_user_id=buyer_user_id, limit=1)
            if saved_shops:
                slug = saved_shops[0].slug
        if not slug:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Нет сохраненного магазина. Выберите магазин из списка.",
                        buttons=_rows([[_button("↩️ Назад к магазинам", action="shops")]]),
                        parse_mode=None,
                    ),
                )
            )
        return await self.render_shop_catalog(slug=slug, buyer_user_id=buyer_user_id, replace=True, page=1)

    async def open_saved_shop(self, *, buyer_user_id: int, shop_id: int | None) -> FlowResult:
        if shop_id is None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось открыть магазин. Попробуйте снова.",
                        buttons=_rows([[_button("↩️ Назад к магазинам", action="shops")]]),
                        parse_mode=None,
                    ),
                )
            )
        try:
            saved_shop = await self._adapter.resolve_saved_shop_for_buyer(
                buyer_user_id=buyer_user_id,
                shop_id=shop_id,
            )
        except (NotFoundError, ValueError):
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Этот магазин больше недоступен. Выберите другой магазин.",
                        buttons=_rows([[_button("↩️ Назад к магазинам", action="shops")]]),
                        parse_mode=None,
                    ),
                )
            )
        return await self.render_shop_catalog(
            slug=saved_shop.slug,
            buyer_user_id=buyer_user_id,
            replace=True,
            page=1,
        )

    async def remove_saved_shop(self, *, buyer_user_id: int, shop_id: int | None) -> FlowResult:
        if shop_id is None:
            return await self.render_shops_section(
                buyer_user_id=buyer_user_id,
                notice="Не удалось определить магазин. Выберите его заново.",
            )
        try:
            shop = await self._adapter.resolve_saved_shop_for_buyer(
                buyer_user_id=buyer_user_id,
                shop_id=shop_id,
            )
        except NotFoundError:
            return await self.render_shops_section(
                buyer_user_id=buyer_user_id,
                notice="Магазин уже удален из списка.",
            )

        try:
            result = await self._adapter.remove_saved_shop(buyer_user_id=buyer_user_id, shop_id=shop_id)
        except InvalidStateError:
            text = screen_text(
                title=f"Магазин «{html.escape(shop.title)}»",
                lines=["Удаление недоступно, пока в магазине есть незавершенная покупка."],
            )
            return FlowResult(
                effects=(
                    ReplaceText(
                        text=text,
                        buttons=_rows(
                            [
                                [_button("📋 Покупки", action="assignments")],
                                [_button("↩️ Назад к магазинам", action="shops")],
                            ]
                        ),
                        parse_mode="HTML",
                    ),
                )
            )

        if not result.changed:
            return await self.render_shops_section(
                buyer_user_id=buyer_user_id,
                notice="Магазин уже удален из списка.",
            )
        return await self.render_shops_section(
            buyer_user_id=buyer_user_id,
            notice=f"Магазин «{shop.title}» удален из списка.",
        )

    async def reserve_listing(
        self,
        *,
        buyer_user_id: int,
        listing_id: int | None,
        callback_query_id: str,
    ) -> FlowResult:
        if listing_id is None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось открыть выбранный товар. Попробуйте снова.",
                        buttons=_rows([[_button("↩️ Назад к магазинам", action="shops")]]),
                        parse_mode=None,
                    ),
                )
            )

        try:
            reservation = await self._adapter.reserve_listing_slot(
                buyer_user_id=buyer_user_id,
                listing_id=listing_id,
                idempotency_key=f"tg-reserve:{buyer_user_id}:{listing_id}:{callback_query_id}",
            )
        except NotFoundError:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Товар больше недоступен.",
                        buttons=_rows([[_button("↩️ Назад к магазинам", action="shops")]]),
                        parse_mode=None,
                    ),
                )
            )
        except NoSlotsAvailableError:
            assignments = _buyer_visible_assignments(
                await self._adapter.list_buyer_assignments(buyer_user_id=buyer_user_id)
            )
            active_same_listing = any(
                item.listing_id == listing_id
                and item.status not in {
                    "wb_invalid",
                    "returned_within_14d",
                    "delivery_expired",
                }
                for item in assignments
            )
            if active_same_listing:
                return _active_purchase_exists_result()
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Свободных покупок по этому товару нет. Попробуйте выбрать другой товар.",
                        buttons=_rows([[_button("↩️ Назад к магазинам", action="shops")]]),
                        parse_mode=None,
                    ),
                )
            )
        except InvalidStateError as exc:
            details = str(exc).strip().lower()
            if "already purchased" in details:
                return FlowResult(
                    effects=(
                        ReplaceText(
                            text=ALREADY_PURCHASED_LISTING_NOTICE,
                            buttons=_rows([[_button("↩️ Назад к магазинам", action="shops")]]),
                            parse_mode=None,
                        ),
                    )
                )
            if "already has assignment" in details:
                return _active_purchase_exists_result()
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось открыть покупку. Попробуйте снова.",
                        buttons=_rows([[_button("↩️ Назад к магазинам", action="shops")]]),
                        parse_mode=None,
                    ),
                )
            )

        assignments = _buyer_visible_assignments(
            await self._adapter.list_buyer_assignments(buyer_user_id=buyer_user_id)
        )
        assignment = next((item for item in assignments if item.assignment_id == reservation.assignment_id), None)
        if assignment is None:
            text = screen_text(
                title="Покупка создана",
                cta="Откройте раздел «📋 Покупки», чтобы продолжить.",
            )
        elif reservation.created:
            text = screen_text(
                title="Покупка создана",
                lines=[buyer_task_instruction_text(assignment)],
            )
        else:
            text = screen_text(
                title="Покупка уже активна",
                lines=[buyer_task_instruction_text(assignment)],
            )
        return FlowResult(
            effects=(
                LogEvent(
                    event_name="buyer_slot_reserved",
                    fields={
                        "listing_id": listing_id,
                        "listing_ref": format_listing_ref(listing_id),
                        "assignment_id": reservation.assignment_id,
                        "assignment_ref": format_assignment_ref(reservation.assignment_id),
                        "reservation_created": reservation.created,
                    },
                ),
                ReplaceText(
                    text=text,
                    buttons=_rows(
                        [
                            [
                                _button(
                                    "Ввести токен-подтверждение",
                                    action="submit_payload_prompt",
                                    entity_id=reservation.assignment_id,
                                )
                            ],
                            [
                                _button(
                                    "🚫 Отказаться от покупки",
                                    action="assignment_cancel_prompt",
                                    entity_id=reservation.assignment_id,
                                )
                            ],
                            [
                                _button(
                                    button_label_with_count("📋 Покупки", len(assignments)),
                                    action="assignments",
                                )
                            ],
                            [_button("↩️ Назад к магазинам", action="shops")],
                            [_knowledge_button(topic="purchases")],
                        ]
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    async def render_assignments(self, *, buyer_user_id: int) -> FlowResult:
        assignments = _buyer_visible_assignments(
            await self._adapter.list_buyer_assignments(buyer_user_id=buyer_user_id)
        )
        if not assignments:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text=screen_text(title="Покупки", lines=["У вас пока нет покупок."]),
                        buttons=_rows(
                            [
                                [_button("↩️ Назад", action="menu")],
                                [_knowledge_button(topic="purchases")],
                            ]
                        ),
                        parse_mode="HTML",
                    ),
                )
            )

        lines: list[str] = []
        keyboard_rows: list[list[ButtonSpec]] = []
        for item in assignments:
            display_title = listing_display_title(
                display_title=item.display_title,
                fallback=item.search_phrase,
            )
            shop_title = html.escape(_buyer_shop_title(item))
            cashback_text = format_buyer_cashback_with_percent(
                reward_usdt=item.reward_usdt,
                reference_price_rub=item.reference_price_rub,
                display_rub_per_usdt=self._config.display_rub_per_usdt,
            )
            block_lines = [
                entity_block_heading_with_ref(
                    label="Покупка",
                    ref=format_assignment_ref(item.assignment_id),
                ),
                f"<b>Товар:</b> {html.escape(display_title)}",
                f"<b>Магазин:</b> {shop_title}",
                f"<b>Кэшбэк:</b> {cashback_text}",
            ]
            if item.order_id:
                block_lines.append(f"<b>Номер заказа:</b> {html.escape(item.order_id)}")
            block_lines.append(f"<b>Статус:</b> {buyer_purchase_status_badge(item.status)}")
            if item.status == "reserved":
                block_lines.append(buyer_task_instruction_text(item, include_title=False))
                keyboard_rows.append(
                    [
                        _button(
                            "Ввести токен-подтверждение",
                            action="submit_payload_prompt",
                            entity_id=item.assignment_id,
                        )
                    ]
                )
                keyboard_rows.append(
                    [
                        _button(
                            "🚫 Отказаться от покупки",
                            action="assignment_cancel_prompt",
                            entity_id=item.assignment_id,
                        )
                    ]
                )
            elif item.status == "picked_up_wait_review":
                if getattr(item, "review_verification_status", None) == "pending_manual":
                    reason = str(getattr(item, "review_verification_reason", "") or "").strip()
                    if reason:
                        block_lines.append(
                            "<b>Проверка отзыва:</b> "
                            + html.escape(reason)
                            + " Исправьте отзыв или напишите в поддержку со скриншотом."
                        )
                    else:
                        block_lines.append(
                            "<b>Проверка отзыва:</b> "
                            "Автоматическая проверка не пройдена. "
                            "Исправьте отзыв или напишите в поддержку со скриншотом."
                        )
                keyboard_rows.append(
                    [
                        _button(
                            "✍️ Оставить отзыв",
                            action="submit_review_payload_prompt",
                            entity_id=item.assignment_id,
                        )
                    ]
                )
            lines.append("\n".join(block_lines))
        keyboard_rows.extend(
            [
                [_button("↩️ Назад", action="menu")],
                [_knowledge_button(topic="purchases")],
            ]
        )
        return FlowResult(
            effects=(
                ReplaceText(
                    text=screen_text(
                        title="Покупки",
                        lines=lines,
                        separate_blocks=True,
                    ),
                    buttons=_rows(keyboard_rows),
                    parse_mode="HTML",
                ),
            )
        )

    def start_purchase_payload_prompt(self, *, assignment_id: int | None) -> FlowResult:
        if assignment_id is None:
            return _missing_assignment_result(text="Не удалось открыть покупку. Попробуйте снова.")
        return FlowResult(
            effects=(
                SetPrompt(
                    role=_ROLE_BUYER,
                    prompt_type="buyer_submit_payload",
                    sensitive=True,
                    data={"assignment_id": assignment_id},
                ),
                ReplaceText(
                    text=screen_text(
                        title="Токен-подтверждение",
                        cta="Вставьте токен из расширения следующим сообщением ниже.",
                    ),
                    buttons=_rows([[_button("↩️ Назад к покупкам", action="assignments")]]),
                    parse_mode="HTML",
                ),
            )
        )

    async def start_review_instruction(self, *, buyer_user_id: int, assignment_id: int | None) -> FlowResult:
        if assignment_id is None:
            return _missing_assignment_result(text="Не удалось открыть покупку. Попробуйте снова.")
        assignments = _buyer_visible_assignments(
            await self._adapter.list_buyer_assignments(buyer_user_id=buyer_user_id)
        )
        assignment = next((item for item in assignments if item.assignment_id == assignment_id), None)
        if assignment is None:
            return _missing_assignment_result(text="Покупка не найдена.")
        if assignment.status != "picked_up_wait_review":
            return _missing_assignment_result(text="Для этой покупки отзыв сейчас не требуется.")

        display_title = listing_display_title(
            display_title=assignment.display_title,
            fallback=assignment.search_phrase,
        )
        lines = [
            entity_block_heading_with_ref(
                label="Покупка",
                ref=format_assignment_ref(assignment.assignment_id),
            ),
            f"<b>Товар:</b> {html.escape(display_title)}",
            f"<b>Магазин:</b> {html.escape(_buyer_shop_title(assignment))}",
        ]
        if getattr(assignment, "order_id", None):
            lines.append(f"<b>Номер заказа:</b> {html.escape(assignment.order_id)}")
        lines.extend(
            [
                f"<b>Статус:</b> {buyer_purchase_status_badge(assignment.status)}",
                buyer_review_instruction_text(assignment, include_title=False),
            ]
        )
        return FlowResult(
            effects=(
                ReplaceText(
                    text=screen_text(
                        title="Отзыв",
                        cta="Сначала опубликуйте отзыв на WB через расширение Qpilka.",
                        lines=lines,
                        separate_blocks=True,
                    ),
                    buttons=_rows(
                        [
                            [
                                _button(
                                    "✅ У меня есть токен подтверждения",
                                    action="submit_review_payload_input_prompt",
                                    entity_id=assignment.assignment_id,
                                )
                            ],
                            [_button("↩️ Назад к покупкам", action="assignments")],
                            [_knowledge_button(topic="purchases")],
                        ]
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    def start_review_payload_prompt(self, *, assignment_id: int | None) -> FlowResult:
        if assignment_id is None:
            return _missing_assignment_result(text="Не удалось открыть покупку. Попробуйте снова.")
        return FlowResult(
            effects=(
                SetPrompt(
                    role=_ROLE_BUYER,
                    prompt_type="buyer_submit_review_payload",
                    sensitive=True,
                    data={"assignment_id": assignment_id},
                ),
                ReplaceText(
                    text=screen_text(
                        title="Токен-подтверждение отзыва",
                        cta="Вставьте токен-подтверждение, который выдало расширение после публикации отзыва.",
                    ),
                    buttons=_rows([[_button("↩️ Назад к покупкам", action="assignments")]]),
                    parse_mode="HTML",
                ),
            )
        )

    async def start_assignment_cancel_prompt(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int | None,
    ) -> FlowResult:
        if assignment_id is None:
            return _missing_assignment_result(text="Не удалось открыть покупку. Попробуйте снова.")
        assignments = _buyer_visible_assignments(
            await self._adapter.list_buyer_assignments(buyer_user_id=buyer_user_id)
        )
        assignment = next((item for item in assignments if item.assignment_id == assignment_id), None)
        if assignment is None:
            return _missing_assignment_result(text="Покупка не найдена.")
        if assignment.status != "reserved":
            return _missing_assignment_result(text="Эту покупку уже нельзя отменить.")
        return FlowResult(
            effects=(
                ReplaceText(
                    text=screen_text(
                        title="Отмена покупки",
                        lines=["Бронь будет снята, а покупка снова станет доступна другим покупателям."],
                    ),
                    buttons=_rows(
                        [
                            [
                                _button(
                                    "✅ Отказаться от покупки",
                                    action="assignment_cancel_confirm",
                                    entity_id=assignment_id,
                                )
                            ],
                            [_button("↩️ Назад к покупкам", action="assignments")],
                        ]
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    async def confirm_assignment_cancel(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int | None,
        callback_query_id: str,
    ) -> FlowResult:
        if assignment_id is None:
            return _missing_assignment_result(text="Не удалось отменить покупку. Попробуйте снова.")
        try:
            result = await self._adapter.cancel_assignment_by_buyer(
                buyer_user_id=buyer_user_id,
                assignment_id=assignment_id,
                idempotency_key=f"tg-assignment-cancel:{buyer_user_id}:{assignment_id}:{callback_query_id}",
            )
        except NotFoundError:
            return _missing_assignment_result(text="Покупка не найдена.")
        except InvalidStateError:
            return _missing_assignment_result(text="Эту покупку уже нельзя отменить.")

        text = (
            "Покупка отменена. Она снова доступна другим покупателям."
            if result.changed
            else "Покупка уже была отменена ранее."
        )
        return FlowResult(
            effects=(
                ReplaceText(
                    text=text,
                    buttons=_rows(
                        [
                            [_button("📋 Покупки", action="assignments")],
                            [_button("↩️ К магазинам", action="shops")],
                        ]
                    ),
                    parse_mode=None,
                ),
            )
        )

    async def submit_purchase_payload(
        self,
        *,
        prompt_state: dict[str, Any],
        text: str,
        buyer_user_id: int,
        update_id: int,
    ) -> FlowResult:
        assignment_id = int(prompt_state.get("assignment_id", 0))
        if assignment_id < 1:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(text="Покупка не найдена. Откройте список покупок заново.", parse_mode=None),
                )
            )
        try:
            result = await self._adapter.submit_purchase_payload(
                buyer_user_id=buyer_user_id,
                assignment_id=assignment_id,
                payload_base64=text,
            )
        except NotFoundError:
            return FlowResult(effects=(ReplyText(text="Покупка не найдена.", parse_mode=None),))
        except PayloadValidationError as exc:
            return FlowResult(effects=(ReplyText(text=_purchase_payload_validation_text(exc), parse_mode=None),))
        except DuplicateOrderError:
            return FlowResult(
                effects=(ReplyText(text="Этот номер заказа уже использован в другой покупке.", parse_mode=None),)
            )
        except InvalidStateError:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Сейчас нельзя отправить токен-подтверждение для этой покупки.",
                        parse_mode=None,
                    ),
                )
            )

        if result.changed:
            reply = (
                "Токен-подтверждение принят.\n"
                f"Номер заказа: {result.order_id}\n"
                "Дальше мы автоматически проверим выкуп и начисление кэшбэка."
            )
        else:
            reply = f"Этот токен-подтверждение уже отправлен ранее.\nНомер заказа: {result.order_id}"
        return FlowResult(
            effects=(
                ClearPrompt(),
                LogEvent(
                    event_name="buyer_payload_submitted",
                    fields={
                        "telegram_update_id": update_id,
                        "assignment_id": result.assignment_id,
                        "assignment_ref": format_assignment_ref(result.assignment_id),
                        "changed": result.changed,
                    },
                ),
                ReplyText(text=reply, buttons=_buyer_menu_buttons(), parse_mode=None),
            )
        )

    async def submit_direct_purchase_payload(
        self,
        *,
        text: str,
        buyer_user_id: int,
        update_id: int,
    ) -> FlowResult:
        try:
            result = await self._adapter.submit_purchase_payload_by_task_uuid(
                buyer_user_id=buyer_user_id,
                payload_base64=text,
            )
        except NotFoundError:
            return _direct_purchase_payload_rejected_result(
                update_id=update_id,
                reason="not_found",
                text=(
                    "Токен-подтверждение не принят.\n"
                    "Похоже, токен относится к другой покупке или устарел."
                ),
            )
        except PayloadValidationError as exc:
            return _direct_purchase_payload_rejected_result(
                update_id=update_id,
                reason="payload_validation_error",
                text=_purchase_payload_validation_text(exc),
            )
        except DuplicateOrderError:
            return _direct_purchase_payload_rejected_result(
                update_id=update_id,
                reason="duplicate_order",
                text="Этот номер заказа уже использован в другой покупке.",
            )
        except InvalidStateError:
            return _direct_purchase_payload_rejected_result(
                update_id=update_id,
                reason="invalid_state",
                text="Сейчас нельзя отправить токен-подтверждение для этой покупки.",
            )

        if result.changed:
            reply = (
                "Токен-подтверждение принят.\n"
                f"Номер заказа: {result.order_id}\n"
                "Дальше мы автоматически проверим выкуп и начисление кэшбэка."
            )
        else:
            reply = f"Этот токен-подтверждение уже отправлен ранее.\nНомер заказа: {result.order_id}"
        return FlowResult(
            effects=(
                DeleteSourceMessage(),
                LogEvent(
                    event_name="buyer_payload_submitted",
                    fields={
                        "telegram_update_id": update_id,
                        "assignment_id": result.assignment_id,
                        "assignment_ref": format_assignment_ref(result.assignment_id),
                        "changed": result.changed,
                        "direct_paste": True,
                    },
                ),
                ReplyText(text=reply, buttons=_buyer_menu_buttons(), parse_mode=None),
            )
        )

    async def submit_review_payload(
        self,
        *,
        prompt_state: dict[str, Any],
        text: str,
        buyer_user_id: int,
        update_id: int,
    ) -> FlowResult:
        assignment_id = int(prompt_state.get("assignment_id", 0))
        if assignment_id < 1:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(text="Покупка не найдена. Откройте список покупок заново.", parse_mode=None),
                )
            )
        try:
            result = await self._adapter.submit_review_payload(
                buyer_user_id=buyer_user_id,
                assignment_id=assignment_id,
                payload_base64=text,
            )
        except NotFoundError:
            return FlowResult(effects=(ReplyText(text="Покупка не найдена.", parse_mode=None),))
        except PayloadValidationError as exc:
            return FlowResult(effects=(ReplyText(text=_review_payload_validation_text(exc), parse_mode=None),))
        except InvalidStateError:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Сейчас нельзя отправить токен-подтверждение отзыва для этой покупки.",
                        parse_mode=None,
                    ),
                )
            )

        reply, buttons = self._review_payload_reply_and_buttons(result)
        return FlowResult(
            effects=(
                ClearPrompt(),
                LogEvent(
                    event_name="buyer_review_payload_submitted",
                    fields={
                        "telegram_update_id": update_id,
                        "assignment_id": result.assignment_id,
                        "assignment_ref": format_assignment_ref(result.assignment_id),
                        "changed": result.changed,
                        "verification_status": result.verification_status,
                    },
                ),
                ReplyText(text=reply, buttons=buttons, parse_mode=None),
            )
        )

    async def submit_direct_review_payload(
        self,
        *,
        text: str,
        buyer_user_id: int,
        update_id: int,
    ) -> FlowResult:
        try:
            result = await self._adapter.submit_review_payload_by_task_uuid(
                buyer_user_id=buyer_user_id,
                payload_base64=text,
            )
        except NotFoundError:
            return _direct_review_payload_rejected_result(
                update_id=update_id,
                reason="not_found",
                text="Токен-подтверждение отзыва не принят.\nПохоже, токен относится к другой покупке или устарел.",
            )
        except PayloadValidationError as exc:
            return _direct_review_payload_rejected_result(
                update_id=update_id,
                reason="payload_validation_error",
                text=_review_payload_validation_text(exc),
            )
        except InvalidStateError:
            return _direct_review_payload_rejected_result(
                update_id=update_id,
                reason="invalid_state",
                text="Сейчас нельзя отправить токен-подтверждение отзыва для этой покупки.",
            )

        reply, buttons = self._review_payload_reply_and_buttons(result)
        return FlowResult(
            effects=(
                DeleteSourceMessage(),
                LogEvent(
                    event_name="buyer_review_payload_submitted",
                    fields={
                        "telegram_update_id": update_id,
                        "assignment_id": result.assignment_id,
                        "assignment_ref": format_assignment_ref(result.assignment_id),
                        "changed": result.changed,
                        "verification_status": result.verification_status,
                        "direct_paste": True,
                    },
                ),
                ReplyText(text=reply, buttons=buttons, parse_mode=None),
            )
        )

    def _review_payload_reply_and_buttons(self, result: Any) -> tuple[str, tuple[tuple[ButtonSpec, ...], ...]]:
        buttons = _buyer_menu_buttons()
        if result.verification_status != "pending_manual":
            if result.changed:
                reply = "Отзыв подтвержден. Ожидайте начисления кэшбэка через 15 дней после выкупа товара."
            else:
                reply = "Этот токен-подтверждение отзыва уже был отправлен ранее."
        else:
            reason = str(result.verification_reason or "").strip()
            if result.changed:
                reply = (
                    "Токен-подтверждение отзыва сохранен, но автоматическая проверка не пройдена.\n"
                    "Кэшбэк пока не будет выплачен."
                )
            else:
                reply = (
                    "Этот токен-подтверждение отзыва уже был отправлен ранее.\n"
                    "Кэшбэк по покупке все еще заблокирован."
                )
            if reason:
                reply += f"\nПричина: {reason}"
            reply += (
                "\nИсправьте отзыв и отправьте новый токен "
                "или напишите в поддержку со скриншотом опубликованного отзыва."
            )
            buttons = self._buyer_review_followup_buttons(assignment_id=result.assignment_id)
        return reply, buttons

    async def submit_shop_slug(self, *, buyer_user_id: int, slug: str) -> FlowResult:
        catalog = await self.render_shop_catalog(
            slug=slug,
            buyer_user_id=buyer_user_id,
            replace=False,
            page=1,
            include_store_effect=True,
        )
        return FlowResult(effects=(ClearPrompt(), *catalog.effects))

    async def render_shop_catalog(
        self,
        *,
        slug: str,
        buyer_user_id: int | None = None,
        replace: bool = False,
        page: int = 1,
        include_store_effect: bool = True,
    ) -> FlowResult:
        normalized_slug = slug.strip()
        try:
            shop = await self._adapter.resolve_shop_by_slug(slug=normalized_slug)
            listings = await self._adapter.list_active_listings_by_shop_slug(
                slug=normalized_slug,
                buyer_user_id=buyer_user_id,
            )
        except (NotFoundError, InvalidStateError):
            if replace:
                return FlowResult(
                    effects=(
                        ReplaceText(
                            text="Магазин недоступен. Проверьте ссылку и попробуйте снова.",
                            buttons=_rows([[_button("↩️ Назад к магазинам", action="shops")]]),
                            parse_mode=None,
                        ),
                    )
                )
            return FlowResult(
                effects=(ReplyText(text="Магазин недоступен. Проверьте ссылку и попробуйте снова.", parse_mode=None),)
            )

        if buyer_user_id is not None:
            try:
                await self._adapter.touch_saved_shop(buyer_user_id=buyer_user_id, shop_id=shop.shop_id)
            except DomainError:
                pass

        effects = []
        if include_store_effect:
            effects.append(SetUserData(key=self._config.last_shop_slug_key, value=shop.slug))

        active_shop_purchase = None
        active_shop_purchases_count = 0
        if buyer_user_id is not None:
            buyer_assignments = _buyer_visible_assignments(
                await self._adapter.list_buyer_assignments(buyer_user_id=buyer_user_id)
            )
            active_shop_purchases_count = sum(1 for item in buyer_assignments if item.shop_slug == shop.slug)
            active_shop_purchase = next((item for item in buyer_assignments if item.shop_slug == shop.slug), None)

        can_remove_shop = active_shop_purchase is None and buyer_user_id is not None
        header = f"Магазин «{shop.title}»"
        shop_ref = format_shop_ref(shop.shop_id)
        if not listings:
            if active_shop_purchase is not None:
                text = screen_text(
                    title=html.escape(header),
                    title_suffix_html=title_ref_suffix(shop_ref),
                    lines=["У вас уже есть активная покупка в этом магазине. Других объявлений здесь пока нет."],
                )
                keyboard_rows = [
                    [
                        _button(
                            button_label_with_count("📋 Покупки", active_shop_purchases_count),
                            action="assignments",
                        )
                    ],
                    [_button("↩️ Назад к магазинам", action="shops")],
                    [_knowledge_button(topic="shops")],
                ]
            else:
                text = screen_text(
                    title=html.escape(header),
                    title_suffix_html=title_ref_suffix(shop_ref),
                    lines=["Активных объявлений пока нет."],
                )
                keyboard_rows = []
                if can_remove_shop:
                    keyboard_rows.append([_button("🗑 Удалить магазин", action="shop_remove", entity_id=shop.shop_id)])
                keyboard_rows.extend(
                    [
                        [_button("↩️ Назад к магазинам", action="shops")],
                        [_knowledge_button(topic="shops")],
                    ]
                )
            effects.append(_text_effect(text=text, buttons=_rows(keyboard_rows), replace=replace, parse_mode="HTML"))
            return FlowResult(effects=tuple(effects))

        resolved_page, total_pages, start_index, end_index = resolve_numbered_page(
            total_items=len(listings),
            requested_page=page,
        )
        listings_page = listings[start_index:end_index]
        lines: list[str] = []
        for idx, listing in enumerate(listings_page, start=start_index + 1):
            display_title = listing_display_title(
                display_title=listing.display_title,
                fallback=listing.search_phrase,
            )
            cashback_text = format_buyer_cashback_with_percent(
                reward_usdt=listing.reward_usdt,
                reference_price_rub=listing.reference_price_rub,
                display_rub_per_usdt=self._config.display_rub_per_usdt,
            )
            lines.append(
                f"<b>{idx}. {html.escape(display_title)}</b>\n"
                f"<b>Цена:</b> {format_price_optional_rub(listing.reference_price_rub)}\n"
                f"<b>Кэшбэк:</b> {cashback_text}"
            )
        extra_rows = []
        if can_remove_shop:
            extra_rows.append([_button("🗑 Удалить магазин", action="shop_remove", entity_id=shop.shop_id)])
        extra_rows.extend(
            [
                [_button("↩️ Назад к магазинам", action="shops")],
                [_knowledge_button(topic="shops")],
            ]
        )
        text = screen_text(
            title=html.escape(header),
            title_suffix_html=title_ref_suffix(shop_ref),
            lines=lines,
            separate_blocks=True,
        )
        effects.append(
            _text_effect(
                text=text,
                buttons=numbered_page_buttons(
                    flow=_ROLE_BUYER,
                    open_action="listing_open",
                    page_action="shop_page",
                    item_ids=[listing.listing_id for listing in listings_page],
                    start_number=start_index + 1,
                    page=resolved_page,
                    total_pages=total_pages,
                    extra_rows=extra_rows,
                ),
                replace=replace,
                parse_mode="HTML",
            )
        )
        return FlowResult(effects=tuple(effects))

    async def open_listing_deep_link(
        self,
        *,
        buyer_user_id: int,
        listing_id: int | None,
        replace: bool = False,
    ) -> FlowResult:
        if listing_id is None:
            return _listing_deep_link_unavailable_result(replace=replace)
        try:
            resolved = await self._adapter.resolve_active_listing_deep_link(
                listing_id=listing_id,
                buyer_user_id=buyer_user_id,
            )
        except (NotFoundError, InvalidStateError, ValueError):
            return _listing_deep_link_unavailable_result(replace=replace)

        try:
            await self._adapter.touch_saved_shop(buyer_user_id=buyer_user_id, shop_id=resolved.shop_id)
        except DomainError:
            pass

        effects: list[Any] = [SetUserData(key=self._config.last_shop_slug_key, value=resolved.shop_slug)]
        effects.extend(
            _listing_detail_effects(
                listing=resolved.listing,
                notice=repeat_purchase_listing_notice(resolved.buyer_action_state),
                action_state=resolved.buyer_action_state,
                display_rub_per_usdt=self._config.display_rub_per_usdt,
            )
        )
        return FlowResult(effects=tuple(effects))

    async def render_listing_detail(
        self,
        *,
        buyer_user_id: int,
        shop_slug: str,
        listing_id: int,
        notice: str | None = None,
    ) -> FlowResult:
        try:
            listings = await self._adapter.list_active_listings_by_shop_slug(
                slug=shop_slug,
                buyer_user_id=buyer_user_id,
            )
        except (NotFoundError, InvalidStateError):
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Магазин недоступен. Откройте каталог заново.",
                        buttons=_buyer_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        listing = next((item for item in listings if item.listing_id == listing_id), None)
        if listing is None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Товар больше недоступен.",
                        buttons=_rows([[_button("↩️ Назад к магазинам", action="shops")]]),
                        parse_mode=None,
                    ),
                )
            )
        return FlowResult(
            effects=tuple(
                _listing_detail_effects(
                    listing=listing,
                    notice=notice,
                    action_state=None,
                    display_rub_per_usdt=self._config.display_rub_per_usdt,
                )
            )
        )

    def _support_button(self) -> ButtonSpec | None:
        support_bot_username = self._config.support_bot_username
        if not support_bot_username:
            return None
        return ButtonSpec(
            text="🆘 Поддержка",
            url=build_support_deep_link(
                bot_username=support_bot_username,
                role=_ROLE_BUYER,
                topic="generic",
                refs=(),
            ),
        )

    def _buyer_review_followup_buttons(self, *, assignment_id: int) -> tuple[tuple[ButtonSpec, ...], ...]:
        keyboard_rows: list[list[ButtonSpec]] = []
        support_bot_username = self._config.support_bot_username
        if support_bot_username:
            keyboard_rows.append(
                [
                    ButtonSpec(
                        text="🆘 Поддержка",
                        url=build_support_deep_link(
                            bot_username=support_bot_username,
                            role=_ROLE_BUYER,
                            topic="review",
                            refs=(format_assignment_ref(assignment_id),),
                        ),
                    )
                ]
            )
        keyboard_rows.extend([list(row) for row in _buyer_menu_buttons()])
        return _rows(keyboard_rows)

def buyer_task_instruction_text(assignment: Any, *, include_title: bool = True) -> str:
    listing_token = _build_buyer_listing_token(
        task_uuid=str(assignment.task_uuid),
        search_phrase=assignment.search_phrase,
        wb_product_id=assignment.wb_product_id,
        brand_name=getattr(assignment, "wb_brand_name", None),
    )
    reservation_deadline = format_datetime_msk(getattr(assignment, "reservation_expires_at", None))
    display_title = listing_display_title(
        display_title=getattr(assignment, "display_title", None),
        fallback=assignment.search_phrase,
    )
    lines: list[str] = []
    if include_title:
        lines.append(f"<b>Товар:</b> {html.escape(display_title)}")
    lines.extend(
        [
            (
                '1. Введите токен в <a href="'
                f"{_QPILKA_EXTENSION_URL}"
                '">расширении для браузера Chrome / Яндекс Qpilka</a>:'
            ),
            f"<code>{listing_token}</code>",
            (
                "2. Выполните шаги, описанные в расширении, и оформите заказ "
                f"до {reservation_deadline} (по истечении срока бронь отменится)."
            ),
            "3. Отправьте токен-подтверждение сюда.",
        ]
    )
    return "\n".join(lines)


def buyer_review_instruction_text(assignment: Any, *, include_title: bool = True) -> str:
    review_token = _build_buyer_review_token(
        task_uuid=str(assignment.task_uuid),
        wb_product_id=assignment.wb_product_id,
        review_phrases=getattr(assignment, "review_phrases", None),
    )
    display_title = listing_display_title(
        display_title=getattr(assignment, "display_title", None),
        fallback=assignment.search_phrase,
    )
    lines: list[str] = []
    if include_title:
        lines.append(f"<b>Товар:</b> {html.escape(display_title)}")
    selected_phrases = normalize_review_phrases(getattr(assignment, "review_phrases", None))
    lines.extend(
        [
            "Скопируйте токен ниже в расширение Qpilka.",
            f"<code>{review_token}</code>",
            (
                'Расширение покажет, какой отзыв оставить на WB. '
                '<a href="'
                f"{_QPILKA_EXTENSION_URL}"
                '">Открыть расширение для Chrome / Яндекс</a>.'
            ),
            "Поставьте 5 звезд и добавьте обязательные фразы.",
            "После публикации расширение выдаст токен-подтверждение.",
            "Вернитесь сюда и нажмите кнопку ниже.",
        ]
    )
    if selected_phrases:
        lines.append("<b>Обязательные фразы:</b> " + html.escape(format_review_phrases_text(selected_phrases)))
    return "\n".join(lines)


def buyer_shop_activity_badge(active_listings_count: int) -> str:
    return "🟢" if active_listings_count > 0 else "🔴"


def buyer_dashboard_status_bucket(status: str) -> str | None:
    if status == "reserved":
        return "awaiting_order"
    if status in {"order_verified", "picked_up_wait_review", "wb_invalid", "delivery_expired"}:
        return "ordered"
    if status in {
        "picked_up_wait_unlock",
        "withdraw_sent",
        "returned_within_14d",
    }:
        return "picked_up"
    return None


def _buyer_visible_assignments(assignments: list[Any]) -> list[Any]:
    visible_statuses = {
        "reserved",
        "order_verified",
        "picked_up_wait_review",
        "picked_up_wait_unlock",
        "withdraw_sent",
    }
    return [item for item in assignments if item.status in visible_statuses]


def classify_buyer_token_text(text: str) -> str | None:
    if looks_like_purchase_payload(text):
        return "purchase"
    try:
        decode_review_payload(text)
    except PayloadValidationError:
        return None
    return "review"


def looks_like_purchase_payload(text: str) -> bool:
    try:
        decode_purchase_payload(text)
    except PayloadValidationError:
        return False
    return True


def _buyer_shop_title(assignment: Any) -> str:
    title = str(getattr(assignment, "shop_title", "") or "").strip()
    if title:
        return title
    return str(getattr(assignment, "shop_slug", "") or "").strip()


def buyer_purchase_status_badge(status: str) -> str:
    bucket = buyer_dashboard_status_bucket(status)
    if bucket == "awaiting_order":
        color = "red"
    elif bucket == "ordered":
        color = "yellow"
    elif bucket == "picked_up":
        color = "green"
    else:
        color = "blue"
    return status_badge(_humanize_assignment_status(status), color=color)


def _humanize_assignment_status(status: str) -> str:
    mapping = {
        "reserved": "Ожидает заказа",
        "order_verified": "Заказан",
        "picked_up_wait_review": "Нужно оставить отзыв",
        "picked_up_wait_unlock": "Выкуплен",
        "withdraw_sent": "Выплачен",
        "expired_2h": "Бронь истекла",
        "buyer_cancelled": "Покупка отменена",
        "wb_invalid": "Не подтвержден",
        "returned_within_14d": "Возвращен",
        "delivery_expired": "Срок выкупа истек",
    }
    return mapping.get(status, status)


def _build_buyer_listing_token(
    *,
    task_uuid: str,
    search_phrase: str,
    wb_product_id: int,
    brand_name: str | None,
) -> str:
    payload = [
        1,
        task_uuid,
        search_phrase,
        wb_product_id,
        _BUYER_TASK_COMPANION_PRODUCTS,
        (brand_name or "").strip(),
    ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _build_buyer_review_token(
    *,
    task_uuid: str,
    wb_product_id: int,
    review_phrases: list[str] | None,
) -> str:
    payload: list[Any] = [2, task_uuid, wb_product_id]
    payload.extend(normalize_review_phrases(review_phrases)[:2])
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _active_purchase_exists_result() -> FlowResult:
    return FlowResult(
        effects=(
            ReplaceText(
                text=ACTIVE_PURCHASE_LISTING_NOTICE,
                buttons=_rows(
                    [
                        [_button("📋 Покупки", action="assignments")],
                        [_button("↩️ Назад к магазинам", action="shops")],
                    ]
                ),
                parse_mode=None,
            ),
        )
    )


def _missing_assignment_result(*, text: str) -> FlowResult:
    return FlowResult(
        effects=(
            ReplaceText(
                text=text,
                buttons=_rows([[_button("↩️ Назад к покупкам", action="assignments")]]),
                parse_mode=None,
            ),
        )
    )


def _purchase_payload_validation_text(exc: PayloadValidationError) -> str:
    details = str(exc).strip().lower()
    base = (
        "Токен-подтверждение не принят.\n"
        "Проверьте, что вы скопировали его полностью из расширения для этой покупки."
    )
    if "task_uuid" in details:
        return f"{base}\nПохоже, токен относится к другой покупке или устарел."
    if details and "timezone" in details:
        return f"{base}\nПроверьте дату и время на устройстве и сформируйте токен заново."
    return base


def _direct_purchase_payload_rejected_result(*, update_id: int, reason: str, text: str) -> FlowResult:
    return FlowResult(
        effects=(
            DeleteSourceMessage(),
            LogEvent(
                event_name="buyer_direct_payload_rejected",
                fields={
                    "telegram_update_id": update_id,
                    "reason": reason,
                },
            ),
            ReplyText(text=text, buttons=_buyer_menu_buttons(), parse_mode=None),
        )
    )


def _direct_review_payload_rejected_result(*, update_id: int, reason: str, text: str) -> FlowResult:
    return FlowResult(
        effects=(
            DeleteSourceMessage(),
            LogEvent(
                event_name="buyer_direct_review_payload_rejected",
                fields={
                    "telegram_update_id": update_id,
                    "reason": reason,
                },
            ),
            ReplyText(text=text, buttons=_buyer_menu_buttons(), parse_mode=None),
        )
    )


def _review_payload_validation_text(exc: PayloadValidationError) -> str:
    details = str(exc).strip().lower()
    base = (
        "Токен-подтверждение отзыва не принят.\n"
        "Проверьте, что вы скопировали его полностью из расширения для этой покупки."
    )
    if "task_uuid" in details:
        return f"{base}\nПохоже, токен относится к другой покупке или устарел."
    if "timezone" in details:
        return f"{base}\nПроверьте дату и время на устройстве и сформируйте токен заново."
    return base


def _button(text: str, *, action: str, entity_id: int | str = "") -> ButtonSpec:
    return ButtonSpec(text=text, flow=_ROLE_BUYER, action=action, entity_id=str(entity_id))


def _knowledge_button(*, topic: str) -> ButtonSpec:
    mapping = {
        "guide": ("📘 Инструкция", "kb_guide"),
        "shops": ("📘 Про магазины", "kb_shops"),
        "purchases": ("📘 Про покупки", "kb_purchases"),
        "balance": ("📘 Про баланс и вывод", "kb_balance"),
    }
    label, action = mapping[topic]
    return _button(label, action=action)


def _buyer_menu_buttons(
    *,
    shops_count: int | None = None,
    purchases_count: int | None = None,
) -> tuple[tuple[ButtonSpec, ...], ...]:
    return _rows(
        [
            [
                _button(button_label_with_count("🏪 Магазины", shops_count), action="shops"),
                _button(button_label_with_count("📋 Покупки", purchases_count), action="assignments"),
            ],
            [_button("💳 Баланс и вывод", action="balance")],
            [_knowledge_button(topic="guide")],
        ]
    )


def _rows(rows: list[list[ButtonSpec]]) -> tuple[tuple[ButtonSpec, ...], ...]:
    return tuple(tuple(row) for row in rows)


def _text_effect(
    *,
    text: str,
    buttons: tuple[tuple[ButtonSpec, ...], ...],
    replace: bool,
    parse_mode: str | None,
) -> ReplaceText | ReplyText:
    if replace:
        return ReplaceText(text=text, buttons=buttons, parse_mode=parse_mode)
    return ReplyText(text=text, buttons=buttons, parse_mode=parse_mode)


def _listing_detail_effects(
    *,
    listing: Any,
    notice: str | None,
    action_state: str | None,
    display_rub_per_usdt: Decimal,
) -> tuple[ReplyPhoto | ReplaceText, ...]:
    keyboard_rows: list[list[ButtonSpec]] = []
    if action_state is None:
        keyboard_rows.append([_button("✅ Купить", action="reserve", entity_id=listing.listing_id)])
    elif action_state in {"active_purchase", "already_purchased"}:
        keyboard_rows.append([_button("📋 Покупки", action="assignments")])
    keyboard_rows.extend(
        [
            [_button("↩️ Назад к каталогу", action="open_last_shop")],
            [_knowledge_button(topic="purchases")],
        ]
    )
    return (
        ReplyPhoto(photo_url=listing.wb_photo_url),
        ReplaceText(
            text=buyer_listing_detail_html(
                listing=listing,
                notice=notice,
                display_rub_per_usdt=display_rub_per_usdt,
            ),
            buttons=_rows(keyboard_rows),
            parse_mode="HTML",
        ),
    )

def _listing_deep_link_unavailable_result(*, replace: bool) -> FlowResult:
    effect = _text_effect(
        text="Товар по ссылке недоступен. Откройте магазин или выберите другой товар.",
        buttons=_rows([[_button("↩️ К магазинам", action="shops")]]),
        replace=replace,
        parse_mode=None,
    )
    return FlowResult(effects=(effect,))

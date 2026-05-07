from __future__ import annotations

import html
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from libs.domain.errors import DomainError, InvalidStateError, NotFoundError
from libs.domain.public_refs import build_support_deep_link, format_listing_ref, format_shop_ref
from services.bot_api.transport_effects import (
    ButtonSpec,
    ClearPrompt,
    FlowResult,
    ReplaceText,
    ReplyPhoto,
    ReplyText,
    SetPrompt,
    SetUserData,
)

_ROLE_BUYER = "buyer"
_QPILKA_EXTENSION_URL = "https://chromewebstore.google.com/detail/qpilka/joefinmgneknnaejambgbaclobeedaga"
_NUMBERED_PAGE_SIZE = 10
_USDT_EXACT_QUANT = Decimal("0.000001")
_RUB_QUANT = Decimal("1")


class BuyerMarketplaceAdapter(Protocol):
    async def get_buyer_balance_snapshot(self, *, buyer_user_id: int) -> Any: ...

    async def list_buyer_assignments(self, *, buyer_user_id: int) -> list[Any]: ...

    async def list_saved_shops(self, *, buyer_user_id: int, limit: int = 20) -> list[Any]: ...

    async def resolve_shop_by_slug(self, *, slug: str) -> Any: ...

    async def list_active_listings_by_shop_slug(
        self,
        *,
        slug: str,
        buyer_user_id: int | None = None,
    ) -> list[Any]: ...

    async def touch_saved_shop(self, *, buyer_user_id: int, shop_id: int) -> None: ...

    async def resolve_saved_shop_for_buyer(self, *, buyer_user_id: int, shop_id: int) -> Any: ...

    async def remove_saved_shop(self, *, buyer_user_id: int, shop_id: int) -> Any: ...


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
        text = _screen_text(
            title="Кабинет покупателя",
            cta="Выберите раздел ниже.",
            lines=[
                (
                    "<b>Покупки:</b> "
                    f"ожидают заказа: {bucket_counts['awaiting_order']} · "
                    f"заказаны: {bucket_counts['ordered']} · "
                    f"выкуплены: {bucket_counts['picked_up']}"
                ),
                f"<b>Баланс:</b> {self._format_buyer_balance_amount(snapshot.buyer_available_usdt)}",
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

    def render_knowledge_screen(self, *, topic: str) -> FlowResult:
        if topic == "guide":
            text = _screen_text(
                title="Инструкция покупателя",
                cta=(
                    "Купилка позволяет просто и безопасно покупать товары на Wildberries "
                    "и получать за это кэшбэк на криптокошелек."
                ),
                lines=[
                    (
                        "<b>Как пользоваться ботом</b>\n"
                        "1. Установите расширение для браузера Chrome / Яндекс Qpilka "
                        "(обязательно):\n"
                        f'<a href="{_QPILKA_EXTENSION_URL}">{_QPILKA_EXTENSION_URL}</a>\n'
                        "2. Откройте магазин и выберите товар.\n"
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
                        "1. <b>Где найти магазин?</b>\n"
                        "Ссылки на магазины публикуются в профильных телеграм группах.\n\n"
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
            text = _screen_text(
                title="Про магазины",
                cta="Магазин — это подборка доступных объявлений одного продавца.",
                lines=[
                    (
                        "Магазины сохраняются в вашем профиле, и вы всегда можете к ним вернуться "
                        "позднее. Добавить магазин в профиль можно, перейдя по его ссылке."
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
            text = _screen_text(
                title="Про покупки",
                cta="Покупка появляется после бронирования товара и проходит несколько статусов.",
                lines=[
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
            text = _screen_text(
                title="Про баланс и вывод",
                cta=(
                    "На балансе покупателя отображается сумма, доступная к выводу, "
                    "а также сумма, ожидающая разблокировки кэшбэка."
                ),
                lines=[
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
            text = _screen_text(
                title="Магазины",
                cta="Сохраненных магазинов пока нет.",
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

        resolved_page, total_pages, start_index, end_index = _resolve_numbered_page(
            total_items=len(saved_shops),
            requested_page=page,
        )
        shops_page = saved_shops[start_index:end_index]
        for idx, shop in enumerate(shops_page, start=start_index + 1):
            badge = buyer_shop_activity_badge(shop.active_listings_count)
            title = html.escape(shop.title)
            lines.append(f"<b>{idx}. {badge} {title} (объявлений: {shop.active_listings_count})</b>")

        text = _screen_text(
            title="Магазины",
            cta="Выберите номер магазина.",
            lines=lines,
            separate_blocks=True,
        )
        return FlowResult(
            effects=(
                ReplaceText(
                    text=text,
                    buttons=_numbered_page_buttons(
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
            text = _screen_text(
                title=f"Магазин «{html.escape(shop.title)}»",
                cta="Удаление недоступно, пока в магазине есть незавершенная покупка.",
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
                text = _screen_text(
                    title=html.escape(header),
                    title_suffix_html=_title_ref_suffix(shop_ref),
                    cta="У вас уже есть активная покупка в этом магазине. Других объявлений здесь пока нет.",
                )
                keyboard_rows = [
                    [
                        _button(
                            _button_label_with_count("📋 Покупки", active_shop_purchases_count),
                            action="assignments",
                        )
                    ],
                    [_button("↩️ Назад к магазинам", action="shops")],
                    [_knowledge_button(topic="shops")],
                ]
            else:
                text = _screen_text(
                    title=html.escape(header),
                    title_suffix_html=_title_ref_suffix(shop_ref),
                    cta="Активных объявлений пока нет.",
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

        resolved_page, total_pages, start_index, end_index = _resolve_numbered_page(
            total_items=len(listings),
            requested_page=page,
        )
        listings_page = listings[start_index:end_index]
        lines: list[str] = []
        for idx, listing in enumerate(listings_page, start=start_index + 1):
            display_title = _listing_display_title(
                display_title=listing.display_title,
                fallback=listing.search_phrase,
            )
            cashback_text = self._format_buyer_cashback_with_percent(
                reward_usdt=listing.reward_usdt,
                reference_price_rub=listing.reference_price_rub,
            )
            lines.append(
                f"<b>{idx}. {html.escape(display_title)}</b>\n"
                f"<b>Цена:</b> {_format_price_optional_rub(listing.reference_price_rub)}\n"
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
        text = _screen_text(
            title=html.escape(header),
            title_suffix_html=_title_ref_suffix(shop_ref),
            cta="Выберите номер объявления.",
            lines=lines,
            separate_blocks=True,
        )
        effects.append(
            _text_effect(
                text=text,
                buttons=_numbered_page_buttons(
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
        keyboard_rows = [
            [_button("✅ Купить", action="reserve", entity_id=listing.listing_id)],
            [_button("↩️ Назад к каталогу", action="open_last_shop")],
            [_knowledge_button(topic="purchases")],
        ]
        return FlowResult(
            effects=(
                ReplyPhoto(photo_url=listing.wb_photo_url),
                ReplaceText(
                    text=buyer_listing_detail_html(
                        listing=listing,
                        notice=notice,
                        display_rub_per_usdt=self._config.display_rub_per_usdt,
                    ),
                    buttons=_rows(keyboard_rows),
                    parse_mode="HTML",
                ),
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

    def _format_rub_approx(self, amount: Decimal) -> str:
        rub = amount * self._config.display_rub_per_usdt
        return f"~{_format_decimal(rub, quant=_RUB_QUANT)} ₽"

    def _format_buyer_cashback_with_percent(
        self,
        *,
        reward_usdt: Decimal,
        reference_price_rub: int | None,
    ) -> str:
        primary = self._format_rub_approx(reward_usdt)
        if reward_usdt.quantize(_USDT_EXACT_QUANT, rounding=ROUND_HALF_UP) == Decimal("0.000000"):
            return primary
        if reference_price_rub is None or reference_price_rub < 1:
            return primary
        cashback_rub = Decimal(_format_decimal(reward_usdt * self._config.display_rub_per_usdt, quant=_RUB_QUANT))
        percent = (cashback_rub / Decimal(reference_price_rub) * Decimal("100")).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
        return f"{primary} (~{percent}%)"

    def _format_buyer_balance_amount(self, amount: Decimal) -> str:
        return self._format_rub_approx(amount)


def buyer_listing_detail_html(
    *,
    listing: Any,
    display_rub_per_usdt: Decimal,
    notice: str | None = None,
) -> str:
    display_title = _listing_display_title(
        display_title=listing.display_title,
        fallback=listing.search_phrase,
    )
    lines: list[str] = []
    if notice:
        lines.append(html.escape(notice))
    cashback_text = _format_buyer_cashback_with_percent(
        reward_usdt=listing.reward_usdt,
        reference_price_rub=listing.reference_price_rub,
        display_rub_per_usdt=display_rub_per_usdt,
    )
    lines.extend(
        [
            f"<b>Предмет:</b> {html.escape(listing.wb_subject_name or '—')}",
            _format_listing_price_line(label="Цена", price_rub=listing.reference_price_rub, source=None),
            f"<b>Кэшбэк:</b> {html.escape(cashback_text)}",
            f"<b>Поисковая фраза:</b> &quot;{html.escape(listing.search_phrase)}&quot;",
        ]
    )
    if _should_show_buyer_sizes(listing.wb_tech_sizes):
        lines.append(f"<b>Размеры:</b> {html.escape(_format_sizes_text(listing.wb_tech_sizes))}")
    description_block = _format_expandable_block_html(title="Описание", body=listing.wb_description)
    if description_block:
        lines.append(f"\n{description_block}")
    characteristics_block = _format_characteristics_block_html(listing.wb_characteristics)
    if characteristics_block:
        lines.append(f"\n{characteristics_block}")
    return _screen_text(
        title=f"📦 {display_title}",
        title_suffix_html=_title_ref_suffix(format_listing_ref(listing.listing_id)),
        cta="Проверьте товар и выберите следующее действие ниже.",
        lines=lines,
        separate_blocks=True,
    )


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
                _button(_button_label_with_count("🏪 Магазины", shops_count), action="shops"),
                _button(_button_label_with_count("📋 Покупки", purchases_count), action="assignments"),
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


def _numbered_page_buttons(
    *,
    open_action: str,
    page_action: str,
    item_ids: list[int],
    start_number: int,
    page: int,
    total_pages: int,
    extra_rows: list[list[ButtonSpec]] | None = None,
) -> tuple[tuple[ButtonSpec, ...], ...]:
    rows: list[list[ButtonSpec]] = []
    current_row: list[ButtonSpec] = []
    for offset, item_id in enumerate(item_ids):
        current_row.append(_button(str(start_number + offset), action=open_action, entity_id=item_id))
        if len(current_row) == 5:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    if total_pages > 1:
        nav_row: list[ButtonSpec] = []
        if page > 1:
            nav_row.append(_button("⬅️", action=page_action, entity_id=page - 1))
        if page < total_pages:
            nav_row.append(_button("➡️", action=page_action, entity_id=page + 1))
        if nav_row:
            rows.append(nav_row)

    if extra_rows:
        rows.extend(extra_rows)
    return _rows(rows)


def _resolve_numbered_page(
    *,
    total_items: int,
    requested_page: int,
    page_size: int = _NUMBERED_PAGE_SIZE,
) -> tuple[int, int, int, int]:
    if total_items <= 0:
        return 1, 1, 0, 0
    total_pages = (total_items + page_size - 1) // page_size
    page = max(1, min(requested_page, total_pages))
    start_index = (page - 1) * page_size
    end_index = min(start_index + page_size, total_items)
    return page, total_pages, start_index, end_index


def _button_label_with_count(label: str, count: int | None) -> str:
    if count is None:
        return label
    normalized_count = max(0, int(count))
    return f"{label} · {normalized_count}"


def _listing_display_title(*, display_title: str | None, fallback: str) -> str:
    normalized = (display_title or "").strip()
    return normalized or fallback.strip()


def _format_buyer_cashback_with_percent(
    *,
    reward_usdt: Decimal,
    reference_price_rub: int | None,
    display_rub_per_usdt: Decimal,
) -> str:
    primary = f"~{_format_decimal(reward_usdt * display_rub_per_usdt, quant=_RUB_QUANT)} ₽"
    if reward_usdt.quantize(_USDT_EXACT_QUANT, rounding=ROUND_HALF_UP) == Decimal("0.000000"):
        return primary
    if reference_price_rub is None or reference_price_rub < 1:
        return primary
    cashback_rub = Decimal(_format_decimal(reward_usdt * display_rub_per_usdt, quant=_RUB_QUANT))
    percent = (cashback_rub / Decimal(reference_price_rub) * Decimal("100")).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )
    return f"{primary} (~{percent}%)"


def _format_decimal(
    amount: Decimal,
    *,
    quant: Decimal,
    rounding=ROUND_HALF_UP,
) -> str:
    normalized = amount.quantize(quant, rounding=rounding)
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _format_price_rub(amount: int | Decimal | None) -> str:
    if amount is None:
        return "0 ₽"
    rub = Decimal(str(amount)).quantize(_RUB_QUANT, rounding=ROUND_CEILING)
    return f"{_format_decimal(rub, quant=_RUB_QUANT)} ₽"


def _format_price_optional_rub(amount: int | Decimal | None) -> str:
    if amount is None:
        return "—"
    return _format_price_rub(amount)


def _format_listing_price_line(*, label: str, price_rub: int | None, source: str | None) -> str:
    if price_rub is None:
        return f"<b>{html.escape(label)}:</b> —"
    suffix = ""
    if source == "orders":
        suffix = " (из заказов)"
    elif source == "manual":
        suffix = " (вручную)"
    return f"<b>{html.escape(label)}:</b> {_format_price_rub(price_rub)}{html.escape(suffix)}"


def _normalize_sizes(sizes: list[str] | None) -> list[str]:
    if not sizes:
        return []
    normalized: list[str] = []
    for size in sizes:
        cleaned = str(size).strip()
        if cleaned:
            normalized.append(cleaned)
    return normalized


def _should_show_buyer_sizes(sizes: list[str] | None) -> bool:
    return _normalize_sizes(sizes) != ["0"]


def _format_sizes_text(sizes: list[str] | None) -> str:
    normalized = _normalize_sizes(sizes)
    if not normalized:
        return "—"
    return ", ".join(normalized)


def _format_characteristics_block_html(characteristics: list[dict[str, str]] | None) -> str | None:
    if not characteristics:
        return None
    lines = []
    for item in characteristics:
        name = html.escape(str(item.get("name", "")).strip())
        value = html.escape(str(item.get("value", "")).strip())
        if not name or not value:
            continue
        lines.append(f"{name}: {value}")
    if not lines:
        return None
    return "<b>Характеристики</b>\n<blockquote expandable>" + "\n".join(lines) + "</blockquote>"


def _format_expandable_block_html(*, title: str, body: str | None) -> str | None:
    normalized = (body or "").strip()
    if not normalized:
        return None
    return f"<b>{html.escape(title)}</b>\n<blockquote expandable>{html.escape(normalized)}</blockquote>"


def _screen_text(
    *,
    title: str,
    title_suffix_html: str | None = None,
    cta: str | None = None,
    lines: list[str] | None = None,
    note: str | None = None,
    warning: bool = False,
    separate_blocks: bool = False,
) -> str:
    plain_title = html.unescape(title)
    if title.startswith(("🧑‍💼 ", "🛍️ ", "🏪 ", "📦 ", "📋 ", "💳 ", "💰 ", "📘 ")):
        decorated_title = title
    elif plain_title.startswith(("Инструкция", "Про ")):
        decorated_title = f"📘 {title}"
    elif plain_title.startswith("Кабинет покупателя"):
        decorated_title = f"🛍️ {title}"
    elif plain_title.startswith(("Магазины", "Магазин")):
        decorated_title = f"🏪 {title}"
    elif plain_title.startswith(("Объявления", "Название объявления", "Новое объявление")):
        decorated_title = f"📦 {title}"
    elif plain_title.startswith(("Покупки", "Покупка", "Токен-подтверждение", "Токен отзыва", "Отмена покупки")):
        decorated_title = f"📋 {title}"
    elif plain_title.startswith(("Баланс", "Транзакции", "Отмена вывода")):
        decorated_title = f"💳 {title}"
    else:
        decorated_title = title
    title_html = f"{'⚠️ ' if warning else ''}<b>{decorated_title}</b>"
    if title_suffix_html:
        title_html += title_suffix_html
    parts = [title_html]
    if cta:
        parts.append(f"<i>{cta}</i>")
    if lines:
        filtered = [line for line in lines if line]
        if filtered:
            parts.append(("\n\n" if separate_blocks else "\n").join(filtered))
    if note:
        parts.append(f"<i>{note}</i>")
    return "\n\n".join(parts)


def _format_copyable_code(value: str) -> str:
    return f"<code>{html.escape(value.strip())}</code>"


def _title_ref_suffix(value: str | None) -> str | None:
    if not value:
        return None
    return f" · {_format_copyable_code(value)}"

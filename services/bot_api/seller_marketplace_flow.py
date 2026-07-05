from __future__ import annotations

import html
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from libs.domain.errors import InsufficientFundsError, InvalidStateError, ListingValidationError, NotFoundError
from libs.domain.public_refs import build_support_deep_link, format_deposit_ref, format_listing_ref, format_shop_ref
from libs.security.token_cipher import encrypt_token
from services.bot_api.deep_links import build_listing_deep_link, build_shop_deep_link
from services.bot_api.presentation import (
    button_label_with_count,
    entity_block_heading_with_ref,
    format_cashback_with_percent,
    format_characteristics_block_html,
    format_copyable_code,
    format_datetime_msk,
    format_expandable_block_html,
    format_listing_price_line,
    format_review_phrases_text,
    format_sizes_text,
    format_usdt,
    format_usdt_value,
    format_usdt_with_rub,
    listing_display_title,
    numbered_page_buttons,
    page_nav_row,
    resolve_numbered_page,
    screen_text,
    status_badge,
    title_ref_suffix,
    withdrawal_history_block_html,
    withdrawal_request_block_html,
)
from services.bot_api.seller_listing_creation_flow import SellerListingCreationFlow
from services.bot_api.transport_effects import (
    ButtonSpec,
    ClearPrompt,
    FlowResult,
    LogEvent,
    ReplaceText,
    ReplyPhoto,
    ReplyText,
    SetPrompt,
    SetUserData,
)

_ROLE_SELLER = "seller"
_USDT_EXACT_QUANT = Decimal("0.000001")
_LISTING_COLLATERAL_FEE_MULTIPLIER = Decimal("1.01")


@dataclass(frozen=True)
class SellerMarketplaceFlowConfig:
    display_rub_per_usdt: Decimal
    telegram_bot_username: str
    token_cipher_key: str
    seller_collateral_shard_key: str
    seller_collateral_invoice_ttl_hours: int
    tonapi_usdt_jetton_master: str
    telegram_wallet_open_url: str
    support_bot_username: str | None = None
    seller_listings_page_key: str = "seller_listings_page"


class SellerMarketplaceFlow:
    def __init__(
        self,
        *,
        seller_service: Any,
        seller_workflow: Any | None,
        finance_service: Any,
        deposit_service: Any,
        wb_ping_client: Any | None,
        listing_creation_flow: SellerListingCreationFlow,
        config: SellerMarketplaceFlowConfig,
        listing_product_validator: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._seller_service = seller_service
        self._seller_workflow = seller_workflow
        self._finance_service = finance_service
        self._deposit_service = deposit_service
        self._wb_ping_client = wb_ping_client
        self._listing_creation_flow = listing_creation_flow
        self._config = config
        self._listing_product_validator = listing_product_validator

    async def render_dashboard(self, *, seller_user_id: int) -> FlowResult:
        shops = await self._seller_service.list_shops(seller_user_id=seller_user_id)
        listings = await self._seller_service.list_listing_collateral_views(seller_user_id=seller_user_id)
        balance = await self._seller_service.get_seller_balance_snapshot(seller_user_id=seller_user_id)
        orders = await self._load_seller_order_counters(seller_user_id=seller_user_id)

        listings_active = sum(1 for item in listings if item.status == "active")
        listings_total = len(listings)
        shops_total = len(shops)
        shops_active = sum(1 for item in shops if _is_valid_shop_token(item.wb_token_status))
        balance_total = (
            balance.seller_available_usdt + balance.seller_collateral_usdt + balance.seller_withdraw_pending_usdt
        )
        text = screen_text(
            title="Кабинет продавца",
            cta="Выберите раздел ниже.",
            lines=[
                f"<b>Магазины:</b> {shops_total} · {shops_active} активно",
                f"<b>Объявления:</b> {listings_total} · {listings_active} активно",
                "<b>Покупки:</b> "
                f"ожидают заказа: {orders['awaiting_order']} · "
                f"заказаны: {orders['ordered']} · "
                f"выкуплены: {orders['picked_up']}",
                f"<b>Баланс:</b> {self._format_usdt_with_rub(balance_total)}",
                f"<b>Свободно:</b> {self._format_usdt_with_rub(balance.seller_available_usdt)}",
            ],
            note="Откройте нужный раздел ниже.",
        )
        return FlowResult(
            effects=(
                ReplaceText(
                    text=text,
                    buttons=self._seller_menu_buttons(listings_count=listings_total, shops_count=shops_total),
                    parse_mode="HTML",
                ),
            )
        )

    def render_knowledge_screen(self, *, topic: str) -> FlowResult:
        if topic == "guide":
            text = screen_text(
                title="Инструкция продавца",
                cta=(
                    "Купилка помогает выдать кэшбэк за покупку и отзыв, "
                    "а обеспечение держит на балансе до завершения покупки."
                ),
                lines=[
                    (
                        "<b>Как запустить витрину</b>\n"
                        "1. Создайте магазин и добавьте WB API токен в режиме чтения.\n"
                        "2. Создайте объявление: артикул WB, кэшбэк в ₽, число покупок, поисковая фраза "
                        "и до 10 фраз для отзыва.\n"
                        "3. Пополните баланс, если не хватает обеспечения.\n"
                        "4. Активируйте объявление и отправьте покупателям ссылку на товар.\n"
                        "5. Следите за покупками и выводите свободный остаток."
                    ),
                    (
                        "<b>Полезно знать</b>\n"
                        "1. Покупатели видят название магазина и название объявления.\n"
                        "2. Объявление нельзя менять после создания: если условия изменились, создайте новое "
                        "и удалите старое.\n"
                        "3. Деньги под активные объявления нельзя вывести, пока они зарезервированы."
                    ),
                ],
                note="Подробности по каждому разделу открываются кнопками ниже.",
                separate_blocks=True,
            )
            rows = [
                [self._knowledge_button(topic="shops"), self._knowledge_button(topic="listings")],
                [self._knowledge_button(topic="balance")],
                [_button("↩️ Назад", action="menu")],
            ]
        elif topic == "shops":
            text = screen_text(
                title="Про магазины",
                cta="Магазин — это ваша публичная витрина, которая объединяет объявления.",
                lines=[
                    (
                        "В магазине находятся объявления. Для покупателя публикуйте ссылку на конкретный товар, "
                        "чтобы он сразу попал в нужную карточку."
                    ),
                    (
                        "<b>Полезно знать</b>\n"
                        "1. Название магазина видят покупатели, поэтому используйте нейтральное и понятное имя.\n"
                        "2. При переименовании ссылка меняется, старая перестает работать.\n"
                        "3. Без валидного WB API токена объявления нельзя активировать."
                    ),
                ],
                separate_blocks=True,
            )
            rows = [
                [self._knowledge_button(topic="guide"), self._knowledge_button(topic="listings")],
                [self._knowledge_button(topic="balance")],
                [_button("↩️ К магазинам", action="shops")],
            ]
        elif topic == "listings":
            text = screen_text(
                title="Про объявления",
                cta="Объявление описывает один товар WB, размер кэшбэка и число доступных покупок.",
                lines=[
                    (
                        "После создания бот фиксирует кэшбэк в USDT, проверяет карточку WB "
                        "и показывает покупателю только нужные поля. После выкупа покупатель "
                        "подтверждает отзыв на 5 звезд."
                    ),
                    "Каждое объявление получает отдельную ссылку на товар для покупателей.",
                    (
                        "<b>Полезно знать</b>\n"
                        "1. Если параметр нужно изменить, создайте новое объявление.\n"
                        "2. Активация требует валидный токен WB, достаточное обеспечение и живую карточку товара.\n"
                        "3. Для отзыва можно указать до 10 фраз; покупатель получит до двух случайных фраз.\n"
                        "4. Если средств не хватает, сначала пополните баланс продавца."
                    ),
                ],
                separate_blocks=True,
            )
            rows = [
                [self._knowledge_button(topic="guide"), self._knowledge_button(topic="shops")],
                [self._knowledge_button(topic="balance")],
                [_button("↩️ К объявлениям", action="listings")],
            ]
        else:
            text = screen_text(
                title="Про баланс и вывод",
                cta="Баланс продавца делится на свободные средства, обеспечение объявлений и вывод.",
                lines=[
                    (
                        "Свободный остаток можно использовать для новых объявлений или вывести. "
                        "Обеспечение активных объявлений и сумма в заявке на вывод временно недоступны."
                    ),
                    (
                        "<b>Полезно знать</b>\n"
                        "1. Пополнение и вывод работают в USDT в сети TON.\n"
                        "2. Если есть активная заявка на вывод, новую создать нельзя.\n"
                        "3. История показывает и пополнения, и выводы в одном разделе."
                    ),
                ],
                separate_blocks=True,
            )
            rows = [
                [self._knowledge_button(topic="guide"), self._knowledge_button(topic="shops")],
                [self._knowledge_button(topic="listings")],
                [_button("↩️ К балансу", action="balance")],
            ]
        return FlowResult(effects=(ReplaceText(text=text, buttons=_rows(rows), parse_mode="HTML"),))

    def start_shop_create_token_prompt(self, *, seller_user_id: int) -> FlowResult:
        return FlowResult(
            effects=(
                SetPrompt(
                    prompt_type="seller_shop_create_token",
                    sensitive=True,
                    role=_ROLE_SELLER,
                    data={"seller_user_id": seller_user_id, "notify_sensitive_delete": False},
                ),
                ReplaceText(
                    text=self.shop_token_instruction_text(),
                    buttons=self._seller_back_buttons(action="shops", label="↩️ К магазинам"),
                    parse_mode="HTML",
                ),
            )
        )

    async def submit_shop_create_token(self, *, prompt_state: dict[str, Any], text: str) -> FlowResult:
        seller_user_id = int(prompt_state.get("seller_user_id", 0))
        wb_token = text.strip()
        if seller_user_id < 1:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(text="Не удалось продолжить создание магазина. Откройте раздел «🏪 Магазины» заново."),
                )
            )
        if not wb_token:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Токен не может быть пустым. Повторите ввод.",
                        buttons=self._seller_back_buttons(action="shops", label="↩️ К магазинам"),
                        parse_mode=None,
                    ),
                )
            )
        if self._wb_ping_client is None:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(text="Проверка токена временно недоступна. Попробуйте позже.", parse_mode=None),
                )
            )
        try:
            ping_result = await self._wb_ping_client.validate_token(wb_token)
        except Exception as exc:
            return FlowResult(
                effects=(
                    LogEvent(
                        event_name="seller_shop_create_token_validation_failed",
                        fields={"seller_user_id": seller_user_id, "error_type": type(exc).__name__},
                    ),
                    ClearPrompt(),
                    ReplyText(
                        text="Не удалось проверить токен. Попробуйте снова через раздел «🏪 Магазины».",
                        buttons=self._seller_back_buttons(action="shops", label="↩️ К магазинам"),
                        parse_mode=None,
                    ),
                )
            )
        if not ping_result.valid:
            details = ping_result.message or "неизвестная ошибка"
            return FlowResult(
                effects=(
                    ReplyText(
                        text=(
                            "Токен не прошел проверку и не сохранен.\n"
                            f"Причина: {details}\n"
                            "Проверьте, что токен «Базовый», работает в режиме "
                            "«Только для чтения» и у него есть категории "
                            "«Контент», «Статистика», «Вопросы и отзывы», "
                            "затем отправьте его снова."
                        ),
                        buttons=self._seller_back_buttons(action="shops", label="↩️ К магазинам"),
                        parse_mode=None,
                    ),
                )
            )

        token_ciphertext = encrypt_token(wb_token, self._config.token_cipher_key)
        return FlowResult(
            effects=(
                SetPrompt(
                    prompt_type="seller_shop_title_after_token",
                    sensitive=False,
                    role=_ROLE_SELLER,
                    data={"seller_user_id": seller_user_id, "validated_token_ciphertext": token_ciphertext},
                ),
                ReplyText(
                    text=(
                        "Токен валиден. Сообщение с токеном удалено в целях безопасности.\n\n"
                        "Шаг 2/2: введите название магазина следующим сообщением.\n"
                        "Название увидят покупатели, поэтому используйте нейтральное и понятное имя "
                        "без брендов и внутренних пометок."
                    ),
                    buttons=self._seller_back_buttons(action="shops", label="↩️ К магазинам"),
                    parse_mode=None,
                ),
            )
        )

    async def submit_shop_title_after_token(self, *, prompt_state: dict[str, Any], text: str) -> FlowResult:
        seller_user_id = int(prompt_state.get("seller_user_id", 0))
        token_ciphertext = str(prompt_state.get("validated_token_ciphertext", "")).strip()
        if seller_user_id < 1 or not token_ciphertext:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(
                        text="Не удалось продолжить создание магазина. Начните заново из раздела «🏪 Магазины».",
                        buttons=self._seller_back_buttons(action="shops", label="↩️ К магазинам"),
                        parse_mode=None,
                    ),
                )
            )
        try:
            shop = await self._seller_service.create_shop(seller_user_id=seller_user_id, title=text)
            await self._seller_service.save_validated_shop_token(
                seller_user_id=seller_user_id,
                shop_id=shop.shop_id,
                token_ciphertext=token_ciphertext,
            )
        except ValueError:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Название магазина не может быть пустым. Повторите ввод.",
                        buttons=self._seller_back_buttons(action="shops", label="↩️ К магазинам"),
                        parse_mode=None,
                    ),
                )
            )
        except InvalidStateError as exc:
            details = str(exc).strip().lower()
            if "title" in details and ("exists" in details or "unique" in details):
                error_text = "Магазин с таким названием уже есть.\nВведите другое название."
            else:
                error_text = "Не удалось создать магазин.\nПроверьте название и попробуйте еще раз."
            return FlowResult(
                effects=(
                    ReplyText(
                        text=error_text,
                        buttons=self._seller_back_buttons(action="shops", label="↩️ К магазинам"),
                        parse_mode=None,
                    ),
                )
            )

        deep_link = build_shop_deep_link(bot_username=self._config.telegram_bot_username, slug=shop.slug)
        return FlowResult(
            effects=(
                ClearPrompt(),
                ReplyText(
                    text=f"Магазин «{shop.title}» создан.\nСсылка для покупателей:\n{deep_link}",
                    buttons=self._seller_shop_detail_buttons(shop_id=shop.shop_id, token_is_valid=True),
                    parse_mode=None,
                ),
            )
        )

    async def render_shops(self, *, seller_user_id: int, notice: str | None = None) -> FlowResult:
        shops = await self._seller_service.list_shops(seller_user_id=seller_user_id)
        if not shops:
            lines = ["Магазинов пока нет."]
            if notice:
                lines.insert(0, html.escape(notice))
            return FlowResult(
                effects=(
                    ReplaceText(
                        text=screen_text(
                            title="Магазины",
                            lines=lines,
                            note="Нажмите «➕ Создать магазин», чтобы добавить первый магазин.",
                        ),
                        buttons=self._seller_shops_menu_buttons(has_shops=False),
                        parse_mode="HTML",
                    ),
                )
            )

        lines = ["Выберите магазин."]
        if notice:
            lines.insert(0, html.escape(notice))
        keyboard_rows = [
            [
                ButtonSpec(
                    text=f"🏬 {shop.title} · {format_shop_ref(shop.shop_id)}",
                    flow=_ROLE_SELLER,
                    action="shop_open",
                    entity_id=str(shop.shop_id),
                )
            ]
            for shop in shops
        ]
        keyboard_rows.extend(self._seller_shops_menu_buttons(has_shops=True))
        return FlowResult(
            effects=(
                ReplaceText(
                    text=screen_text(title="Магазины", lines=lines),
                    buttons=_rows(keyboard_rows),
                    parse_mode="HTML",
                ),
            )
        )

    async def render_shop_details(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
        notice: str | None = None,
        reply: bool = False,
    ) -> FlowResult:
        try:
            shop = await self._seller_service.get_shop(seller_user_id=seller_user_id, shop_id=shop_id)
        except NotFoundError:
            result = await self.render_shops(
                seller_user_id=seller_user_id,
                notice="Магазин не найден или уже удален.",
            )
            return _as_reply(result) if reply else result

        deep_link = build_shop_deep_link(bot_username=self._config.telegram_bot_username, slug=shop.slug)
        lines = [
            f"<b>Название:</b> {html.escape(shop.title)}",
            f"<b>Ссылка для покупателей:</b>\n{html.escape(deep_link)}",
            (
                "<b>Токен WB API:</b> активно"
                if _is_valid_shop_token(shop.wb_token_status)
                else "<b>Токен WB API:</b> неактивно"
            ),
        ]
        if notice:
            lines.insert(0, html.escape(notice))
        effect_type = ReplyText if reply else ReplaceText
        return FlowResult(
            effects=(
                effect_type(
                    text=screen_text(
                        title=f"Магазин «{html.escape(shop.title)}»",
                        title_suffix_html=title_ref_suffix(format_shop_ref(shop.shop_id)),
                        lines=lines,
                        note="Название магазина видят покупатели.",
                    ),
                    buttons=self._seller_shop_detail_buttons(
                        shop_id=shop_id,
                        token_is_valid=_is_valid_shop_token(shop.wb_token_status),
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    async def start_shop_rename_prompt(self, *, seller_user_id: int, shop_id: int | None) -> FlowResult:
        if shop_id is None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось выбрать магазин для переименования. Попробуйте еще раз.",
                        buttons=self._seller_shops_menu_buttons(has_shops=True),
                        parse_mode=None,
                    ),
                )
            )
        try:
            shop = await self._seller_service.get_shop(seller_user_id=seller_user_id, shop_id=shop_id)
        except NotFoundError:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Магазин не найден.",
                        buttons=self._seller_shops_menu_buttons(has_shops=True),
                        parse_mode=None,
                    ),
                )
            )
        token_is_valid = _is_valid_shop_token(shop.wb_token_status)
        return FlowResult(
            effects=(
                SetPrompt(
                    prompt_type="seller_shop_rename",
                    sensitive=False,
                    role=_ROLE_SELLER,
                    data={"shop_id": shop_id, "seller_user_id": seller_user_id, "token_is_valid": token_is_valid},
                ),
                ReplaceText(
                    text=screen_text(
                        title=f"Переименование магазина «{html.escape(shop.title)}»",
                        cta="Введите новое название магазина следующим сообщением ниже.",
                        lines=[
                            "При переименовании ссылка магазина изменится, старая перестанет работать.",
                            "Название видят покупатели, поэтому используйте нейтральное и понятное имя.",
                        ],
                        warning=True,
                    ),
                    buttons=self._seller_shop_detail_buttons(shop_id=shop_id, token_is_valid=token_is_valid),
                    parse_mode="HTML",
                ),
            )
        )

    async def submit_shop_rename(self, *, prompt_state: dict[str, Any], text: str) -> FlowResult:
        seller_user_id = int(prompt_state.get("seller_user_id", 0))
        shop_id = int(prompt_state.get("shop_id", 0))
        token_is_valid = bool(prompt_state.get("token_is_valid", False))
        if seller_user_id < 1 or shop_id < 1:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(
                        text="Не удалось продолжить переименование. Откройте магазины заново.",
                        buttons=self._seller_shops_menu_buttons(has_shops=True),
                        parse_mode=None,
                    ),
                )
            )
        try:
            shop = await self._seller_service.rename_shop(seller_user_id=seller_user_id, shop_id=shop_id, title=text)
        except ValueError:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Название магазина не может быть пустым. Повторите ввод.",
                        buttons=self._seller_shop_detail_buttons(shop_id=shop_id, token_is_valid=token_is_valid),
                        parse_mode=None,
                    ),
                )
            )
        except (NotFoundError, InvalidStateError) as exc:
            details = str(exc).strip().lower()
            if "title" in details and ("exists" in details or "unique" in details):
                error_text = "Магазин с таким названием уже существует.\nВведите другое название."
            else:
                error_text = "Не удалось переименовать магазин.\nПроверьте название и попробуйте еще раз."
            return FlowResult(
                effects=(
                    ReplyText(
                        text=error_text,
                        buttons=self._seller_shop_detail_buttons(shop_id=shop_id, token_is_valid=token_is_valid),
                        parse_mode=None,
                    ),
                )
            )

        deep_link = build_shop_deep_link(bot_username=self._config.telegram_bot_username, slug=shop.slug)
        return FlowResult(
            effects=(
                ClearPrompt(),
                ReplyText(
                    text=f"Магазин переименован: «{shop.title}».\nНовая ссылка для покупателей:\n{deep_link}",
                    buttons=self._seller_shop_detail_buttons(
                        shop_id=shop_id,
                        token_is_valid=_is_valid_shop_token(shop.wb_token_status),
                    ),
                    parse_mode=None,
                ),
            )
        )

    async def start_shop_token_prompt(self, *, seller_user_id: int, shop_id: int | None) -> FlowResult:
        if shop_id is None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось открыть настройки токена. Попробуйте еще раз.",
                        buttons=self._seller_shops_menu_buttons(has_shops=True),
                        parse_mode=None,
                    ),
                )
            )
        try:
            shop = await self._seller_service.get_shop(seller_user_id=seller_user_id, shop_id=shop_id)
        except NotFoundError:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Магазин не найден.",
                        buttons=self._seller_shops_menu_buttons(has_shops=True),
                        parse_mode=None,
                    ),
                )
            )
        return FlowResult(
            effects=(
                SetPrompt(
                    prompt_type="seller_shop_token",
                    sensitive=True,
                    role=_ROLE_SELLER,
                    data={"shop_id": shop_id, "seller_user_id": seller_user_id, "notify_sensitive_delete": False},
                ),
                ReplaceText(
                    text=self.shop_token_instruction_text(shop_title=shop.title),
                    buttons=self._seller_shop_detail_buttons(
                        shop_id=shop_id,
                        token_is_valid=_is_valid_shop_token(shop.wb_token_status),
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    async def submit_shop_token(self, *, prompt_state: dict[str, Any], text: str) -> FlowResult:
        shop_id = int(prompt_state.get("shop_id", 0))
        seller_user_id = int(prompt_state.get("seller_user_id", 0))
        if shop_id < 1:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(text="Не удалось продолжить ввод токена. Откройте карточку магазина заново."),
                )
            )
        if self._wb_ping_client is None:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(
                        text="Проверка токена временно недоступна. Попробуйте позже.",
                        parse_mode=None,
                    ),
                )
            )
        try:
            ping_result = await self._wb_ping_client.validate_token(text.strip())
            if not ping_result.valid:
                details = ping_result.message or "неизвестная ошибка"
                notice = (
                    "Токен не принят.\n"
                    f"Проверка ping завершилась ошибкой: {details}\n"
                    "Токен не сохранен. Проверьте доступы «Статистика» и «Контент» "
                    "и отправьте корректный токен."
                )
            else:
                token_ciphertext = encrypt_token(text.strip(), self._config.token_cipher_key)
                await self._seller_service.save_validated_shop_token(
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                    token_ciphertext=token_ciphertext,
                )
                notice = "Токен валиден и сохранен. Сообщение с токеном удалено в целях безопасности."
        except Exception as exc:
            return FlowResult(
                effects=(
                    LogEvent(
                        event_name="seller_shop_token_update_failed",
                        fields={"seller_user_id": seller_user_id, "shop_id": shop_id, "error_type": type(exc).__name__},
                    ),
                    ClearPrompt(),
                    *(
                        await self.render_shop_details(
                            seller_user_id=seller_user_id,
                            shop_id=shop_id,
                            notice=(
                                "Не удалось проверить или сохранить токен. "
                                "Попробуйте снова через карточку магазина."
                            ),
                            reply=True,
                        )
                    ).effects,
                )
            )
        details = await self.render_shop_details(
            seller_user_id=seller_user_id,
            shop_id=shop_id,
            notice=notice,
            reply=True,
        )
        return FlowResult(effects=(ClearPrompt(), *details.effects))

    async def render_shop_delete_preview(self, *, seller_user_id: int, shop_id: int | None) -> FlowResult:
        if shop_id is None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось выбрать магазин для удаления. Попробуйте еще раз.",
                        buttons=self._seller_shops_menu_buttons(has_shops=True),
                        parse_mode=None,
                    ),
                )
            )
        try:
            shop = await self._seller_service.get_shop(seller_user_id=seller_user_id, shop_id=shop_id)
            preview = await self._seller_service.get_shop_delete_preview(seller_user_id=seller_user_id, shop_id=shop_id)
        except NotFoundError:
            return await self.render_shops(seller_user_id=seller_user_id, notice="Магазин не найден или уже удален.")

        return FlowResult(
            effects=(
                ReplaceText(
                    text=screen_text(
                        title=f"Удаление магазина «{html.escape(shop.title)}» необратимо",
                        lines=[
                            f"Активных объявлений: {preview.active_listings_count}",
                            f"Незавершенных покупок: {preview.open_assignments_count}",
                            "Покупателям будет выплачен кэшбэк: "
                            f"{self._format_usdt_with_rub(preview.assignment_linked_reserved_usdt)}",
                            f"Продавцу вернется: {self._format_usdt_with_rub(preview.unassigned_collateral_usdt)}",
                        ],
                        note="При удалении магазина незавершенные покупки закроются с выплатой кэшбэка покупателям.",
                        warning=True,
                    ),
                    buttons=_rows(
                        [
                            [_button("✅ Подтвердить удаление", action="shop_delete_confirm", entity_id=shop_id)],
                            [_button("↩️ Отмена", action="shop_open", entity_id=shop_id)],
                        ]
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    async def execute_shop_delete(self, *, seller_user_id: int, shop_id: int | None) -> FlowResult:
        if shop_id is None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось выбрать магазин для удаления. Попробуйте еще раз.",
                        buttons=self._seller_shops_menu_buttons(has_shops=True),
                        parse_mode=None,
                    ),
                )
            )
        try:
            result = await self._seller_service.delete_shop(
                seller_user_id=seller_user_id,
                shop_id=shop_id,
                deleted_by_user_id=seller_user_id,
                idempotency_key=f"tg-shop-delete:{seller_user_id}:{shop_id}",
            )
        except NotFoundError:
            return await self.render_shops(seller_user_id=seller_user_id, notice="Магазин не найден или уже удален.")
        if not result.changed:
            message = "Магазин уже удален."
        else:
            message = (
                "Магазин удален.\n"
                f"Покупателям ушло: {self._format_usdt_with_rub(result.assignment_transferred_usdt)}\n"
                f"Продавцу вернулось: {self._format_usdt_with_rub(result.unassigned_collateral_returned_usdt)}"
            )
        shops = await self.render_shops(seller_user_id=seller_user_id, notice=message)
        return FlowResult(
            effects=(
                LogEvent(
                    event_name="seller_shop_deleted",
                    fields={
                        "shop_id": shop_id,
                        "shop_ref": format_shop_ref(shop_id),
                        "assignment_transferred_usdt": str(result.assignment_transferred_usdt),
                        "unassigned_collateral_returned_usdt": str(result.unassigned_collateral_returned_usdt),
                    },
                ),
                *shops.effects,
            )
        )

    async def render_listings(
        self,
        *,
        seller_user_id: int,
        page: int = 1,
        notice: str | None = None,
    ) -> FlowResult:
        listings = await self._seller_service.list_listing_collateral_views(seller_user_id=seller_user_id)
        if not listings:
            lines = ["Объявлений пока нет."]
            if notice:
                lines.insert(0, html.escape(notice))
            return FlowResult(
                effects=(
                    ReplaceText(
                        text=screen_text(
                            title="Объявления",
                            lines=lines,
                            note="Нажмите «➕ Создать объявление», чтобы добавить первое объявление.",
                        ),
                        buttons=_rows(
                            [
                                [_button("➕ Создать объявление", action="listing_create_pick_shop")],
                                [_button("↩️ Назад", action="menu")],
                            ]
                        ),
                        parse_mode="HTML",
                    ),
                )
            )

        balance_snapshot = await self._seller_service.get_seller_balance_snapshot(seller_user_id=seller_user_id)
        resolved_page, total_pages, start_index, end_index = resolve_numbered_page(
            total_items=len(listings),
            requested_page=page,
        )
        page_items = listings[start_index:end_index]
        lines = []
        if notice:
            lines.append(html.escape(notice))
        for number, listing in enumerate(page_items, start=start_index + 1):
            display_title = listing_display_title(display_title=listing.display_title, fallback=listing.search_phrase)
            listing_link = build_listing_deep_link(
                bot_username=self._config.telegram_bot_username,
                listing_id=listing.listing_id,
            )
            collateral_line = self._format_listing_collateral_line(
                collateral_view=listing,
                seller_available_usdt=balance_snapshot.seller_available_usdt,
            )
            lines.append(
                f"<b>{number}. {html.escape(display_title)}</b>\n"
                f"<b>Артикул WB:</b> {listing.wb_product_id}\n"
                "<b>Кэшбэк:</b> "
                f"{self._format_cashback_with_percent(
                    reward_usdt=listing.reward_usdt,
                    reference_price_rub=listing.reference_price_rub,
                )}\n"
                f"<b>Поисковая фраза:</b> &quot;{html.escape(listing.search_phrase)}&quot;\n"
                f"<b>План покупок / В процессе:</b> {listing.slot_count} / {listing.in_progress_assignments_count}\n"
                f"<b>Ссылка на товар:</b> {html.escape(listing_link)}\n"
                f"<b>Обеспечение:</b> {collateral_line}\n"
                f"<b>Статус:</b> {self._listing_activity_badge(is_active=listing.status == 'active')}"
            )
        title = "Объявления"
        if total_pages > 1:
            title = f"Объявления · стр. {resolved_page}/{total_pages}"
        return FlowResult(
            effects=(
                SetUserData(key=self._config.seller_listings_page_key, value=resolved_page),
                ReplaceText(
                    text=screen_text(
                        title=title,
                        cta="Нажмите номер ниже, чтобы открыть карточку объявления.",
                        lines=lines,
                        note="Новое объявление создается кнопкой ниже.",
                        separate_blocks=True,
                    ),
                    buttons=numbered_page_buttons(
                        flow=_ROLE_SELLER,
                        open_action="listing_open",
                        page_action="listings",
                        item_ids=[item.listing_id for item in page_items],
                        start_number=start_index + 1,
                        page=resolved_page,
                        total_pages=total_pages,
                        extra_rows=[
                            [_button("➕ Создать объявление", action="listing_create_pick_shop")],
                            [_button("↩️ Назад", action="menu")],
                            [self._knowledge_button(topic="listings")],
                        ],
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    async def render_listing_detail(
        self,
        *,
        seller_user_id: int,
        listing_id: int | None,
        list_page: int = 1,
        notice: str | None = None,
    ) -> FlowResult:
        if listing_id is None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось открыть объявление. Попробуйте еще раз.",
                        buttons=self._seller_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        try:
            listing = await self._seller_service.get_listing(seller_user_id=seller_user_id, listing_id=listing_id)
        except NotFoundError:
            return await self.render_listings(
                seller_user_id=seller_user_id,
                page=list_page,
                notice="Объявление не найдено или уже удалено.",
            )
        views = await self._seller_service.list_listing_collateral_views(seller_user_id=seller_user_id)
        balance_snapshot = await self._seller_service.get_seller_balance_snapshot(seller_user_id=seller_user_id)
        collateral_view = next((item for item in views if item.listing_id == listing_id), None)
        return FlowResult(
            effects=(
                ReplyPhoto(photo_url=listing.wb_photo_url),
                ReplaceText(
                    text=self._seller_listing_detail_html(
                        listing=listing,
                        collateral_view=collateral_view,
                        seller_available_usdt=balance_snapshot.seller_available_usdt,
                        listing_link=build_listing_deep_link(
                            bot_username=self._config.telegram_bot_username,
                            listing_id=listing.listing_id,
                        ),
                        notice=notice,
                    ),
                    buttons=self._seller_listing_detail_buttons(
                        listing_id=listing.listing_id,
                        status=listing.status,
                        list_page=list_page,
                        can_activate=self._listing_has_sufficient_collateral(
                            collateral_view=collateral_view,
                            seller_available_usdt=balance_snapshot.seller_available_usdt,
                            listing_status=listing.status,
                        ),
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    def render_listing_edit_disabled(self, *, list_page: int = 1) -> FlowResult:
        return FlowResult(
            effects=(
                ReplaceText(
                    text=screen_text(
                        title="Редактирование отключено",
                        lines=[
                            (
                                "Редактирование объявлений недоступно, чтобы не создавать конфликтов "
                                "с уже начатыми покупками."
                            ),
                        ],
                        note="Если нужно изменить параметры, создайте новое объявление и удалите старое.",
                        warning=True,
                    ),
                    buttons=_rows([[_button("↩️ К объявлениям", action="listings", entity_id=list_page)]]),
                    parse_mode="HTML",
                ),
            )
        )

    def render_listing_edit_field_disabled(self) -> FlowResult:
        return FlowResult(
            effects=(
                ClearPrompt(),
                ReplaceText(
                    text=screen_text(
                        title="Редактирование отключено",
                        lines=["Изменение объявления недоступно."],
                        note="Создайте новое объявление с нужными параметрами и удалите старое.",
                        warning=True,
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    async def render_listing_create_shop_picker(self, *, seller_user_id: int) -> FlowResult:
        shops = await self._seller_service.list_shops(seller_user_id=seller_user_id)
        if not shops:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Нет доступных магазинов. Сначала создайте магазин.",
                        buttons=self._seller_shops_menu_buttons(has_shops=False),
                        parse_mode=None,
                    ),
                )
            )
        listings = await self._seller_service.list_listing_collateral_views(seller_user_id=seller_user_id)
        listing_counts_by_shop: dict[int, int] = {}
        for listing in listings:
            listing_counts_by_shop[listing.shop_id] = listing_counts_by_shop.get(listing.shop_id, 0) + 1
        keyboard_rows = [
            [
                _button(
                    button_label_with_count(f"🏬 {shop.title}", listing_counts_by_shop.get(shop.shop_id, 0)),
                    action="listing_create_prompt",
                    entity_id=shop.shop_id,
                )
            ]
            for shop in shops
        ]
        keyboard_rows.append([_button("↩️ Назад к объявлениям", action="listings")])
        return FlowResult(
            effects=(
                ReplaceText(
                    text=screen_text(title="Новое объявление", cta="Выберите магазин для нового объявления."),
                    buttons=_rows(keyboard_rows),
                    parse_mode="HTML",
                ),
            )
        )

    async def start_listing_create_prompt(self, *, seller_user_id: int, shop_id: int | None) -> FlowResult:
        if shop_id is None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось выбрать магазин. Попробуйте еще раз.",
                        buttons=self._seller_shops_menu_buttons(has_shops=True),
                        parse_mode=None,
                    ),
                )
            )
        try:
            shop = await self._seller_service.get_shop(seller_user_id=seller_user_id, shop_id=shop_id)
        except NotFoundError:
            return await self.render_shops(seller_user_id=seller_user_id, notice="Магазин не найден или уже удален.")
        return self._listing_creation_flow.start_prompt(
            seller_user_id=seller_user_id,
            shop_id=shop_id,
            shop_title=shop.title,
        )

    async def execute_listing_activate(
        self,
        *,
        seller_user_id: int,
        listing_id: int | None,
        list_page: int = 1,
    ) -> FlowResult:
        if listing_id is None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось определить объявление. Нажмите кнопку еще раз.",
                        buttons=self._seller_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        try:
            workflow = self._seller_workflow
            if workflow is None:
                listing = await self._seller_service.get_listing(seller_user_id=seller_user_id, listing_id=listing_id)
                if self._listing_product_validator is not None:
                    await self._listing_product_validator(
                        seller_user_id=seller_user_id,
                        shop_id=listing.shop_id,
                        wb_product_id=listing.wb_product_id,
                    )
                result = await self._seller_service.activate_listing(
                    seller_user_id=seller_user_id,
                    listing_id=listing_id,
                    idempotency_key=f"tg-listing-activate:{seller_user_id}:{listing_id}",
                )
            else:
                result = await workflow.activate_listing(
                    seller_user_id=seller_user_id,
                    listing_id=listing_id,
                    idempotency_key=f"tg-listing-activate:{seller_user_id}:{listing_id}",
                )
        except NotFoundError:
            return FlowResult(effects=(ReplaceText(text="Объявление не найдено.", parse_mode=None),))
        except ListingValidationError as exc:
            return FlowResult(effects=(ReplaceText(text=str(exc), parse_mode=None),))
        except InvalidStateError:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось активировать объявление. Проверьте токен магазина и обеспечение.",
                        parse_mode=None,
                    ),
                )
            )
        except InsufficientFundsError:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text=screen_text(
                            title="Недостаточно средств для активации",
                            lines=["На балансе не хватает средств, чтобы зарезервировать обеспечение."],
                            note="Пополните баланс и попробуйте снова.",
                            warning=True,
                        ),
                        buttons=_rows(
                            [
                                [_button("➕ Пополнить", action="topup_prompt")],
                                [_button("↩️ К карточке", action="listing_open", entity_id=listing_id)],
                            ]
                        ),
                        parse_mode="HTML",
                    ),
                )
            )
        detail = await self.render_listing_detail(
            seller_user_id=seller_user_id,
            listing_id=listing_id,
            list_page=list_page,
            notice="Объявление активно." if result.changed else "Объявление уже активно.",
        )
        return FlowResult(
            effects=(
                LogEvent(
                    event_name="seller_listing_activated",
                    fields={
                        "listing_id": listing_id,
                        "listing_ref": format_listing_ref(listing_id),
                        "changed": result.changed,
                    },
                ),
                *detail.effects,
            )
        )

    async def execute_listing_pause(
        self,
        *,
        seller_user_id: int,
        listing_id: int | None,
        list_page: int = 1,
    ) -> FlowResult:
        if listing_id is None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось определить объявление. Нажмите кнопку еще раз.",
                        buttons=self._seller_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        try:
            result = await self._seller_service.pause_listing(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
                reason="manual_pause",
            )
        except (NotFoundError, InvalidStateError):
            return FlowResult(effects=(ReplaceText(text="Не удалось поставить объявление на паузу.", parse_mode=None),))
        detail = await self.render_listing_detail(
            seller_user_id=seller_user_id,
            listing_id=listing_id,
            list_page=list_page,
            notice="Объявление поставлено на паузу." if result.changed else "Объявление уже на паузе.",
        )
        return FlowResult(
            effects=(
                LogEvent(
                    event_name="seller_listing_paused",
                    fields={
                        "listing_id": listing_id,
                        "listing_ref": format_listing_ref(listing_id),
                        "changed": result.changed,
                    },
                ),
                *detail.effects,
            )
        )

    async def execute_listing_unpause(
        self,
        *,
        seller_user_id: int,
        listing_id: int | None,
        list_page: int = 1,
    ) -> FlowResult:
        if listing_id is None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось определить объявление. Нажмите кнопку еще раз.",
                        buttons=self._seller_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        try:
            workflow = self._seller_workflow
            if workflow is None:
                listing = await self._seller_service.get_listing(seller_user_id=seller_user_id, listing_id=listing_id)
                if self._listing_product_validator is not None:
                    await self._listing_product_validator(
                        seller_user_id=seller_user_id,
                        shop_id=listing.shop_id,
                        wb_product_id=listing.wb_product_id,
                    )
                result = await self._seller_service.unpause_listing(
                    seller_user_id=seller_user_id,
                    listing_id=listing_id,
                )
            else:
                result = await workflow.unpause_listing(seller_user_id=seller_user_id, listing_id=listing_id)
        except NotFoundError:
            return FlowResult(effects=(ReplaceText(text="Объявление не найдено.", parse_mode=None),))
        except ListingValidationError as exc:
            return FlowResult(effects=(ReplaceText(text=str(exc), parse_mode=None),))
        except InvalidStateError:
            return FlowResult(effects=(ReplaceText(text="Не удалось снять паузу с объявления.", parse_mode=None),))
        detail = await self.render_listing_detail(
            seller_user_id=seller_user_id,
            listing_id=listing_id,
            list_page=list_page,
            notice="Объявление снова активно." if result.changed else "Объявление уже активно.",
        )
        return FlowResult(
            effects=(
                LogEvent(
                    event_name="seller_listing_unpaused",
                    fields={
                        "listing_id": listing_id,
                        "listing_ref": format_listing_ref(listing_id),
                        "changed": result.changed,
                    },
                ),
                *detail.effects,
            )
        )

    async def render_listing_delete_preview(self, *, seller_user_id: int, listing_id: int | None) -> FlowResult:
        if listing_id is None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось определить объявление. Нажмите кнопку еще раз.",
                        buttons=self._seller_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        try:
            preview = await self._seller_service.get_listing_delete_preview(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
            )
        except NotFoundError:
            return FlowResult(effects=(ReplaceText(text="Объявление не найдено.", parse_mode=None),))
        return FlowResult(
            effects=(
                ReplaceText(
                    text=screen_text(
                        title="Удаление объявления необратимо",
                        lines=[
                            f"Незавершенных покупок: {preview.open_assignments_count}",
                            "Покупателям будет выплачен кэшбэк: "
                            f"{self._format_usdt_with_rub(preview.assignment_linked_reserved_usdt)}",
                            f"Продавцу вернется: {self._format_usdt_with_rub(preview.unassigned_collateral_usdt)}",
                        ],
                        note="При удалении объявления незавершенные покупки закроются с выплатой кэшбэка покупателям.",
                        warning=True,
                    ),
                    buttons=_rows(
                        [
                            [_button("✅ Подтвердить удаление", action="listing_delete_confirm", entity_id=listing_id)],
                            [_button("↩️ Отмена", action="listing_open", entity_id=listing_id)],
                        ]
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    async def execute_listing_delete(
        self,
        *,
        seller_user_id: int,
        listing_id: int | None,
        list_page: int = 1,
    ) -> FlowResult:
        if listing_id is None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось определить объявление. Нажмите кнопку еще раз.",
                        buttons=self._seller_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        try:
            result = await self._seller_service.delete_listing(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
                deleted_by_user_id=seller_user_id,
                idempotency_key=f"tg-listing-delete:{seller_user_id}:{listing_id}",
            )
        except NotFoundError:
            return FlowResult(effects=(ReplaceText(text="Объявление не найдено.", parse_mode=None),))
        if not result.changed:
            message = "Объявление уже удалено."
        else:
            message = (
                "Объявление удалено.\n"
                f"Покупателям ушло: {self._format_usdt_with_rub(result.assignment_transferred_usdt)}\n"
                f"Продавцу вернулось: {self._format_usdt_with_rub(result.unassigned_collateral_returned_usdt)}"
            )
        listings = await self.render_listings(seller_user_id=seller_user_id, page=list_page, notice=message)
        return FlowResult(
            effects=(
                LogEvent(
                    event_name="seller_listing_deleted",
                    fields={
                        "listing_id": listing_id,
                        "listing_ref": format_listing_ref(listing_id),
                        "assignment_transferred_usdt": str(result.assignment_transferred_usdt),
                        "unassigned_collateral_returned_usdt": str(result.unassigned_collateral_returned_usdt),
                    },
                ),
                *listings.effects,
            )
        )

    async def render_balance(self, *, seller_user_id: int) -> FlowResult:
        snapshot = await self._seller_service.get_seller_balance_snapshot(seller_user_id=seller_user_id)
        active_request = await self._finance_service.get_active_seller_withdrawal_request(
            seller_user_id=seller_user_id
        )
        listings = await self._seller_service.list_listing_collateral_views(seller_user_id=seller_user_id)
        allocated_total = snapshot.seller_collateral_usdt
        required_total = sum((item.collateral_required_usdt for item in listings), Decimal("0"))
        activation_capacity = snapshot.seller_available_usdt + snapshot.seller_collateral_usdt
        shortfall = required_total - activation_capacity
        lines = [
            f"<b>Свободно для новых объявлений:</b> {self._format_usdt_with_rub(snapshot.seller_available_usdt)}",
            f"<b>Уже выделено под объявления:</b> {self._format_usdt_with_rub(allocated_total)}",
            f"<b>В процессе вывода:</b> {self._format_usdt_with_rub(snapshot.seller_withdraw_pending_usdt)}",
        ]
        if active_request is not None:
            lines.append(withdrawal_request_block_html(active_request))
        if shortfall > Decimal("0.000000"):
            lines.append(f"<b>Не хватает для активации:</b> {self._format_usdt_with_rub(shortfall)}")
        return FlowResult(
            effects=(
                ReplaceText(
                    text=screen_text(
                        title="Баланс продавца",
                        cta="Выберите следующее действие ниже.",
                        lines=lines,
                        note=(
                            "Пополните баланс продавца, если средств не хватает для активации объявлений."
                            if shortfall > Decimal("0.000000")
                            else None
                        ),
                        separate_blocks=True,
                    ),
                    buttons=self._seller_balance_menu_buttons(
                        can_withdraw_available=(
                            active_request is None and snapshot.seller_available_usdt > Decimal("0.000000")
                        ),
                        active_request_id=(
                            active_request.withdrawal_request_id if active_request is not None else None
                        ),
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    def start_topup_prompt(self, *, seller_user_id: int) -> FlowResult:
        return FlowResult(
            effects=(
                SetPrompt(
                    prompt_type="seller_topup_amount",
                    sensitive=False,
                    role=_ROLE_SELLER,
                    data={"seller_user_id": seller_user_id},
                ),
                ReplaceText(
                    text=(
                        "Введите сумму пополнения в USDT (например, 1.2).\n"
                        "Бот автоматически рассчитает точную сумму для перевода."
                    ),
                    buttons=_rows(
                        [
                            [_button("❓ Как перевести?", action="topup_help")],
                            *self._seller_balance_menu_buttons(),
                        ]
                    ),
                    parse_mode=None,
                ),
            )
        )

    async def submit_topup_amount(self, *, prompt_state: dict[str, Any], text: str, update_id: int) -> FlowResult:
        seller_user_id = int(prompt_state.get("seller_user_id", 0))
        if seller_user_id < 1:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(
                        text="Не удалось продолжить пополнение. Откройте раздел «💰 Баланс» заново.",
                        buttons=self._seller_balance_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        try:
            amount = Decimal(text)
        except InvalidOperation:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Неверный формат суммы. Введите число, например 1.2.",
                        buttons=self._seller_balance_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        if amount <= Decimal("0.000000"):
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Сумма должна быть больше 0.",
                        buttons=self._seller_balance_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )

        shards = await self._deposit_service.list_active_shards()
        if not shards:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Адрес для оплаты временно недоступен. Попробуйте позже.",
                        buttons=self._seller_balance_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        target_shard = next(
            (shard for shard in shards if shard.shard_key == self._config.seller_collateral_shard_key),
            shards[0],
        )
        try:
            intent = await self._deposit_service.create_seller_deposit_intent(
                seller_user_id=seller_user_id,
                request_amount_usdt=amount,
                shard_id=target_shard.shard_id,
                idempotency_key=f"tg-seller-topup:{seller_user_id}:{update_id}",
            )
        except (NotFoundError, InvalidStateError, ValueError) as exc:
            details = str(exc).strip()
            if "all 999 suffixes" in details:
                text_out = "Сейчас нельзя создать новый счет: достигнут лимит активных счетов.\nПопробуйте позже."
            else:
                text_out = "Не удалось создать счет на пополнение. Попробуйте еще раз."
            return FlowResult(
                effects=(ReplyText(text=text_out, buttons=self._seller_balance_menu_buttons(), parse_mode=None),)
            )
        except Exception as exc:
            return FlowResult(
                effects=(
                    LogEvent(
                        event_name="seller_topup_intent_create_failed",
                        fields={
                            "seller_user_id": seller_user_id,
                            "telegram_update_id": update_id,
                            "error_type": type(exc).__name__,
                        },
                    ),
                    ReplyText(
                        text="Техническая ошибка при создании счета. Попробуйте еще раз.",
                        buttons=self._seller_balance_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        expected_amount_text = format_copyable_code(
            f"{format_usdt_value(intent.expected_amount_usdt, precise=True)} USDT"
        )
        return FlowResult(
            effects=(
                ClearPrompt(),
                ReplyText(
                    text=screen_text(
                        title="Счет на пополнение создан",
                        title_suffix_html=title_ref_suffix(format_deposit_ref(intent.deposit_intent_id)),
                        cta=(
                            "Откройте Телеграм Кошелек или используйте ссылку для других "
                            "кошельков, либо скопируйте адрес и сумму вручную."
                        ),
                        lines=[
                            f"<b>Срок действия:</b> {self._config.seller_collateral_invoice_ttl_hours} ч",
                            "<b>Сеть:</b> USDT в сети TON (не ERC-20)",
                            f"<b>Адрес:</b> {format_copyable_code(intent.deposit_address)}",
                            f"<b>Сумма (должна полностью совпадать):</b> {expected_amount_text}",
                        ],
                        note=(
                            "Телеграм Кошелек откроется без автоматически подставленного перевода. "
                            "Ссылка для других кошельков может открыть уже подготовленный перевод. "
                            "В любом случае адрес и сумму можно скопировать вручную."
                        ),
                    ),
                    buttons=_rows(
                        [
                            [ButtonSpec(text="👛 Открыть Телеграм Кошелек", url=self._config.telegram_wallet_open_url)],
                            [
                                ButtonSpec(
                                    text="🔗 Ссылка (другие кошельки)",
                                    url=self._build_ton_usdt_wallet_link(
                                        destination_address=intent.deposit_address,
                                        expected_amount_usdt=intent.expected_amount_usdt,
                                        text=f"QPI deposit {format_deposit_ref(intent.deposit_intent_id)}",
                                    ),
                                )
                            ],
                            [_button("❓ Как перевести?", action="topup_help")],
                            *self._seller_balance_menu_buttons(),
                        ]
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    async def render_transaction_history(self, *, seller_user_id: int, page: int = 1) -> FlowResult:
        intents = await self._deposit_service.list_seller_deposit_intents(seller_user_id=seller_user_id, limit=1000)
        withdrawals = await self._finance_service.list_seller_withdrawal_history(
            seller_user_id=seller_user_id,
            limit=1000,
        )
        combined_history: list[tuple[str, datetime, int, Any]] = []
        for item in intents:
            combined_history.append(("topup", item.created_at, int(getattr(item, "deposit_intent_id", 0) or 0), item))
        for item in withdrawals:
            combined_history.append(
                ("withdraw", item.requested_at, int(getattr(item, "withdrawal_request_id", 0) or 0), item)
            )
        combined_history.sort(key=lambda entry: (entry[1], entry[2]), reverse=True)

        if not combined_history:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text=screen_text(
                            title="Транзакции продавца",
                            cta="Здесь отображаются пополнения и выводы продавца.",
                            lines=["Транзакций пока нет."],
                            note="Нажмите «➕ Пополнить» или создайте заявку на вывод с экрана баланса.",
                        ),
                        buttons=self._seller_balance_menu_buttons(),
                        parse_mode="HTML",
                    ),
                )
            )

        resolved_page, total_pages, start_index, end_index = resolve_numbered_page(
            total_items=len(combined_history),
            requested_page=page,
            page_size=8,
        )
        lines: list[str] = []
        for entry_type, _, entry_id, item in combined_history[start_index:end_index]:
            if entry_type == "withdraw":
                lines.append(withdrawal_history_block_html(item))
                continue
            expected_amount = format_usdt_value(item.expected_amount_usdt, precise=True)
            block_lines = []
            if entry_id > 0:
                block_lines.append(
                    entity_block_heading_with_ref(label="Счет на пополнение", ref=format_deposit_ref(entry_id))
                )
            else:
                block_lines.append("<b>Пополнение</b>")
            block_lines.extend(
                [
                    f"<b>Сумма:</b> {expected_amount} USDT",
                    f"<b>Статус:</b> {self._deposit_status_badge(item.status)}",
                    f"<b>Создан:</b> {format_datetime_msk(item.created_at)}",
                    f"<b>Срок счета:</b> до {format_datetime_msk(item.expires_at)}",
                ]
            )
            block = "\n".join(block_lines)
            if item.status == "credited" and item.credited_amount_usdt is not None:
                block += f"\n<b>Зачислено:</b> {format_usdt_value(item.credited_amount_usdt, precise=True)} USDT"
            if item.status == "manual_review":
                block += "\n<i>Перевод найден, но нужна проверка администратором.</i>"
            if item.status == "expired":
                block += "\n<i>Если вы оплатили после срока, обратитесь к администратору.</i>"
            lines.append(block)

        keyboard_rows: list[list[ButtonSpec]] = []
        nav_row = page_nav_row(
            flow=_ROLE_SELLER,
            page_action="topup_history",
            page=resolved_page,
            total_pages=total_pages,
            previous_label="⬅️",
            next_label="➡️",
        )
        if nav_row:
            keyboard_rows.append(list(nav_row))
        keyboard_rows.extend(self._seller_balance_menu_buttons())
        return FlowResult(
            effects=(
                ReplaceText(
                    text=screen_text(
                        title=(
                            f"Транзакции продавца · стр. {resolved_page}/{total_pages}"
                            if total_pages > 1
                            else "Транзакции продавца"
                        ),
                        cta="Проверьте статус пополнений и выводов ниже.",
                        lines=lines,
                        note=(
                            "Если пополнение или вывод зависли, проверьте статус "
                            "и при необходимости обратитесь к администратору."
                        ),
                        separate_blocks=True,
                    ),
                    buttons=_rows(keyboard_rows),
                    parse_mode="HTML",
                ),
            )
        )

    def render_topup_help(self) -> FlowResult:
        return FlowResult(
            effects=(
                ReplaceText(
                    text=screen_text(
                        title="Как перевести USDT",
                        cta=(
                            "Следуйте шагам ниже, затем вернитесь к сообщению со счетом "
                            "и отправьте точную сумму в сети TON.\n"
                            "Рекомендуем делать перевод на несколько объявлений сразу, "
                            "так как комиссия за перевод в сети TON составляет "
                            "фиксированный 1 USDT."
                        ),
                        lines=[
                            (
                                '1. Зайдите в <a href="https://help.ru.wallet.tg/article/60-znakomstvo-s-wallet">'
                                "официальный кошелек Wallet</a> в Telegram: "
                                '<a href="https://t.me/wallet">@wallet</a>.\n'
                                "Также можно использовать любой другой TON-совместимый кошелек "
                                "или перевести USDT напрямую с криптобиржи."
                            ),
                            (
                                "2. Пополните Крипто Кошелек, купив необходимый объем USDT, "
                                'например на <a href="https://help.ru.wallet.tg/article/'
                                '80-kak-kupit-kriptovalutu-na-p2p-markete">'
                                "P2P Маркете</a>.\n"
                                "Самый простой и быстрый способ: "
                                "Крипто Кошелек > Пополнить > P2P Экспресс."
                            ),
                            (
                                "3. Выведите USDT на предоставленный в боте адрес:\n"
                                "Крипто Кошелек > Вывести > Внешний кошелек или биржа > "
                                "Доллары > Сеть TON."
                            ),
                        ],
                        note=(
                            "Важно точно указать сумму перевода, так как по ней "
                            "идентифицируется ваш платеж. Адрес и сумма остаются "
                            "в предыдущем сообщении со счетом."
                        ),
                        separate_blocks=True,
                    ),
                    buttons=_rows(
                        [
                            [_button("↩️ К балансу", action="balance")],
                            [_button("🧾 Транзакции", action="topup_history")],
                        ]
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    def shop_token_instruction_text(self, *, shop_title: str | None = None) -> str:
        title = f"Токен WB API для магазина «{html.escape(shop_title)}»" if shop_title else "Создание магазина"
        note = (
            "Сначала бот проверит токен, и только потом попросит название магазина."
            if shop_title is None
            else "Сообщение с токеном будет удалено автоматически."
        )
        return screen_text(
            title=title,
            cta="Отправьте токен WB API следующим сообщением ниже.",
            lines=[
                "<b>Шаг 1 из 2.</b>",
                (
                    "<b>Как создать:</b> Создайте Базовый токен в режиме "
                    "«Только для чтения» с категориями: Контент, Статистика, Вопросы и отзывы."
                ),
                "<b>Где найти:</b> ЛК ВБ -> Интеграции по API -> Создать токен -> Для интеграции вручную.",
                "<b>Зачем нужен токен:</b> для получения информации о товаре, проверки статуса заказов и отзывов.",
                "<b>Безопасно:</b> токен создается только в режиме чтения, поэтому изменить данные с ним невозможно.",
            ],
            note=note,
        )

    async def _load_seller_order_counters(self, *, seller_user_id: int) -> dict[str, int]:
        loader = getattr(self._seller_service, "get_seller_order_counters", None)
        if loader is None:
            return {"awaiting_order": 0, "ordered": 0, "picked_up": 0}
        return await loader(seller_user_id=seller_user_id)

    def menu_buttons(
        self,
        *,
        listings_count: int | None = None,
        shops_count: int | None = None,
    ) -> tuple[tuple[ButtonSpec, ...], ...]:
        return self._seller_menu_buttons(listings_count=listings_count, shops_count=shops_count)

    def _seller_menu_buttons(
        self,
        *,
        listings_count: int | None = None,
        shops_count: int | None = None,
    ) -> tuple[tuple[ButtonSpec, ...], ...]:
        keyboard = [
            [
                _button(button_label_with_count("📦 Объявления", listings_count), action="listings"),
                _button(button_label_with_count("🏬 Магазины", shops_count), action="shops"),
            ],
            [_button("💰 Баланс", action="balance")],
            [self._knowledge_button(topic="guide")],
        ]
        support = self._support_button()
        if support is not None:
            keyboard.append([support])
        return _rows(keyboard)

    @staticmethod
    def _seller_shops_menu_buttons(*, has_shops: bool) -> tuple[tuple[ButtonSpec, ...], ...]:
        return _rows(
            [
                [_button("➕ Создать магазин", action="shop_create_token_prompt")],
                [_button("↩️ Назад", action="menu")],
                [_knowledge_button(topic="shops")],
            ]
        )

    @staticmethod
    def _seller_back_buttons(*, action: str, label: str) -> tuple[tuple[ButtonSpec, ...], ...]:
        return _rows([[_button(label, action=action)]])

    @staticmethod
    def _seller_shop_detail_buttons(
        *,
        shop_id: int,
        token_is_valid: bool = False,
    ) -> tuple[tuple[ButtonSpec, ...], ...]:
        token_label = "✅ Токен WB API" if token_is_valid else "❌ Токен WB API"
        return _rows(
            [
                [_button(token_label, action="shop_token_prompt", entity_id=shop_id)],
                [
                    _button("✏️ Переименовать", action="shop_rename_prompt", entity_id=shop_id),
                    _button("🗑 Удалить", action="shop_delete_preview", entity_id=shop_id),
                ],
                [_button("↩️ К списку магазинов", action="shops")],
                [_knowledge_button(topic="shops")],
            ]
        )

    def _seller_listing_detail_buttons(
        self,
        *,
        listing_id: int,
        status: str,
        list_page: int,
        can_activate: bool,
    ) -> tuple[tuple[ButtonSpec, ...], ...]:
        if status == "draft" and can_activate:
            action_button = _button("✅ Активировать", action="listing_activate", entity_id=listing_id)
        elif status == "draft":
            action_button = _button(
                "⛔ Недостаточно средств",
                action="listing_activation_blocked",
                entity_id=listing_id,
            )
        elif status == "active":
            action_button = _button("⏸ Пауза", action="listing_pause", entity_id=listing_id)
        else:
            action_button = _button("▶️ Снять паузу", action="listing_unpause", entity_id=listing_id)
        return _rows(
            [
                [action_button],
                [_button("🗑 Удалить", action="listing_delete_preview", entity_id=listing_id)],
                [_button("↩️ Назад к объявлениям", action="listings", entity_id=list_page)],
                [self._knowledge_button(topic="listings")],
            ]
        )

    def _seller_balance_menu_buttons(
        self,
        *,
        can_withdraw_available: bool = False,
        active_request_id: int | None = None,
    ) -> tuple[tuple[ButtonSpec, ...], ...]:
        keyboard: list[list[ButtonSpec]] = [[_button("➕ Пополнить", action="topup_prompt")]]
        if active_request_id is not None:
            keyboard.append(
                [_button("🚫 Отменить заявку", action="withdraw_cancel_prompt", entity_id=active_request_id)]
            )
        elif can_withdraw_available:
            keyboard.extend(
                [
                    [_button("💸 Вывести все доступное", action="withdraw_full")],
                    [_button("✍️ Указать сумму вручную", action="withdraw_prompt_amount")],
                ]
            )
        keyboard.extend(
            [
                [_button("🧾 Транзакции", action="topup_history")],
                [_button("↩️ Назад", action="menu")],
                [self._knowledge_button(topic="balance")],
            ]
        )
        return _rows(keyboard)

    def _support_button(self) -> ButtonSpec | None:
        if not self._config.support_bot_username:
            return None
        return ButtonSpec(
            text="🆘 Поддержка",
            url=build_support_deep_link(
                bot_username=self._config.support_bot_username,
                role=_ROLE_SELLER,
                topic="generic",
                refs=(),
            ),
        )

    @staticmethod
    def _knowledge_button(*, topic: str) -> ButtonSpec:
        return _knowledge_button(topic=topic)

    def _format_usdt_with_rub(self, amount: Decimal, *, precise: bool = False) -> str:
        return format_usdt_with_rub(
            amount,
            display_rub_per_usdt=self._config.display_rub_per_usdt,
            precise=precise,
        )

    def _format_cashback_with_percent(self, *, reward_usdt: Decimal, reference_price_rub: int | None) -> str:
        return format_cashback_with_percent(
            reward_usdt=reward_usdt,
            reference_price_rub=reference_price_rub,
            display_rub_per_usdt=self._config.display_rub_per_usdt,
        )

    @staticmethod
    def _listing_has_sufficient_collateral(
        *,
        collateral_view: Any,
        seller_available_usdt: Decimal = Decimal("0.000000"),
        listing_status: str | None = None,
    ) -> bool:
        if collateral_view is None:
            return True
        status = listing_status or getattr(collateral_view, "status", None)
        effective_collateral = collateral_view.collateral_locked_usdt
        if status == "draft":
            effective_collateral += seller_available_usdt
        return effective_collateral >= collateral_view.collateral_required_usdt

    def _format_listing_collateral_line(
        self,
        *,
        collateral_view: Any,
        seller_available_usdt: Decimal = Decimal("0.000000"),
    ) -> str:
        if collateral_view is None:
            return "—"
        required_text = self._format_usdt_with_rub(collateral_view.collateral_required_usdt)
        if self._listing_has_sufficient_collateral(
            collateral_view=collateral_view,
            seller_available_usdt=seller_available_usdt,
        ):
            return f"🟢 {required_text}"
        return f"🔴 {format_usdt(collateral_view.collateral_required_usdt)} (недостаточно средств)"

    def _listing_detail_note(
        self,
        *,
        listing: Any,
        collateral_view: Any,
        seller_available_usdt: Decimal = Decimal("0.000000"),
    ) -> str:
        if listing.status == "active":
            return "Объявление активно. При необходимости поставьте его на паузу или поделитесь ссылкой на товар."
        if not self._listing_has_sufficient_collateral(
            collateral_view=collateral_view,
            seller_available_usdt=seller_available_usdt,
            listing_status=listing.status,
        ):
            return "Для активации пополните баланс продавца, затем вернитесь к карточке объявления."
        return "Проверьте параметры и активируйте объявление, когда будете готовы."

    def _seller_listing_detail_html(
        self,
        *,
        listing: Any,
        collateral_view: Any,
        seller_available_usdt: Decimal = Decimal("0.000000"),
        listing_link: str | None = None,
        notice: str | None = None,
    ) -> str:
        display_title = listing_display_title(display_title=listing.display_title, fallback=listing.search_phrase)
        planned = collateral_view.slot_count if collateral_view is not None else listing.slot_count
        in_progress = collateral_view.in_progress_assignments_count if collateral_view is not None else 0
        cashback_text = self._format_cashback_with_percent(
            reward_usdt=listing.reward_usdt,
            reference_price_rub=listing.reference_price_rub,
        )
        is_active = listing.status == "active"
        title = f"{'🟢' if is_active else '🔴'} {html.escape(display_title)}"
        lines: list[str] = []
        if notice:
            lines.append(html.escape(notice))
        lines.extend(
            [
                f"<b>Артикул WB:</b> {listing.wb_product_id}",
                f"<b>Кэшбэк:</b> {html.escape(cashback_text)}",
                f"<b>Поисковая фраза:</b> &quot;{html.escape(listing.search_phrase)}&quot;",
                f"<b>План покупок / В процессе:</b> {planned} / {in_progress}",
            ]
        )
        if listing_link:
            lines.append(f"<b>Ссылка на товар:</b>\n{html.escape(listing_link)}")
        lines.extend(
            [
                "<b>Обеспечение:</b> "
                + self._format_listing_collateral_line(
                    collateral_view=collateral_view,
                    seller_available_usdt=seller_available_usdt,
                ),
                f"<b>Статус:</b> {self._listing_activity_badge(is_active=is_active)}",
            ]
        )
        parameters_lines = [
            f"Предмет: {html.escape(listing.wb_subject_name or '—')}",
            f"Артикул продавца: {html.escape(listing.wb_vendor_code or '—')}",
            f"Бренд: {html.escape(listing.wb_brand_name or '—')}",
            f"Название WB: {html.escape(listing.wb_source_title or display_title)}",
            format_listing_price_line(
                label="Цена покупателя",
                price_rub=listing.reference_price_rub,
                source=listing.reference_price_source,
            )
            .replace("<b>", "")
            .replace("</b>", ""),
            "Фразы для отзыва: " + html.escape(format_review_phrases_text(getattr(listing, "review_phrases", []))),
            f"Размеры: {html.escape(format_sizes_text(listing.wb_tech_sizes))}",
        ]
        lines.append("\n<b>Параметры</b>\n<blockquote expandable>" + "\n".join(parameters_lines) + "</blockquote>")
        description_block = format_expandable_block_html(title="Описание", body=listing.wb_description)
        if description_block:
            lines.append(f"\n{description_block}")
        characteristics_block = format_characteristics_block_html(listing.wb_characteristics)
        if characteristics_block:
            lines.append(f"\n{characteristics_block}")
        return screen_text(
            title=title,
            title_suffix_html=title_ref_suffix(format_listing_ref(listing.listing_id)),
            cta="Проверьте объявление и выберите следующее действие ниже.",
            lines=lines,
            note=self._listing_detail_note(
                listing=listing,
                collateral_view=collateral_view,
                seller_available_usdt=seller_available_usdt,
            ),
        )

    @staticmethod
    def _listing_activity_badge(*, is_active: bool) -> str:
        return status_badge("активно" if is_active else "не активно", color="green" if is_active else "red")

    def _deposit_status_badge(self, status: str) -> str:
        color = {
            "credited": "green",
            "expired": "red",
            "cancelled": "red",
            "manual_review": "yellow",
            "matched": "blue",
            "pending": "yellow",
        }.get(status, "blue")
        return status_badge(_humanize_deposit_status(status), color=color)

    def _build_ton_usdt_wallet_link(
        self,
        *,
        destination_address: str,
        expected_amount_usdt: Decimal,
        text: str | None = None,
    ) -> str:
        normalized_address = destination_address.strip()
        base_units = int(expected_amount_usdt.quantize(_USDT_EXACT_QUANT, rounding=ROUND_HALF_UP) * Decimal("1000000"))
        params = {"jetton": self._config.tonapi_usdt_jetton_master, "amount": str(base_units)}
        if text:
            params["text"] = text.strip()
        query = urllib.parse.urlencode(params)
        encoded_address = urllib.parse.quote(normalized_address, safe="")
        return f"ton://transfer/{encoded_address}?{query}"


def _as_reply(result: FlowResult) -> FlowResult:
    effects = []
    for effect in result.effects:
        if isinstance(effect, ReplaceText):
            effects.append(ReplyText(text=effect.text, buttons=effect.buttons, parse_mode=effect.parse_mode))
        else:
            effects.append(effect)
    return FlowResult(effects=tuple(effects))


def _is_valid_shop_token(status: str | None) -> bool:
    return (status or "").strip().lower() == "valid"


def _knowledge_button(*, topic: str) -> ButtonSpec:
    mapping = {
        "guide": ("📘 Инструкция", "kb_guide"),
        "shops": ("📘 Про магазины", "kb_shops"),
        "listings": ("📘 Про объявления", "kb_listings"),
        "balance": ("📘 Про баланс и вывод", "kb_balance"),
    }
    label, action = mapping[topic]
    return ButtonSpec(text=label, flow=_ROLE_SELLER, action=action)


def _button(text: str, *, action: str, entity_id: int | str = "") -> ButtonSpec:
    return ButtonSpec(text=text, flow=_ROLE_SELLER, action=action, entity_id=str(entity_id))


def _rows(rows: list[list[ButtonSpec]] | tuple[tuple[ButtonSpec, ...], ...]) -> tuple[tuple[ButtonSpec, ...], ...]:
    return tuple(tuple(row) for row in rows)


def _humanize_deposit_status(status: str) -> str:
    mapping = {
        "pending": "Ожидается оплата",
        "matched": "Платеж найден, идет проверка",
        "manual_review": "Нужна проверка администратором",
        "credited": "Зачислено",
        "expired": "Срок счета истек",
        "cancelled": "Отменено",
    }
    return mapping.get(status, status)

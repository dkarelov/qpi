from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from libs.domain.errors import (
    DomainError,
    InsufficientFundsError,
    InvalidStateError,
    ListingValidationError,
    NotFoundError,
)
from libs.domain.fx_rates import FxRateService
from libs.domain.listing_creation import parse_listing_create_csv, sanitize_buyer_display_title
from libs.domain.seller import SellerService
from libs.domain.seller_workflow import SellerWorkflowService
from libs.integrations.wb import WbPingClient
from libs.security.token_cipher import encrypt_token


@dataclass(frozen=True)
class SellerCommandResponse:
    text: str
    delete_source_message: bool = False


class SellerCommandProcessor:
    """Minimal seller command handlers; transport layer can call this from Telegram adapters."""

    def __init__(
        self,
        *,
        seller_service: SellerService,
        wb_ping_client: WbPingClient,
        token_cipher_key: str,
        bot_username: str,
        seller_workflow_service: SellerWorkflowService | None = None,
        display_rub_per_usdt: Decimal = Decimal("100"),
        fx_rate_service: FxRateService | None = None,
        fx_rate_ttl_seconds: int = 900,
    ) -> None:
        self._seller_service = seller_service
        self._seller_workflow_service = seller_workflow_service
        self._wb_ping_client = wb_ping_client
        self._token_cipher_key = token_cipher_key
        self._bot_username = bot_username.lstrip("@")
        self._display_rub_per_usdt = display_rub_per_usdt
        self._fx_rate_service = fx_rate_service
        self._fx_rate_ttl_seconds = fx_rate_ttl_seconds

    async def handle(
        self,
        *,
        telegram_id: int,
        username: str | None,
        text: str,
    ) -> SellerCommandResponse:
        normalized = text.strip()
        if not normalized:
            return SellerCommandResponse(text="Пустая команда. Отправьте /start.")

        command, _, args = normalized.partition(" ")
        command = command.lower()
        args = args.strip()

        try:
            seller = await self._seller_service.bootstrap_seller(
                telegram_id=telegram_id,
                username=username,
            )
            seller_user_id = seller.user_id

            if command == "/start":
                return SellerCommandResponse(
                    text=(
                        "Роль: продавец.\n"
                        "Команды:\n"
                        "/shop_create <название>\n"
                        "/shop_list\n"
                        "/shop_delete <shop_id> [confirm]\n"
                        "/token_set <shop_id> <wb_token>\n"
                        "/listing_create <shop_id> <артикул ВБ, кэшбэк в рублях, макс. заказов, "
                        "поисковая фраза, фраза для отзыва 1, ... , фраза для отзыва 10> "
                        "[|| <цена покупателя в рублях> [|| <название для покупателей>]]\n"
                        "/listing_list [shop_id]\n"
                        "/listing_activate <listing_id> [idempotency_key]\n"
                        "/listing_pause <listing_id> [reason]\n"
                        "/listing_unpause <listing_id>\n"
                        "/listing_delete <listing_id> [confirm]"
                    )
                )

            if command == "/shop_create":
                if not args:
                    return SellerCommandResponse(text="Использование: /shop_create <название>")
                shop = await self._seller_service.create_shop(
                    seller_user_id=seller_user_id,
                    title=args,
                )
                deep_link = f"https://t.me/{self._bot_username}?start=shop_{shop.slug}"
                return SellerCommandResponse(
                    text=(
                        f"Магазин «{shop.title}» создан.\n"
                        f"Ссылка для покупателей:\n{deep_link}"
                    )
                )

            if command == "/shop_list":
                shops = await self._seller_service.list_shops(seller_user_id=seller_user_id)
                if not shops:
                    return SellerCommandResponse(text="У вас пока нет магазинов.")
                lines = [f"{item.shop_id} | {item.slug} | {item.title}" for item in shops]
                return SellerCommandResponse(text="Магазины:\n" + "\n".join(lines))

            if command == "/shop_delete":
                tokens = args.split()
                if not tokens:
                    return SellerCommandResponse(
                        text="Использование: /shop_delete <shop_id> [confirm]"
                    )
                shop_id = int(tokens[0])
                is_confirmed = len(tokens) > 1 and tokens[1].lower() == "confirm"
                preview = await self._seller_service.get_shop_delete_preview(
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                )
                if not is_confirmed:
                    return SellerCommandResponse(
                        text=(
                            "ВНИМАНИЕ: удаление необратимо.\n"
                            f"Активных листингов: {preview.active_listings_count}\n"
                            f"Открытых назначений: {preview.open_assignments_count}\n"
                            "После подтверждения:\n"
                            "- связанным назначениям уйдет покупателям: "
                            f"{preview.assignment_linked_reserved_usdt} USDT\n"
                            "- несвязанное обеспечение вернется продавцу: "
                            f"{preview.unassigned_collateral_usdt} USDT\n"
                            f"Подтвердите: /shop_delete {shop_id} confirm"
                        )
                    )
                result = await self._seller_service.delete_shop(
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                    deleted_by_user_id=seller_user_id,
                    idempotency_key=f"shop-delete:{shop_id}",
                )
                if not result.changed:
                    return SellerCommandResponse(text="Магазин уже удален.")
                return SellerCommandResponse(
                    text=(
                        "Магазин удален.\n"
                        f"Переводов покупателям: {result.assignment_transfers_count}, "
                        f"сумма: {result.assignment_transferred_usdt} USDT\n"
                        f"Возвращено продавцу: {result.unassigned_collateral_returned_usdt} USDT"
                    )
                )

            if command == "/token_set":
                tokens = args.split(maxsplit=1)
                if len(tokens) < 2:
                    return SellerCommandResponse(
                        text="Использование: /token_set <shop_id> <wb_token>",
                        delete_source_message=True,
                    )
                shop_id = int(tokens[0])
                wb_token = tokens[1].strip()
                ping_result = await self._wb_ping_client.validate_token(wb_token)
                if not ping_result.valid:
                    details = ping_result.message or "неизвестная ошибка"
                    return SellerCommandResponse(
                        text=(
                            "Токен не принят.\n"
                            f"Проверка ping завершилась ошибкой: {details}\n"
                            "Токен не сохранен. Проверьте доступы «Статистика» и «Контент» "
                            "и отправьте корректный токен."
                        ),
                        delete_source_message=True,
                    )
                token_ciphertext = encrypt_token(wb_token, self._token_cipher_key)
                await self._seller_service.save_validated_shop_token(
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                    token_ciphertext=token_ciphertext,
                )
                return SellerCommandResponse(
                    text=(
                        "Токен валиден и сохранен. "
                        "Сообщение с токеном удалено в целях безопасности."
                    ),
                    delete_source_message=True,
                )

            if command == "/listing_create":
                if self._seller_workflow_service is None:
                    return SellerCommandResponse(
                        text="Команда /listing_create временно недоступна в этом режиме."
                    )
                try:
                    shop_id, listing_input, manual_price_rub, display_title = (
                        self._parse_listing_create_command_args(args)
                    )
                    wb_product_id, cashback_rub, slot_count, search_phrase, review_phrases = (
                        parse_listing_create_csv(listing_input)
                    )
                except (ValueError, InvalidOperation):
                    return SellerCommandResponse(text=self._listing_create_usage_text())
                if wb_product_id < 1 or cashback_rub <= Decimal("0") or slot_count < 1 or not search_phrase:
                    return SellerCommandResponse(text=self._listing_create_usage_text())

                fx_rate = await self._resolve_display_rub_per_usdt()
                reward_usdt = (cashback_rub / fx_rate).quantize(
                    Decimal("0.000001"),
                    rounding=ROUND_HALF_UP,
                )
                if reward_usdt <= Decimal("0"):
                    return SellerCommandResponse(text=self._listing_create_usage_text())

                snapshot = await self._seller_workflow_service.load_listing_creation_snapshot(
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                    wb_product_id=wb_product_id,
                )
                observed_buyer_price = await self._seller_workflow_service.lookup_listing_buyer_price(
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                    wb_product_id=wb_product_id,
                )
                reference_price_rub = (
                    observed_buyer_price.buyer_price_rub
                    if observed_buyer_price is not None
                    else manual_price_rub
                )
                if reference_price_rub is None:
                    return SellerCommandResponse(
                        text=(
                            "Не удалось определить цену покупателя по заказам WB за 30 дней.\n"
                            "Повторите команду и после данных объявления добавьте "
                            "`|| <цена покупателя в рублях>`."
                        )
                    )
                reference_price_source = "orders" if observed_buyer_price is not None else "manual"
                resolved_display_title = (display_title or "").strip() or sanitize_buyer_display_title(
                    wb_product_id=wb_product_id,
                    source_title=snapshot.name,
                    brand_name=snapshot.brand,
                )
                listing = await self._seller_service.create_listing_draft(
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                    wb_product_id=wb_product_id,
                    display_title=resolved_display_title,
                    wb_source_title=snapshot.name,
                    wb_subject_name=snapshot.subject_name,
                    wb_brand_name=snapshot.brand,
                    wb_vendor_code=snapshot.vendor_code,
                    wb_description=snapshot.description,
                    wb_photo_url=snapshot.photo_url,
                    wb_tech_sizes=snapshot.tech_sizes,
                    wb_characteristics=snapshot.characteristics,
                    review_phrases=review_phrases,
                    reference_price_rub=reference_price_rub,
                    reference_price_source=reference_price_source,
                    reference_price_updated_at=self._seller_workflow_service.reference_price_updated_at(
                        observed_buyer_price=observed_buyer_price,
                        reference_price_source=reference_price_source,
                    ),
                    search_phrase=search_phrase,
                    reward_usdt=reward_usdt,
                    slot_count=slot_count,
                )
                review_phrases_text = ", ".join(review_phrases) if review_phrases else "—"
                return SellerCommandResponse(
                    text=(
                        f"Листинг создан: id={listing.listing_id}, status={listing.status}\n"
                        f"Название: {listing.display_title}\n"
                        f"Артикул WB: {listing.wb_product_id}\n"
                        f"Поиск: \"{listing.search_phrase}\"\n"
                        f"Кэшбэк: {cashback_rub.quantize(Decimal('1'), rounding=ROUND_HALF_UP)} ₽ "
                        f"({listing.reward_usdt} USDT)\n"
                        f"Цена покупателя: {reference_price_rub} ₽ ({reference_price_source})\n"
                        f"Слоты: {listing.slot_count}\n"
                        f"Фразы для отзыва: {review_phrases_text}"
                    )
                )

            if command == "/listing_list":
                shop_id = int(args) if args else None
                listings = await self._seller_service.list_listings(
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                )
                if not listings:
                    return SellerCommandResponse(text="Листинги не найдены.")
                lines = [
                    (
                        f"{item.listing_id} | shop={item.shop_id} | wb={item.wb_product_id} | "
                        f"search=\"{item.search_phrase}\" | status={item.status} | "
                        "кэшбэк="
                        f"{item.reward_usdt} | "
                        f"slots={item.available_slots}/{item.slot_count}"
                    )
                    for item in listings
                ]
                return SellerCommandResponse(text="Листинги:\n" + "\n".join(lines))

            if command == "/listing_activate":
                tokens = args.split()
                if not tokens:
                    return SellerCommandResponse(
                        text="Использование: /listing_activate <listing_id> [idempotency_key]"
                    )
                listing_id = int(tokens[0])
                idempotency_key = (
                    tokens[1]
                    if len(tokens) > 1
                    else f"listing-activate:{seller_user_id}:{listing_id}"
                )
                if self._seller_workflow_service is None:
                    result = await self._seller_service.activate_listing(
                        seller_user_id=seller_user_id,
                        listing_id=listing_id,
                        idempotency_key=idempotency_key,
                    )
                else:
                    result = await self._seller_workflow_service.activate_listing(
                        seller_user_id=seller_user_id,
                        listing_id=listing_id,
                        idempotency_key=idempotency_key,
                    )
                if not result.changed:
                    return SellerCommandResponse(text="Листинг уже активен.")
                return SellerCommandResponse(text="Листинг активирован, обеспечение заблокировано.")

            if command == "/listing_pause":
                tokens = args.split(maxsplit=1)
                if not tokens:
                    return SellerCommandResponse(
                        text="Использование: /listing_pause <listing_id> [reason]"
                    )
                listing_id = int(tokens[0])
                reason = tokens[1] if len(tokens) > 1 else "manual_pause"
                result = await self._seller_service.pause_listing(
                    seller_user_id=seller_user_id,
                    listing_id=listing_id,
                    reason=reason,
                )
                if not result.changed:
                    return SellerCommandResponse(text="Листинг уже на паузе.")
                return SellerCommandResponse(text="Листинг поставлен на паузу.")

            if command == "/listing_unpause":
                if not args:
                    return SellerCommandResponse(
                        text="Использование: /listing_unpause <listing_id>"
                    )
                listing_id = int(args)
                if self._seller_workflow_service is None:
                    result = await self._seller_service.unpause_listing(
                        seller_user_id=seller_user_id,
                        listing_id=listing_id,
                    )
                else:
                    result = await self._seller_workflow_service.unpause_listing(
                        seller_user_id=seller_user_id,
                        listing_id=listing_id,
                    )
                if not result.changed:
                    return SellerCommandResponse(text="Листинг уже активен.")
                return SellerCommandResponse(text="Листинг снят с паузы и активен.")

            if command == "/listing_delete":
                tokens = args.split()
                if not tokens:
                    return SellerCommandResponse(
                        text="Использование: /listing_delete <listing_id> [confirm]"
                    )
                listing_id = int(tokens[0])
                is_confirmed = len(tokens) > 1 and tokens[1].lower() == "confirm"
                preview = await self._seller_service.get_listing_delete_preview(
                    seller_user_id=seller_user_id,
                    listing_id=listing_id,
                )
                if not is_confirmed:
                    return SellerCommandResponse(
                        text=(
                            "ВНИМАНИЕ: удаление необратимо.\n"
                            f"Открытых назначений: {preview.open_assignments_count}\n"
                            "После подтверждения:\n"
                            "- связанным назначениям уйдет покупателям: "
                            f"{preview.assignment_linked_reserved_usdt} USDT\n"
                            "- несвязанное обеспечение вернется продавцу: "
                            f"{preview.unassigned_collateral_usdt} USDT\n"
                            f"Подтвердите: /listing_delete {listing_id} confirm"
                        )
                    )
                result = await self._seller_service.delete_listing(
                    seller_user_id=seller_user_id,
                    listing_id=listing_id,
                    deleted_by_user_id=seller_user_id,
                    idempotency_key=f"listing-delete:{listing_id}",
                )
                if not result.changed:
                    return SellerCommandResponse(text="Листинг уже удален.")
                return SellerCommandResponse(
                    text=(
                        "Листинг удален.\n"
                        f"Переведено покупателям: {result.assignment_transferred_usdt} USDT\n"
                        f"Возвращено продавцу: {result.unassigned_collateral_returned_usdt} USDT"
                    )
                )

            return SellerCommandResponse(text="Неизвестная команда. Отправьте /start.")
        except ValueError:
            return SellerCommandResponse(text="Неверный формат аргументов команды.")
        except InvalidOperation:
            return SellerCommandResponse(text="Неверный числовой формат в аргументах.")
        except NotFoundError as exc:
            return SellerCommandResponse(text=f"Не найдено: {exc}")
        except InsufficientFundsError:
            return SellerCommandResponse(text="Недостаточно средств для операции.")
        except ListingValidationError as exc:
            return SellerCommandResponse(text=str(exc))
        except InvalidStateError as exc:
            return SellerCommandResponse(text=f"Операция недоступна: {exc}")
        except DomainError as exc:
            return SellerCommandResponse(text=f"Ошибка доменной логики: {exc}")

    @staticmethod
    def _listing_create_usage_text() -> str:
        return (
            "Использование: /listing_create <shop_id> "
            "<артикул ВБ, кэшбэк в рублях, макс. заказов, поисковая фраза, "
            "фраза для отзыва 1, ... , фраза для отзыва 10> "
            "[|| <цена покупателя в рублях> [|| <название для покупателей>]]"
        )

    @staticmethod
    def _parse_listing_create_command_args(
        args: str,
    ) -> tuple[int, str, int | None, str | None]:
        shop_id_text, separator, remainder = args.partition(" ")
        if not separator:
            raise ValueError("missing listing payload")
        shop_id = int(shop_id_text)
        segments = [segment.strip() for segment in remainder.split("||")]
        if not segments or not segments[0]:
            raise ValueError("missing listing payload")
        if len(segments) > 3:
            raise ValueError("too many listing create segments")

        manual_price_rub: int | None = None
        if len(segments) >= 2 and segments[1]:
            manual_price_rub = int(
                Decimal(segments[1]).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            if manual_price_rub < 1:
                raise ValueError("manual_price_rub must be >= 1")

        display_title = None
        if len(segments) == 3:
            display_title = segments[2] or None

        return shop_id, segments[0], manual_price_rub, display_title

    async def _resolve_display_rub_per_usdt(self) -> Decimal:
        if self._fx_rate_service is None:
            return self._display_rub_per_usdt
        try:
            return await self._fx_rate_service.get_usdt_rub_rate(
                max_age_seconds=self._fx_rate_ttl_seconds,
                fallback_rate=self._display_rub_per_usdt,
            )
        except Exception:
            return self._display_rub_per_usdt

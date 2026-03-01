from __future__ import annotations

from dataclasses import dataclass

from libs.domain.buyer import BuyerService
from libs.domain.errors import (
    DomainError,
    DuplicateOrderError,
    InvalidStateError,
    NoSlotsAvailableError,
    NotFoundError,
    PayloadValidationError,
)


@dataclass(frozen=True)
class BuyerCommandResponse:
    text: str
    delete_source_message: bool = False


class BuyerCommandProcessor:
    """Minimal buyer command handlers; transport layer can call this from Telegram adapters."""

    def __init__(self, *, buyer_service: BuyerService, bot_username: str) -> None:
        self._buyer_service = buyer_service
        self._bot_username = bot_username.lstrip("@")

    async def handle(
        self,
        *,
        telegram_id: int,
        username: str | None,
        text: str,
    ) -> BuyerCommandResponse:
        normalized = text.strip()
        if not normalized:
            return BuyerCommandResponse(text="Пустая команда. Отправьте /start.")

        command, _, args = normalized.partition(" ")
        command = command.lower()
        args = args.strip()

        try:
            buyer = await self._buyer_service.bootstrap_buyer(
                telegram_id=telegram_id,
                username=username,
            )
            buyer_user_id = buyer.user_id

            if command == "/start":
                if args.startswith("shop_"):
                    slug = args[len("shop_") :].strip()
                    if slug:
                        return await self._render_shop_catalog(slug=slug)
                return BuyerCommandResponse(
                    text=(
                        "Роль: покупатель.\n"
                        "Команды:\n"
                        "/shop <slug>\n"
                        "/reserve <listing_id> [idempotency_key]\n"
                        "/submit_order <assignment_id> <base64_payload>\n"
                        "/my_orders"
                    )
                )

            if command == "/shop":
                if not args:
                    return BuyerCommandResponse(text="Использование: /shop <slug>")
                return await self._render_shop_catalog(slug=args)

            if command == "/reserve":
                tokens = args.split()
                if not tokens:
                    return BuyerCommandResponse(
                        text="Использование: /reserve <listing_id> [idempotency_key]"
                    )
                listing_id = int(tokens[0])
                idempotency_key = (
                    tokens[1] if len(tokens) > 1 else f"reserve:{buyer_user_id}:{listing_id}"
                )
                reservation = await self._buyer_service.reserve_listing_slot(
                    buyer_user_id=buyer_user_id,
                    listing_id=listing_id,
                    idempotency_key=idempotency_key,
                )
                if reservation.created:
                    return BuyerCommandResponse(
                        text=(
                            f"Слот зарезервирован: assignment_id={reservation.assignment_id}\n"
                            f"Нужно отправить подтверждение покупки до "
                            f"{reservation.reservation_expires_at.isoformat()}\n"
                            "Формат: /submit_order <assignment_id> <base64_payload>"
                        )
                    )
                return BuyerCommandResponse(
                    text=(
                        f"Резерв уже существует: assignment_id={reservation.assignment_id}\n"
                        f"Дедлайн: {reservation.reservation_expires_at.isoformat()}"
                    )
                )

            if command == "/submit_order":
                tokens = args.split(maxsplit=1)
                if len(tokens) != 2:
                    return BuyerCommandResponse(
                        text="Использование: /submit_order <assignment_id> <base64_payload>",
                        delete_source_message=True,
                    )
                assignment_id = int(tokens[0])
                payload = tokens[1].strip()
                result = await self._buyer_service.submit_purchase_payload(
                    buyer_user_id=buyer_user_id,
                    assignment_id=assignment_id,
                    payload_base64=payload,
                )
                if not result.changed:
                    return BuyerCommandResponse(
                        text=f"Заказ уже подтвержден ранее: order_id={result.order_id}",
                        delete_source_message=True,
                    )
                return BuyerCommandResponse(
                    text=(
                        "Подтверждение принято.\n"
                        f"assignment_id={result.assignment_id}\n"
                        f"order_id={result.order_id}\n"
                        "Статус: order_verified"
                    ),
                    delete_source_message=True,
                )

            if command == "/my_orders":
                assignments = await self._buyer_service.list_buyer_assignments(
                    buyer_user_id=buyer_user_id
                )
                if not assignments:
                    return BuyerCommandResponse(text="У вас пока нет назначений.")
                lines = []
                for assignment in assignments:
                    lines.append(
                        f"{assignment.assignment_id} | shop={assignment.shop_slug} | "
                        f"listing={assignment.listing_id} | "
                        f"wb_product_id={assignment.wb_product_id} | "
                        f"status={assignment.status} | кэшбэк={assignment.reward_usdt} USDT | "
                        f"order_id={assignment.order_id or '-'}"
                    )
                return BuyerCommandResponse(text="Мои назначения:\n" + "\n".join(lines))

            return BuyerCommandResponse(text="Неизвестная команда. Отправьте /start.")
        except ValueError:
            return BuyerCommandResponse(text="Неверный формат аргументов команды.")
        except NotFoundError as exc:
            return BuyerCommandResponse(text=f"Не найдено: {exc}")
        except NoSlotsAvailableError:
            return BuyerCommandResponse(text="Свободных слотов нет.")
        except PayloadValidationError as exc:
            return BuyerCommandResponse(
                text=f"Подтверждение отклонено: {exc}",
                delete_source_message=True,
            )
        except DuplicateOrderError:
            return BuyerCommandResponse(
                text="Подтверждение отклонено: этот order_id уже использован.",
                delete_source_message=True,
            )
        except InvalidStateError as exc:
            return BuyerCommandResponse(text=f"Операция недоступна: {exc}")
        except DomainError as exc:
            return BuyerCommandResponse(text=f"Ошибка доменной логики: {exc}")

    async def _render_shop_catalog(self, *, slug: str) -> BuyerCommandResponse:
        shop = await self._buyer_service.resolve_shop_by_slug(slug=slug)
        listings = await self._buyer_service.list_active_listings_by_shop_slug(slug=slug)
        deep_link = f"https://t.me/{self._bot_username}?start=shop_{shop.slug}"
        if not listings:
            return BuyerCommandResponse(
                text=(
                    f"Магазин: {shop.title} ({shop.slug})\n"
                    "Активных листингов пока нет.\n"
                    f"Ссылка: {deep_link}"
                )
            )
        lines = [
            (
                f"{item.listing_id} | wb_product_id={item.wb_product_id} | "
                f"поиск=\"{item.search_phrase}\" | кэшбэк={item.reward_usdt} USDT | "
                f"slots={item.available_slots}/{item.slot_count}"
            )
            for item in listings
        ]
        return BuyerCommandResponse(
            text=(
                f"Магазин: {shop.title} ({shop.slug})\n"
                f"Ссылка: {deep_link}\n"
                "Активные листинги:\n"
                + "\n".join(lines)
                + "\n\nЧтобы занять слот: /reserve <listing_id>"
            )
        )

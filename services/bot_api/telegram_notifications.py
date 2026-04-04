from __future__ import annotations

import html
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from libs.domain.models import NotificationOutboxItem, RenderedTelegramNotification
from libs.domain.notifications import (
    EVENT_ASSIGNMENT_DELIVERY_EXPIRED_BUYER,
    EVENT_ASSIGNMENT_DELIVERY_EXPIRED_SELLER,
    EVENT_ASSIGNMENT_EARLY_PAYOUT_LISTING_DELETE_BUYER,
    EVENT_ASSIGNMENT_EARLY_PAYOUT_SHOP_DELETE_BUYER,
    EVENT_ASSIGNMENT_ORDER_VERIFIED_SELLER,
    EVENT_ASSIGNMENT_PICKED_UP_BUYER,
    EVENT_ASSIGNMENT_PICKED_UP_SELLER,
    EVENT_ASSIGNMENT_RESERVATION_EXPIRED_BUYER,
    EVENT_ASSIGNMENT_RETURNED_BUYER,
    EVENT_ASSIGNMENT_RETURNED_SELLER,
    EVENT_ASSIGNMENT_REWARD_UNLOCKED_BUYER,
    EVENT_ASSIGNMENT_REWARD_UNLOCKED_SELLER,
    EVENT_DEPOSIT_CANCELLED_SELLER,
    EVENT_DEPOSIT_CREDITED_SELLER,
    EVENT_DEPOSIT_EXPIRED_SELLER,
    EVENT_DEPOSIT_MANUAL_REVIEW_ADMIN,
    EVENT_DEPOSIT_MANUAL_REVIEW_SELLER,
    EVENT_MANUAL_BALANCE_CREDIT_TARGET,
    EVENT_SELLER_TOKEN_INVALIDATED,
    EVENT_WITHDRAW_CANCELLED_ADMIN,
    EVENT_WITHDRAW_CREATED_ADMIN,
    EVENT_WITHDRAW_REJECTED_REQUESTER,
    EVENT_WITHDRAW_SENT_REQUESTER,
)
from libs.domain.public_refs import (
    format_chain_tx_ref,
    format_deposit_ref,
    format_withdrawal_ref,
)

MSK = ZoneInfo("Europe/Moscow")


def render_telegram_notification(
    item: NotificationOutboxItem,
    *,
    display_rub_per_usdt: Decimal | None = None,
) -> RenderedTelegramNotification:
    payload = item.payload_json
    event_type = item.event_type
    if event_type == EVENT_ASSIGNMENT_RESERVATION_EXPIRED_BUYER:
        return RenderedTelegramNotification(
            text=(
                "<b>Бронь истекла</b>\n\n"
                f"<b>Товар:</b> {html.escape(payload['display_title'])}\n"
                f"<b>Магазин:</b> {html.escape(payload['shop_title'])}\n\n"
                "Покупка закрыта, потому что токен-подтверждение не был отправлен вовремя."
            ),
            parse_mode="HTML",
            cta_text="📋 Покупки",
            cta_flow="buyer",
            cta_action="assignments",
            cta_entity_id=None,
        )
    if event_type == EVENT_ASSIGNMENT_ORDER_VERIFIED_SELLER:
        return RenderedTelegramNotification(
            text=(
                "<b>Заказ подтвержден</b>\n\n"
                f"<b>Товар:</b> {html.escape(payload['display_title'])}\n"
                f"<b>Магазин:</b> {html.escape(payload['shop_title'])}\n"
                f"<b>Номер заказа:</b> {html.escape(str(payload['order_id']))}"
            ),
            parse_mode="HTML",
            cta_text="📦 Объявления",
            cta_flow="seller",
            cta_action="listing_open",
            cta_entity_id=str(payload["listing_id"]),
        )
    if event_type in {EVENT_ASSIGNMENT_PICKED_UP_BUYER, EVENT_ASSIGNMENT_PICKED_UP_SELLER}:
        title = "Выкуп подтвержден" if item.recipient_scope == "buyer" else "Покупка выкуплена"
        return RenderedTelegramNotification(
            text=(
                f"<b>{title}</b>\n\n"
                f"<b>Товар:</b> {html.escape(payload['display_title'])}\n"
                f"<b>Магазин:</b> {html.escape(payload['shop_title'])}\n"
                "<b>Кэшбэк разблокируется:</b> "
                f"{_format_datetime_msk(payload.get('unlock_at'))}"
            ),
            parse_mode="HTML",
            cta_text="📋 Покупки" if item.recipient_scope == "buyer" else "📦 Объявления",
            cta_flow="buyer" if item.recipient_scope == "buyer" else "seller",
            cta_action="assignments" if item.recipient_scope == "buyer" else "listing_open",
            cta_entity_id=None if item.recipient_scope == "buyer" else str(payload["listing_id"]),
        )
    if event_type in {EVENT_ASSIGNMENT_RETURNED_BUYER, EVENT_ASSIGNMENT_RETURNED_SELLER}:
        return RenderedTelegramNotification(
            text=(
                "<b>Возврат зафиксирован</b>\n\n"
                f"<b>Товар:</b> {html.escape(payload['display_title'])}\n"
                f"<b>Магазин:</b> {html.escape(payload['shop_title'])}\n\n"
                "Кэшбэк по этой покупке отменен."
            ),
            parse_mode="HTML",
            cta_text="📋 Покупки" if item.recipient_scope == "buyer" else "📦 Объявления",
            cta_flow="buyer" if item.recipient_scope == "buyer" else "seller",
            cta_action="assignments" if item.recipient_scope == "buyer" else "listing_open",
            cta_entity_id=None if item.recipient_scope == "buyer" else str(payload["listing_id"]),
        )
    if event_type in {
        EVENT_ASSIGNMENT_DELIVERY_EXPIRED_BUYER,
        EVENT_ASSIGNMENT_DELIVERY_EXPIRED_SELLER,
    }:
        return RenderedTelegramNotification(
            text=(
                "<b>Срок выкупа истек</b>\n\n"
                f"<b>Товар:</b> {html.escape(payload['display_title'])}\n"
                f"<b>Магазин:</b> {html.escape(payload['shop_title'])}\n\n"
                "Покупка закрыта без начисления кэшбэка."
            ),
            parse_mode="HTML",
            cta_text="📋 Покупки" if item.recipient_scope == "buyer" else "📦 Объявления",
            cta_flow="buyer" if item.recipient_scope == "buyer" else "seller",
            cta_action="assignments" if item.recipient_scope == "buyer" else "listing_open",
            cta_entity_id=None if item.recipient_scope == "buyer" else str(payload["listing_id"]),
        )
    if event_type in {
        EVENT_ASSIGNMENT_REWARD_UNLOCKED_BUYER,
        EVENT_ASSIGNMENT_REWARD_UNLOCKED_SELLER,
    }:
        heading = "Кэшбэк зачислен" if item.recipient_scope == "buyer" else "Кэшбэк выплачен"
        amount_text = (
            _format_rub_approx(payload["reward_usdt"], rub_per_usdt=display_rub_per_usdt)
            if item.recipient_scope == "buyer"
            else f"{_format_usdt_value(payload['reward_usdt'])} USDT"
        )
        return RenderedTelegramNotification(
            text=(
                f"<b>{heading}</b>\n\n"
                f"<b>Товар:</b> {html.escape(payload['display_title'])}\n"
                f"<b>Магазин:</b> {html.escape(payload['shop_title'])}\n"
                f"<b>Сумма:</b> {amount_text}"
            ),
            parse_mode="HTML",
            cta_text="💰 Баланс" if item.recipient_scope == "buyer" else "📦 Объявления",
            cta_flow="buyer" if item.recipient_scope == "buyer" else "seller",
            cta_action="balance" if item.recipient_scope == "buyer" else "listing_open",
            cta_entity_id=None if item.recipient_scope == "buyer" else str(payload["listing_id"]),
        )
    if event_type in {
        EVENT_ASSIGNMENT_EARLY_PAYOUT_LISTING_DELETE_BUYER,
        EVENT_ASSIGNMENT_EARLY_PAYOUT_SHOP_DELETE_BUYER,
    }:
        entity = (
            "объявление"
            if event_type == EVENT_ASSIGNMENT_EARLY_PAYOUT_LISTING_DELETE_BUYER
            else "магазин"
        )
        return RenderedTelegramNotification(
            text=(
                "<b>Кэшбэк зачислен досрочно</b>\n\n"
                f"Продавец удалил {entity}, связанный с вашими покупками.\n"
                f"<b>Магазин:</b> {html.escape(payload['shop_title'])}\n"
                f"<b>Покупок:</b> {int(payload['item_count'])}\n"
                "<b>Сумма:</b> "
                f"{_format_rub_approx(payload['total_reward_usdt'], rub_per_usdt=display_rub_per_usdt)}"
            ),
            parse_mode="HTML",
            cta_text="💰 Баланс",
            cta_flow="buyer",
            cta_action="balance",
            cta_entity_id=None,
        )
    if event_type == EVENT_SELLER_TOKEN_INVALIDATED:
        return RenderedTelegramNotification(
            text=(
                "<b>Токен WB больше не действует</b>\n\n"
                f"<b>Магазин:</b> {html.escape(payload['shop_title'])}\n"
                "<b>Причина:</b> "
                f"{html.escape(_token_invalidation_reason(payload.get('source')))}\n"
                "<b>Объявлений поставлено на паузу:</b> "
                f"{int(payload['paused_listings_count'])}"
            ),
            parse_mode="HTML",
            cta_text="🏪 Магазины",
            cta_flow="seller",
            cta_action="shop_open",
            cta_entity_id=str(payload["shop_id"]),
        )
    if event_type == EVENT_DEPOSIT_CREDITED_SELLER:
        lines = [
            "<b>Пополнение зачислено</b>",
            "",
            f"<b>Сумма:</b> {_format_usdt_value(payload['amount_usdt'])} USDT",
        ]
        if payload.get("tx_hash"):
            lines.append(f"<b>Хэш:</b> {html.escape(str(payload['tx_hash']))}")
        return RenderedTelegramNotification(
            text="\n".join(lines),
            parse_mode="HTML",
            cta_text="💰 Баланс",
            cta_flow="seller",
            cta_action="balance",
            cta_entity_id=None,
        )
    if event_type == EVENT_DEPOSIT_MANUAL_REVIEW_SELLER:
        return RenderedTelegramNotification(
            text=(
                "<b>Пополнение требует ручной проверки</b>\n\n"
                f"<b>Сумма:</b> {_format_usdt_value(payload['amount_usdt'])} USDT\n"
                f"<b>Причина:</b> {html.escape(str(payload['reason']))}"
            ),
            parse_mode="HTML",
            cta_text="💰 Баланс",
            cta_flow="seller",
            cta_action="balance",
            cta_entity_id=None,
        )
    if event_type == EVENT_DEPOSIT_MANUAL_REVIEW_ADMIN:
        lines = [
            "<b>Пополнение на ручной разбор</b>",
            "",
            (
                "<b>Транзакция:</b> "
                f"{_format_public_ref(format_chain_tx_ref(int(payload['chain_tx_id'])))}"
            ),
            f"<b>Сумма:</b> {_format_usdt_value(payload['amount_usdt'])} USDT",
            f"<b>Причина:</b> {html.escape(str(payload['reason']))}",
        ]
        if payload.get("deposit_intent_id") is not None:
            lines.append(
                "<b>Счет:</b> "
                f"{_format_public_ref(format_deposit_ref(int(payload['deposit_intent_id'])))}"
            )
        if payload.get("tx_hash"):
            lines.append(f"<b>Хэш:</b> {html.escape(str(payload['tx_hash']))}")
        return RenderedTelegramNotification(
            text="\n".join(lines),
            parse_mode="HTML",
            cta_text="⚠️ Исключения",
            cta_flow="admin",
            cta_action="exceptions_section",
            cta_entity_id=None,
        )
    if event_type == EVENT_DEPOSIT_EXPIRED_SELLER:
        deposit_ref = format_deposit_ref(int(payload["deposit_intent_id"]))
        return RenderedTelegramNotification(
            text=(
                "<b>Счет на пополнение истек</b>\n\n"
                f"<b>Счет:</b> {_format_public_ref(deposit_ref)}\n"
                f"<b>Ожидалось:</b> {_format_usdt_value(payload['expected_amount_usdt'])} USDT"
            ),
            parse_mode="HTML",
            cta_text="💰 Баланс",
            cta_flow="seller",
            cta_action="balance",
            cta_entity_id=None,
        )
    if event_type == EVENT_DEPOSIT_CANCELLED_SELLER:
        deposit_ref = format_deposit_ref(int(payload["deposit_intent_id"]))
        lines = [
            "<b>Счет на пополнение отменен</b>",
            "",
            f"<b>Счет:</b> {_format_public_ref(deposit_ref)}",
        ]
        if payload.get("reason"):
            lines.append(f"<b>Причина:</b> {html.escape(str(payload['reason']))}")
        return RenderedTelegramNotification(
            text="\n".join(lines),
            parse_mode="HTML",
            cta_text="💰 Баланс",
            cta_flow="seller",
            cta_action="balance",
            cta_entity_id=None,
        )
    if event_type == EVENT_WITHDRAW_CREATED_ADMIN:
        withdraw_ref = format_withdrawal_ref(int(payload["withdrawal_request_id"]))
        return RenderedTelegramNotification(
            text=(
                "<b>Новая заявка на вывод</b> "
                f"· {_format_public_ref(withdraw_ref)}\n\n"
                "<b>Роль:</b> "
                f"{html.escape(_withdraw_requester_label(payload['requester_role']))}\n"
                f"<b>Telegram:</b> {int(payload['requester_telegram_id'])} "
                f"(@{html.escape(payload['requester_username'] or '-')})\n"
                f"<b>Сумма:</b> {_format_usdt_value(payload['amount_usdt'])} USDT\n"
                f"<b>Статус:</b> {_withdraw_status_badge(str(payload['status']))}\n"
                f"<b>Кошелек:</b> {html.escape(str(payload['payout_address']))}\n"
                f"<b>Создана:</b> {_format_datetime_msk(payload.get('requested_at'))}\n"
                f"<b>Обработана:</b> {_format_datetime_msk(payload.get('processed_at'))}\n"
                f"<b>Отправлена:</b> {_format_datetime_msk(payload.get('sent_at'))}"
            ),
            parse_mode="HTML",
            cta_text="💸 Выводы",
            cta_flow="admin",
            cta_action="withdrawals_section",
            cta_entity_id=None,
        )
    if event_type == EVENT_WITHDRAW_CANCELLED_ADMIN:
        withdraw_ref = format_withdrawal_ref(int(payload["withdrawal_request_id"]))
        return RenderedTelegramNotification(
            text=(
                "<b>Заявка на вывод</b> "
                f"· {_format_public_ref(withdraw_ref)} "
                "<b>отменена заявителем</b>\n\n"
                "<b>Роль:</b> "
                f"{html.escape(_withdraw_requester_label(payload['requester_role']))}\n"
                f"<b>Telegram:</b> {int(payload['requester_telegram_id'])} "
                f"(@{html.escape(payload['requester_username'] or '-')})\n"
                f"<b>Сумма:</b> {_format_usdt_value(payload['amount_usdt'])} USDT"
            ),
            parse_mode="HTML",
            cta_text="💸 Выводы",
            cta_flow="admin",
            cta_action="withdrawals_section",
            cta_entity_id=None,
        )
    if event_type in {EVENT_WITHDRAW_REJECTED_REQUESTER, EVENT_WITHDRAW_SENT_REQUESTER}:
        subject = (
            "Заявка продавца на вывод" if payload["requester_role"] == "seller" else "Ваша заявка на вывод"
        )
        withdraw_ref = format_withdrawal_ref(int(payload["withdrawal_request_id"]))
        if event_type == EVENT_WITHDRAW_REJECTED_REQUESTER:
            lines = [
                f"<b>{html.escape(subject)}</b> · {_format_public_ref(withdraw_ref)} <b>отклонена</b>"
            ]
            if payload.get("note"):
                lines.extend(["", f"<b>Причина:</b> {html.escape(str(payload['note']))}"])
        else:
            lines = [
                f"<b>{html.escape(subject)}</b> · {_format_public_ref(withdraw_ref)} <b>отправлена</b>"
            ]
            if payload.get("tx_hash"):
                lines.extend(["", f"<b>Хэш перевода:</b> {html.escape(str(payload['tx_hash']))}"])
        return RenderedTelegramNotification(
            text="\n".join(lines),
            parse_mode="HTML",
            cta_text="💰 Баланс",
            cta_flow="seller" if payload["requester_role"] == "seller" else "buyer",
            cta_action="balance",
            cta_entity_id=None,
        )
    if event_type == EVENT_MANUAL_BALANCE_CREDIT_TARGET:
        amount_text = (
            _format_rub_approx(payload["amount_usdt"], rub_per_usdt=display_rub_per_usdt)
            if payload.get("recipient_role") == "buyer"
            else f"{_format_usdt_value(payload['amount_usdt'])} USDT"
        )
        return RenderedTelegramNotification(
            text=("<b>Баланс пополнен</b>\n\n" f"<b>Сумма:</b> {amount_text}"),
            parse_mode="HTML",
            cta_text="💰 Баланс",
            cta_flow="seller" if payload.get("recipient_role") == "seller" else "buyer",
            cta_action="balance",
            cta_entity_id=None,
        )
    raise ValueError(f"unsupported notification event: {event_type}")


def _format_usdt_value(value: str | Decimal) -> str:
    amount = _normalize_amount(Decimal(str(value)))
    text = format(amount, "f")
    return text.rstrip("0").rstrip(".")


def _format_public_ref(value: str) -> str:
    return f"<code>{html.escape(value.strip())}</code>"


def _format_rub_approx(value: str | Decimal, *, rub_per_usdt: Decimal | None) -> str:
    amount = _normalize_amount(Decimal(str(value)))
    if rub_per_usdt is None:
        return f"{_format_usdt_value(amount)} USDT"
    rub = (amount * Decimal(str(rub_per_usdt))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    text = format(rub, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"~{text} ₽"


def _format_datetime_msk(value: str | datetime | None) -> str:
    if not value:
        return "-"
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    localized = parsed.astimezone(MSK)
    return localized.strftime("%d.%m.%Y %H:%M MSK")


def _normalize_amount(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _token_invalidation_reason(source: str | None) -> str:
    if source == "scrapper_401_withdrawn":
        return "WB отозвал токен"
    if source == "scrapper_401_token_expired":
        return "токен истек"
    if source == "scrapper_401_unauthorized":
        return "WB отклонил авторизацию токена"
    return "токен недействителен"


def _withdraw_requester_label(role: str) -> str:
    if role == "seller":
        return "Продавец"
    if role == "buyer":
        return "Покупатель"
    return role


def _withdraw_status_badge(status: str) -> str:
    if status == "withdraw_pending_admin":
        return "🟡 На проверке"
    if status == "rejected":
        return "🔴 Отклонено"
    if status == "withdraw_sent":
        return "🟢 Отправлено"
    return html.escape(status)

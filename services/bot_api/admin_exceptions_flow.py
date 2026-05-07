from __future__ import annotations

import html
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from libs.domain.errors import InsufficientFundsError, InvalidStateError, NotFoundError, PayloadValidationError
from libs.domain.public_refs import (
    format_assignment_ref,
    format_chain_tx_ref,
    format_deposit_ref,
    parse_assignment_ref,
    parse_chain_tx_ref,
    parse_deposit_ref,
)
from services.bot_api.transport_effects import (
    ButtonSpec,
    ClearPrompt,
    FlowResult,
    LogEvent,
    ReplaceText,
    ReplyText,
    SetPrompt,
)

_ROLE_ADMIN = "admin"
_USDT_EXACT_QUANT = Decimal("0.000001")
_MSK_TZ = ZoneInfo("Europe/Moscow")


class AdminExceptionsAdapter(Protocol):
    async def list_pending_review_confirmations(self, *, limit: int = 1000) -> list[Any]: ...

    async def list_admin_review_txs(self, *, limit: int = 1000) -> list[Any]: ...

    async def list_admin_expired_intents(self, *, limit: int = 1000) -> list[Any]: ...

    async def admin_verify_review_payload(
        self,
        *,
        admin_user_id: int,
        assignment_id: int,
        payload_base64: str,
        idempotency_key: str,
    ) -> Any: ...

    async def credit_intent_from_chain_tx(
        self,
        *,
        deposit_intent_id: int,
        chain_tx_id: int,
        idempotency_key: str,
        admin_user_id: int,
        allow_expired: bool,
    ) -> Any: ...

    async def cancel_deposit_intent(
        self,
        *,
        deposit_intent_id: int,
        admin_user_id: int,
        reason: str,
        idempotency_key: str,
    ) -> bool: ...


class AdminExceptionsFlow:
    def __init__(self, *, adapter: AdminExceptionsAdapter) -> None:
        self._adapter = adapter

    async def render_queue(self) -> FlowResult:
        pending_reviews = await self._adapter.list_pending_review_confirmations(limit=1000)
        review_txs = await self._adapter.list_admin_review_txs(limit=1000)
        expired_intents = await self._adapter.list_admin_expired_intents(limit=1000)
        lines = _review_exception_lines(pending_reviews)
        lines.extend(_deposit_exception_lines(review_txs, expired_intents))
        return FlowResult(
            effects=(
                ReplaceText(
                    text=_screen_text(
                        title="Исключения",
                        cta="Проверьте отзывы и пополнения, которым нужна ручная обработка.",
                        lines=lines,
                        separate_blocks=True,
                    ),
                    buttons=_exception_queue_buttons(
                        pending_reviews_count=len(pending_reviews),
                        review_txs_count=len(review_txs),
                        expired_intents_count=len(expired_intents),
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    def start_review_verification_prompt(self, *, admin_user_id: int) -> FlowResult:
        return FlowResult(
            effects=(
                SetPrompt(
                    role=_ROLE_ADMIN,
                    prompt_type="admin_review_verify",
                    sensitive=True,
                    data={"admin_user_id": admin_user_id},
                ),
                ReplaceText(
                    text="Введите: <код_покупки> <base64_review_token>.\nНапример: P31 eyJ...==",
                    buttons=_back_to_exceptions_buttons(),
                    parse_mode=None,
                ),
            )
        )

    def start_deposit_attach_prompt(self, *, admin_user_id: int) -> FlowResult:
        return FlowResult(
            effects=(
                SetPrompt(
                    role=_ROLE_ADMIN,
                    prompt_type="admin_deposit_attach",
                    sensitive=False,
                    data={"admin_user_id": admin_user_id},
                ),
                ReplaceText(
                    text="Введите: <код_транзакции> <код_счета>.\nНапример: TX11 D22",
                    buttons=_back_to_exceptions_buttons(),
                    parse_mode=None,
                ),
            )
        )

    def start_deposit_cancel_prompt(self, *, admin_user_id: int) -> FlowResult:
        return FlowResult(
            effects=(
                SetPrompt(
                    role=_ROLE_ADMIN,
                    prompt_type="admin_deposit_cancel",
                    sensitive=False,
                    data={"admin_user_id": admin_user_id},
                ),
                ReplaceText(
                    text="Введите: <код_счета> <причина>.\nНапример: D22 late_payment",
                    buttons=_back_to_exceptions_buttons(),
                    parse_mode=None,
                ),
            )
        )

    async def submit_review_verification(
        self,
        *,
        prompt_state: dict[str, Any],
        text: str,
    ) -> FlowResult:
        admin_user_id = int(prompt_state.get("admin_user_id", 0))
        tokens = text.split(maxsplit=1)
        if len(tokens) != 2:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Формат: <код_покупки> <base64_review_token>",
                        parse_mode=None,
                    ),
                )
            )

        assignment_raw, payload_base64 = tokens
        try:
            assignment_id = parse_assignment_ref(assignment_raw)
        except ValueError:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Используйте код покупки вида P31 или обычное число.",
                        parse_mode=None,
                    ),
                )
            )

        if admin_user_id < 1:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(
                        text="Ошибка контекста админа. Откройте меню заново.",
                        parse_mode=None,
                    ),
                )
            )

        try:
            result = await self._adapter.admin_verify_review_payload(
                admin_user_id=admin_user_id,
                assignment_id=assignment_id,
                payload_base64=payload_base64.strip(),
                idempotency_key=f"tg-admin-review-verify:{admin_user_id}:{assignment_id}",
            )
        except PayloadValidationError:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(
                        text=(
                            "Не удалось подтвердить отзыв.\n"
                            "Проверьте, что токен относится к этой покупке и скопирован полностью."
                        ),
                        buttons=_admin_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        except (NotFoundError, InvalidStateError):
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(
                        text="Не удалось подтвердить отзыв. Откройте исключения и попробуйте снова.",
                        buttons=_admin_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )

        assignment_ref = format_assignment_ref(result.assignment_id)
        if result.changed:
            message = (
                "Отзыв подтвержден вручную.\n"
                f"Покупка: {assignment_ref}\n"
                "Кэшбэк будет разблокирован по стандартному сроку после выкупа."
            )
        else:
            message = f"Отзыв для покупки {assignment_ref} уже был подтвержден ранее."
        return FlowResult(
            effects=(
                ClearPrompt(),
                ReplyText(
                    text=message,
                    buttons=_admin_menu_buttons(),
                    parse_mode=None,
                ),
                LogEvent(
                    event_name="admin_review_verified",
                    fields={
                        "assignment_id": result.assignment_id,
                        "assignment_ref": assignment_ref,
                        "changed": result.changed,
                        "verification_status": result.verification_status,
                    },
                ),
            )
        )

    async def submit_deposit_attach(
        self,
        *,
        prompt_state: dict[str, Any],
        text: str,
    ) -> FlowResult:
        admin_user_id = int(prompt_state.get("admin_user_id", 0))
        tokens = text.split(maxsplit=1)
        if len(tokens) != 2:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Формат: <код_транзакции> <код_счета>",
                        parse_mode=None,
                    ),
                )
            )

        chain_tx_raw, intent_raw = tokens
        try:
            chain_tx_id = parse_chain_tx_ref(chain_tx_raw)
            deposit_intent_id = parse_deposit_ref(intent_raw)
        except ValueError:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Используйте коды вида TX11 D22 или обычные числа.",
                        parse_mode=None,
                    ),
                )
            )

        if admin_user_id < 1:
            return _invalid_admin_context_result()

        try:
            result = await self._adapter.credit_intent_from_chain_tx(
                deposit_intent_id=deposit_intent_id,
                chain_tx_id=chain_tx_id,
                idempotency_key=f"tg-admin-deposit-attach:{admin_user_id}:{chain_tx_id}:{deposit_intent_id}",
                admin_user_id=admin_user_id,
                allow_expired=True,
            )
        except (NotFoundError, InvalidStateError, ValueError):
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(
                        text="Не удалось привязать платеж к счету. Проверьте номера и попробуйте снова.",
                        buttons=_admin_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        except InsufficientFundsError:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(
                        text="Недостаточно средств на системном счете для зачисления.",
                        buttons=_admin_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )

        deposit_ref = format_deposit_ref(deposit_intent_id)
        chain_tx_ref = format_chain_tx_ref(chain_tx_id)
        if result.changed:
            message = (
                "Платеж привязан к счету и зачислен.\n"
                f"Счет: {deposit_ref}\n"
                f"Транзакция: {chain_tx_ref}"
            )
        else:
            message = (
                "Эта операция уже была выполнена ранее.\n"
                f"Счет: {deposit_ref}\n"
                f"Транзакция: {chain_tx_ref}"
            )
        return FlowResult(
            effects=(
                ClearPrompt(),
                ReplyText(
                    text=message,
                    buttons=_admin_menu_buttons(),
                    parse_mode=None,
                ),
                LogEvent(
                    event_name="admin_deposit_attach_processed",
                    fields={
                        "chain_tx_id": chain_tx_id,
                        "chain_tx_ref": chain_tx_ref,
                        "deposit_intent_id": deposit_intent_id,
                        "deposit_ref": deposit_ref,
                        "changed": result.changed,
                        "ledger_entry_id": result.ledger_entry_id,
                    },
                ),
            )
        )

    async def submit_deposit_cancel(
        self,
        *,
        prompt_state: dict[str, Any],
        text: str,
    ) -> FlowResult:
        admin_user_id = int(prompt_state.get("admin_user_id", 0))
        tokens = text.split(maxsplit=1)
        if len(tokens) != 2:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Формат: <код_счета> <причина>",
                        parse_mode=None,
                    ),
                )
            )

        intent_raw, reason = tokens
        try:
            deposit_intent_id = parse_deposit_ref(intent_raw)
        except ValueError:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Код счета должен быть вида D22 или числом.",
                        parse_mode=None,
                    ),
                )
            )
        normalized_reason = reason.strip()
        if not normalized_reason:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Причина не может быть пустой.",
                        parse_mode=None,
                    ),
                )
            )

        if admin_user_id < 1:
            return _invalid_admin_context_result()

        try:
            changed = await self._adapter.cancel_deposit_intent(
                deposit_intent_id=deposit_intent_id,
                admin_user_id=admin_user_id,
                reason=normalized_reason,
                idempotency_key=f"tg-admin-deposit-cancel:{admin_user_id}:{deposit_intent_id}",
            )
        except (NotFoundError, InvalidStateError, ValueError):
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(
                        text="Не удалось отменить счет. Проверьте номер и попробуйте снова.",
                        buttons=_admin_menu_buttons(),
                        parse_mode=None,
                    ),
                )
            )

        deposit_ref = format_deposit_ref(deposit_intent_id)
        message = (
            f"Счет {deposit_ref} отменен."
            if changed
            else f"Счет {deposit_ref} уже был отменен ранее."
        )
        return FlowResult(
            effects=(
                ClearPrompt(),
                ReplyText(
                    text=message,
                    buttons=_admin_menu_buttons(),
                    parse_mode=None,
                ),
                LogEvent(
                    event_name="admin_deposit_cancel_processed",
                    fields={
                        "deposit_intent_id": deposit_intent_id,
                        "deposit_ref": deposit_ref,
                        "changed": changed,
                    },
                ),
            )
        )


def _review_exception_lines(pending_reviews: list[Any]) -> list[str]:
    lines: list[str] = []
    if pending_reviews:
        lines.append("Отзывы, требующие проверки:")
        for item in pending_reviews[:20]:
            phrases_text = html.escape(_format_review_phrases_text(item.review_phrases))
            buyer_username = html.escape(item.buyer_username or "-")
            lines.append(
                f"Покупка {format_assignment_ref(item.assignment_id)}\n"
                f"Покупатель: {item.buyer_telegram_id} (@{buyer_username})\n"
                f"Товар: {html.escape(item.display_title)}\n"
                f"Оценка: {item.rating} / 5\n"
                f"Фразы: {phrases_text}\n"
                f"Причина: {html.escape(item.verification_reason or '-')}\n"
                f"Текст: {html.escape(item.review_text)}"
            )
    else:
        lines.append("Отзывов на ручную проверку нет.")
    return lines


def _deposit_exception_lines(review_txs: list[Any], expired_intents: list[Any]) -> list[str]:
    lines: list[str] = ["⚠️ Пополнения, требующие проверки:"]
    if review_txs:
        lines.append("Платежи на ручной разбор:")
        for tx in review_txs[:20]:
            suffix = f"{tx.suffix_code:03d}" if tx.suffix_code is not None else "нет"
            account_hint = (
                f"Счет: {format_deposit_ref(tx.matched_intent_id)}" if tx.matched_intent_id else "Счет: не найден"
            )
            lines.append(
                f"Транзакция {format_chain_tx_ref(tx.chain_tx_id)}\n"
                f"Сумма: {_format_usdt_value(tx.amount_usdt, precise=True)} USDT\n"
                f"Суффикс: {suffix}\n"
                f"Хэш: {tx.tx_hash}\n"
                f"Причина: {tx.review_reason or '-'}\n"
                f"{account_hint}"
            )
    else:
        lines.append("Платежей на ручной разбор нет.")

    if expired_intents:
        lines.append("Просроченные счета:")
        for intent in expired_intents[:20]:
            lines.append(
                f"Счет {format_deposit_ref(intent.deposit_intent_id)}\n"
                f"Продавец: {intent.seller_telegram_id}\n"
                f"Ожидалось: {_format_usdt_value(intent.expected_amount_usdt, precise=True)} USDT\n"
                f"Суффикс: {intent.suffix_code:03d}\n"
                f"Истек: {_format_datetime_msk(intent.expires_at)}"
            )
    else:
        lines.append("Просроченных счетов нет.")
    return lines


def _invalid_admin_context_result() -> FlowResult:
    return FlowResult(
        effects=(
            ClearPrompt(),
            ReplyText(
                text="Ошибка контекста админа. Откройте меню заново.",
                parse_mode=None,
            ),
        )
    )


def _exception_queue_buttons(
    *,
    pending_reviews_count: int,
    review_txs_count: int,
    expired_intents_count: int,
) -> tuple[tuple[ButtonSpec, ...], ...]:
    return (
        (
            _button(
                _button_label_with_count("✅ Проверить отзыв", pending_reviews_count),
                action="review_verify_prompt",
            ),
        ),
        (
            _button(
                _button_label_with_count("🔗 Привязать платеж к счету", review_txs_count),
                action="deposit_attach_prompt",
            ),
            _button(
                _button_label_with_count("🛑 Отменить счет", expired_intents_count),
                action="deposit_cancel_prompt",
            ),
        ),
        (_button("↩️ Назад", action="deposits_section"),),
    )


def _back_to_exceptions_buttons() -> tuple[tuple[ButtonSpec, ...], ...]:
    return ((_button("↩️ Назад к исключениям", action="exceptions_section"),),)


def _admin_menu_buttons() -> tuple[tuple[ButtonSpec, ...], ...]:
    return (
        (
            _button("💸 Выводы", action="withdrawals_section"),
            _button("🏦 Депозиты", action="deposits_section"),
        ),
        (_button("⚠️ Исключения", action="exceptions_section"),),
    )


def _button(text: str, *, action: str, entity_id: str = "") -> ButtonSpec:
    return ButtonSpec(text=text, flow=_ROLE_ADMIN, action=action, entity_id=entity_id)


def _button_label_with_count(label: str, count: int | None) -> str:
    if count is None:
        return label
    normalized_count = max(0, int(count))
    return f"{label} · {normalized_count}"


def _format_review_phrases_text(review_phrases: list[str] | None) -> str:
    normalized = _normalize_review_phrases(review_phrases)
    return ", ".join(normalized) if normalized else "нет"


def _normalize_review_phrases(review_phrases: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for phrase in review_phrases or []:
        text = str(phrase).strip()
        if text:
            normalized.append(text)
    return normalized


def _format_usdt_value(amount: Decimal, *, precise: bool = False) -> str:
    quant = _USDT_EXACT_QUANT if precise else Decimal("0.1")
    return _format_decimal(amount, quant=quant)


def _format_decimal(amount: Decimal, *, quant: Decimal) -> str:
    normalized = amount.quantize(quant, rounding=ROUND_HALF_UP)
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _format_datetime_msk(value: datetime | None) -> str:
    if value is None:
        return "—"
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    localized = normalized.astimezone(_MSK_TZ)
    return localized.strftime("%d.%m.%Y %H:%M MSK")


def _screen_text(
    *,
    title: str,
    cta: str | None = None,
    lines: list[str] | None = None,
    separate_blocks: bool = False,
) -> str:
    parts = [f"<b>{title}</b>"]
    if cta:
        parts.append(f"<i>{cta}</i>")
    if lines:
        filtered = [line for line in lines if line]
        if filtered:
            parts.append(("\n\n" if separate_blocks else "\n").join(filtered))
    return "\n\n".join(parts)

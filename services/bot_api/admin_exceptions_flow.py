from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any, Protocol

from libs.domain.errors import InvalidStateError, NotFoundError, PayloadValidationError
from libs.domain.public_refs import format_assignment_ref, parse_assignment_ref
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


class AdminReviewExceptionsAdapter(Protocol):
    async def list_pending_review_confirmations(self, *, limit: int = 1000) -> list[Any]: ...

    async def admin_verify_review_payload(
        self,
        *,
        admin_user_id: int,
        assignment_id: int,
        payload_base64: str,
        idempotency_key: str,
    ) -> Any: ...


@dataclass(frozen=True)
class AdminDepositExceptionSummary:
    lines: tuple[str, ...] = ()
    review_txs_count: int = 0
    expired_intents_count: int = 0


class AdminExceptionsFlow:
    def __init__(self, *, adapter: AdminReviewExceptionsAdapter) -> None:
        self._adapter = adapter

    async def render_queue(
        self,
        *,
        deposit_summary: AdminDepositExceptionSummary | None = None,
    ) -> FlowResult:
        deposit_summary = deposit_summary or AdminDepositExceptionSummary()
        pending_reviews = await self._adapter.list_pending_review_confirmations(limit=1000)
        lines = _review_exception_lines(pending_reviews)
        lines.extend(deposit_summary.lines)
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
                        review_txs_count=deposit_summary.review_txs_count,
                        expired_intents_count=deposit_summary.expired_intents_count,
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

from __future__ import annotations

import html
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Protocol

from libs.domain.errors import InsufficientFundsError, InvalidStateError, NotFoundError
from libs.domain.public_refs import format_withdrawal_ref
from services.bot_api.transport_effects import (
    ButtonSpec,
    ClearPrompt,
    FlowResult,
    LogEvent,
    ReplaceText,
    ReplyRoleMenuText,
    ReplyText,
    SetPrompt,
)

_USDT_EXACT_QUANT = Decimal("0.000001")
_ZERO_USDT = Decimal("0.000000")


class AddressValidationUnavailable(Exception):
    pass


@dataclass(frozen=True)
class WithdrawalRequester:
    user_id: int
    available_account_id: int
    pending_account_id: int


class WithdrawalRequesterAdapter(Protocol):
    async def get_active_request(self, *, requester_user_id: int) -> Any | None: ...

    async def get_available_balance(self, *, requester_user_id: int) -> Decimal: ...

    async def load_requester(self, *, telegram_id: int, username: str | None) -> WithdrawalRequester: ...

    async def create_withdrawal_request(
        self,
        *,
        requester: WithdrawalRequester,
        amount_usdt: Decimal,
        payout_address: str,
        idempotency_key: str,
    ) -> Any: ...

    async def get_withdrawal_request_detail(self, *, request_id: int) -> Any: ...

    async def cancel_withdrawal_request(
        self,
        *,
        request_id: int,
        requester_user_id: int,
        idempotency_key: str,
    ) -> Any: ...


class TonMainnetAddressValidator(Protocol):
    async def validate(self, *, address: str) -> None: ...


@dataclass(frozen=True)
class WithdrawalFlowConfig:
    role: str
    requester_id_key: str
    amount_prompt_type: str
    address_prompt_type: str
    full_active_text: str
    manual_active_text: str
    invalid_amount_context_text: str
    invalid_address_amount_context_text: str
    invalid_user_context_text: str
    stale_context_text: str
    requested_event_name: str
    create_failed_event_name: str
    idempotency_key_prefix: str
    cancel_idempotency_key_prefix: str
    cancel_return_line: str
    cancel_success_text: str
    balance_button_text: str


SELLER_WITHDRAWAL_CONFIG = WithdrawalFlowConfig(
    role="seller",
    requester_id_key="seller_user_id",
    amount_prompt_type="seller_withdraw_amount",
    address_prompt_type="seller_withdraw_address",
    full_active_text="У вас уже есть активная заявка на вывод. Дождитесь обработки или отмените ее на экране баланса.",
    manual_active_text=(
        "У вас уже есть активная заявка на вывод. "
        "Откройте баланс и отмените ее, если нужно создать новую."
    ),
    invalid_amount_context_text="Ошибка контекста вывода. Откройте баланс заново.",
    invalid_address_amount_context_text="Ошибка контекста суммы. Откройте баланс заново.",
    invalid_user_context_text="Ошибка контекста пользователя. Откройте баланс заново.",
    stale_context_text="Контекст вывода устарел. Откройте баланс заново.",
    requested_event_name="seller_withdraw_requested",
    create_failed_event_name="seller_withdraw_request_create_failed",
    idempotency_key_prefix="tg-seller-withdraw",
    cancel_idempotency_key_prefix="tg-seller-withdraw-cancel",
    cancel_return_line="Средства вернутся в доступный баланс продавца.",
    cancel_success_text="Заявка на вывод отменена. Средства вернулись в доступный баланс продавца.",
    balance_button_text="💰 Баланс",
)

BUYER_WITHDRAWAL_CONFIG = WithdrawalFlowConfig(
    role="buyer",
    requester_id_key="buyer_user_id",
    amount_prompt_type="buyer_withdraw_amount",
    address_prompt_type="buyer_withdraw_address",
    full_active_text="У вас уже есть активная заявка на вывод. Дождитесь обработки или отмените ее на экране баланса.",
    manual_active_text=(
        "У вас уже есть активная заявка на вывод. "
        "Откройте баланс и отмените ее, если нужно создать новую."
    ),
    invalid_amount_context_text="Ошибка контекста вывода. Откройте баланс заново.",
    invalid_address_amount_context_text="Ошибка контекста суммы. Откройте баланс заново.",
    invalid_user_context_text="Ошибка контекста пользователя. Откройте баланс заново.",
    stale_context_text="Контекст вывода устарел. Откройте баланс заново.",
    requested_event_name="buyer_withdraw_requested",
    create_failed_event_name="buyer_withdraw_request_create_failed",
    idempotency_key_prefix="tg-withdraw",
    cancel_idempotency_key_prefix="tg-buyer-withdraw-cancel",
    cancel_return_line="Средства вернутся в доступный баланс покупателя.",
    cancel_success_text="Заявка на вывод отменена. Средства вернулись в доступный баланс.",
    balance_button_text="💳 Баланс и вывод",
)


class WithdrawalRequestCreationFlow:
    def __init__(
        self,
        *,
        config: WithdrawalFlowConfig,
        requester_adapter: WithdrawalRequesterAdapter,
        address_validator: TonMainnetAddressValidator,
    ) -> None:
        self._config = config
        self._requester_adapter = requester_adapter
        self._address_validator = address_validator

    async def start_manual_amount_prompt(self, *, requester_user_id: int) -> FlowResult:
        active_request = await self._requester_adapter.get_active_request(requester_user_id=requester_user_id)
        if active_request is not None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text=self._config.manual_active_text,
                        buttons=_back_to_balance_buttons(role=self._config.role),
                        parse_mode=None,
                    ),
                )
            )

        return FlowResult(
            effects=(
                SetPrompt(
                    role=self._config.role,
                    prompt_type=self._config.amount_prompt_type,
                    sensitive=False,
                    data={self._config.requester_id_key: requester_user_id},
                ),
                ReplaceText(
                    text="Введите сумму вывода в USDT (например, 4.5).",
                    buttons=_back_to_balance_buttons(role=self._config.role),
                    parse_mode=None,
                ),
            )
        )

    async def start_full_amount_prompt(self, *, requester_user_id: int) -> FlowResult:
        active_request = await self._requester_adapter.get_active_request(requester_user_id=requester_user_id)
        if active_request is not None:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text=self._config.full_active_text,
                        buttons=_back_to_balance_buttons(role=self._config.role),
                        parse_mode=None,
                    ),
                )
            )

        amount = await self._requester_adapter.get_available_balance(requester_user_id=requester_user_id)
        if amount <= _ZERO_USDT:
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Нет доступного баланса для вывода.",
                        buttons=_back_to_balance_buttons(role=self._config.role),
                        parse_mode=None,
                    ),
                )
            )

        return self._address_prompt_result(requester_user_id=requester_user_id, amount=amount, replace=True)

    async def submit_manual_amount(self, *, prompt_state: dict[str, Any], text: str) -> FlowResult:
        requester_user_id = int(prompt_state.get(self._config.requester_id_key, 0))
        if requester_user_id < 1:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(
                        text=self._config.invalid_amount_context_text,
                        buttons=_back_to_balance_buttons(role=self._config.role),
                        parse_mode=None,
                    ),
                )
            )

        active_request = await self._requester_adapter.get_active_request(requester_user_id=requester_user_id)
        if active_request is not None:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyRoleMenuText(
                        text=self._config.manual_active_text,
                        role=self._config.role,
                        parse_mode=None,
                    ),
                )
            )

        try:
            amount = Decimal(text)
        except InvalidOperation:
            return FlowResult(effects=(ReplyText(text="Неверный формат суммы. Повторите ввод.", parse_mode=None),))
        if amount <= _ZERO_USDT:
            return FlowResult(effects=(ReplyText(text="Сумма должна быть больше 0.", parse_mode=None),))

        available = await self._requester_adapter.get_available_balance(requester_user_id=requester_user_id)
        if amount > available:
            return FlowResult(
                effects=(
                    ReplyText(
                        text=(
                            "Сумма превышает доступный баланс.\n"
                            f"Сейчас доступно: {_format_usdt_value(available, precise=True)} USDT."
                        ),
                        parse_mode=None,
                    ),
                )
            )

        return self._address_prompt_result(requester_user_id=requester_user_id, amount=amount, replace=False)

    async def submit_address(
        self,
        *,
        prompt_state: dict[str, Any],
        text: str,
        telegram_id: int,
        username: str | None,
        update_id: int,
    ) -> FlowResult:
        requester_user_id = int(prompt_state.get(self._config.requester_id_key, 0))
        amount_raw = str(prompt_state.get("amount_usdt", "0"))
        try:
            amount = Decimal(amount_raw)
        except InvalidOperation:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(text=self._config.invalid_address_amount_context_text, parse_mode=None),
                )
            )

        payout_address = text.strip()
        if not payout_address:
            return FlowResult(effects=(ReplyText(text="Адрес не может быть пустым. Повторите ввод.", parse_mode=None),))
        if requester_user_id < 1:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(text=self._config.invalid_user_context_text, parse_mode=None),
                )
            )

        try:
            await self._address_validator.validate(address=payout_address)
        except ValueError as exc:
            return FlowResult(effects=(ReplyText(text=str(exc), parse_mode=None),))
        except AddressValidationUnavailable:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Не удалось проверить адрес через TonAPI. Повторите попытку позже.",
                        parse_mode=None,
                    ),
                )
            )

        requester = await self._requester_adapter.load_requester(telegram_id=telegram_id, username=username)
        if requester.user_id != requester_user_id:
            return FlowResult(
                effects=(
                    ClearPrompt(),
                    ReplyText(text=self._config.stale_context_text, parse_mode=None),
                )
            )

        try:
            withdrawal = await self._requester_adapter.create_withdrawal_request(
                requester=requester,
                amount_usdt=amount,
                payout_address=payout_address,
                idempotency_key=f"{self._config.idempotency_key_prefix}:{requester.user_id}:{update_id}",
            )
        except InsufficientFundsError:
            return FlowResult(
                effects=(
                    ReplyRoleMenuText(
                        text="Недостаточно доступного баланса для вывода.",
                        role=self._config.role,
                        parse_mode=None,
                    ),
                )
            )
        except InvalidStateError:
            return FlowResult(
                effects=(
                    ReplyRoleMenuText(
                        text=self._config.manual_active_text,
                        role=self._config.role,
                        parse_mode=None,
                    ),
                )
            )
        except Exception as exc:
            return FlowResult(
                effects=(
                    LogEvent(
                        event_name=self._config.create_failed_event_name,
                        fields={
                            f"{self._config.role}_user_id": requester.user_id,
                            "telegram_update_id": update_id,
                            "amount_usdt": str(amount),
                            "error_type": type(exc).__name__,
                            "error_message": str(exc)[:300],
                        },
                    ),
                    ClearPrompt(),
                    ReplyRoleMenuText(
                        text="Техническая ошибка при создании заявки на вывод. Баланс не изменен.",
                        role=self._config.role,
                        parse_mode=None,
                    ),
                )
            )

        return FlowResult(
            effects=(
                ClearPrompt(),
                LogEvent(
                    event_name=self._config.requested_event_name,
                    fields={
                        "telegram_update_id": update_id,
                        "withdrawal_request_id": withdrawal.withdrawal_request_id,
                        "withdrawal_ref": format_withdrawal_ref(withdrawal.withdrawal_request_id),
                    },
                ),
                ReplyRoleMenuText(
                    text="Заявка на вывод создана.\nСтатус: на проверке у администратора.",
                    role=self._config.role,
                    parse_mode=None,
                ),
            )
        )

    async def start_cancel_prompt(self, *, requester_user_id: int, request_id: int | None) -> FlowResult:
        if request_id is None:
            return self._missing_request_result()
        try:
            detail = await self._requester_adapter.get_withdrawal_request_detail(request_id=request_id)
        except NotFoundError:
            return self._no_longer_cancellable_result()
        if (
            detail.requester_user_id != requester_user_id
            or detail.requester_role != self._config.role
            or detail.status != "withdraw_pending_admin"
        ):
            return self._no_longer_cancellable_result()

        return FlowResult(
            effects=(
                ReplaceText(
                    text=_screen_text(
                        title="Отмена вывода",
                        cta="Подтвердите действие ниже.",
                        lines=[
                            f"<b>Сумма:</b> {_format_usdt_value(detail.amount_usdt, precise=True)} USDT",
                            f"<b>Адрес:</b> {html.escape(detail.payout_address)}",
                            self._config.cancel_return_line,
                        ],
                    ),
                    buttons=(
                        (
                            ButtonSpec(
                                text="✅ Отменить заявку",
                                flow=self._config.role,
                                action="withdraw_cancel_confirm",
                                entity_id=str(detail.withdrawal_request_id),
                            ),
                        ),
                        _back_to_balance_buttons(role=self._config.role)[0],
                    ),
                ),
            )
        )

    async def confirm_cancel(self, *, requester_user_id: int, request_id: int | None) -> FlowResult:
        if request_id is None:
            return self._missing_request_result()
        try:
            result = await self._requester_adapter.cancel_withdrawal_request(
                request_id=request_id,
                requester_user_id=requester_user_id,
                idempotency_key=f"{self._config.cancel_idempotency_key_prefix}:{requester_user_id}:{request_id}",
            )
        except (NotFoundError, InvalidStateError):
            return self._no_longer_cancellable_result()

        return FlowResult(
            effects=(
                ReplaceText(
                    text=self._config.cancel_success_text if result.changed else "Заявка уже была отменена ранее.",
                    buttons=(
                        (
                            ButtonSpec(
                                text=self._config.balance_button_text,
                                flow=self._config.role,
                                action="balance",
                            ),
                        ),
                    ),
                    parse_mode=None,
                ),
            )
        )

    def _address_prompt_result(self, *, requester_user_id: int, amount: Decimal, replace: bool) -> FlowResult:
        text = f"Введите адрес кошелька в сети TON для вывода {_format_usdt_value(amount, precise=True)} USDT."
        prompt = SetPrompt(
            role=self._config.role,
            prompt_type=self._config.address_prompt_type,
            sensitive=False,
            data={
                self._config.requester_id_key: requester_user_id,
                "amount_usdt": str(amount),
            },
        )
        screen = ReplaceText if replace else ReplyText
        return FlowResult(
            effects=(
                prompt,
                screen(
                    text=text,
                    buttons=_back_to_balance_buttons(role=self._config.role),
                    parse_mode=None,
                ),
            )
        )

    def _missing_request_result(self) -> FlowResult:
        return FlowResult(
            effects=(
                ReplaceText(
                    text="Не удалось определить заявку на вывод. Откройте баланс заново.",
                    buttons=_back_to_balance_buttons(role=self._config.role),
                    parse_mode=None,
                ),
            )
        )

    def _no_longer_cancellable_result(self) -> FlowResult:
        return FlowResult(
            effects=(
                ReplaceText(
                    text="Эту заявку уже нельзя отменить.",
                    buttons=_back_to_balance_buttons(role=self._config.role),
                    parse_mode=None,
                ),
            )
        )


def _back_to_balance_buttons(*, role: str) -> tuple[tuple[ButtonSpec, ...], ...]:
    return ((ButtonSpec(text="↩️ Назад к балансу", flow=role, action="balance"),),)


def _format_usdt_value(amount: Decimal, *, precise: bool = False) -> str:
    quant = _USDT_EXACT_QUANT if precise else Decimal("0.1")
    normalized = amount.quantize(quant, rounding=ROUND_HALF_UP)
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _screen_text(
    *,
    title: str,
    lines: list[str] | None = None,
    cta: str | None = None,
) -> str:
    decorated_title = f"💳 {title}" if title.startswith("Отмена вывода") else title
    parts: list[str] = [f"<b>{decorated_title}</b>"]
    if cta:
        parts.append(f"<i>{cta}</i>")
    if lines:
        filtered = [line for line in lines if line]
        if filtered:
            parts.append("\n".join(filtered))
    return "\n\n".join(parts)

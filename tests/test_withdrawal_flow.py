from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace

import pytest

from libs.domain.errors import InsufficientFundsError, InvalidStateError, NotFoundError
from services.bot_api.transport_effects import ClearPrompt, ReplaceText, ReplyRoleMenuText, ReplyText, SetPrompt
from services.bot_api.withdrawal_flow import (
    BUYER_WITHDRAWAL_CONFIG,
    SELLER_WITHDRAWAL_CONFIG,
    AddressValidationUnavailable,
    WithdrawalFlowConfig,
    WithdrawalRequestCreationFlow,
    WithdrawalRequester,
)


@dataclass
class FakeWithdrawalAdapter:
    role: str
    active_request: object | None = None
    available_balance: Decimal = Decimal("1.500000")
    requester: WithdrawalRequester = field(
        default_factory=lambda: WithdrawalRequester(user_id=101, available_account_id=301, pending_account_id=303)
    )
    detail: object | None = None
    cancel_result: object = field(default_factory=lambda: SimpleNamespace(changed=True))
    create_side_effect: Exception | None = None
    detail_side_effect: Exception | None = None
    cancel_side_effect: Exception | None = None
    active_calls: list[int] = field(default_factory=list)
    balance_calls: list[int] = field(default_factory=list)
    load_calls: list[tuple[int, str | None]] = field(default_factory=list)
    create_calls: list[dict[str, object]] = field(default_factory=list)
    detail_calls: list[int] = field(default_factory=list)
    cancel_calls: list[dict[str, object]] = field(default_factory=list)

    async def get_active_request(self, *, requester_user_id: int) -> object | None:
        self.active_calls.append(requester_user_id)
        return self.active_request

    async def get_available_balance(self, *, requester_user_id: int) -> Decimal:
        self.balance_calls.append(requester_user_id)
        return self.available_balance

    async def load_requester(self, *, telegram_id: int, username: str | None) -> WithdrawalRequester:
        self.load_calls.append((telegram_id, username))
        return self.requester

    async def create_withdrawal_request(
        self,
        *,
        requester: WithdrawalRequester,
        amount_usdt: Decimal,
        payout_address: str,
        idempotency_key: str,
    ) -> object:
        self.create_calls.append(
            {
                "requester": requester,
                "amount_usdt": amount_usdt,
                "payout_address": payout_address,
                "idempotency_key": idempotency_key,
            }
        )
        if self.create_side_effect is not None:
            raise self.create_side_effect
        return SimpleNamespace(withdrawal_request_id=77)

    async def get_withdrawal_request_detail(self, *, request_id: int) -> object:
        self.detail_calls.append(request_id)
        if self.detail_side_effect is not None:
            raise self.detail_side_effect
        if self.detail is None:
            raise NotFoundError("withdrawal not found")
        return self.detail

    async def cancel_withdrawal_request(
        self,
        *,
        request_id: int,
        requester_user_id: int,
        idempotency_key: str,
    ) -> object:
        self.cancel_calls.append(
            {
                "request_id": request_id,
                "requester_user_id": requester_user_id,
                "idempotency_key": idempotency_key,
            }
        )
        if self.cancel_side_effect is not None:
            raise self.cancel_side_effect
        return self.cancel_result


@dataclass
class FakeAddressValidator:
    side_effect: Exception | None = None
    calls: list[str] = field(default_factory=list)

    async def validate(self, *, address: str) -> None:
        self.calls.append(address)
        if self.side_effect is not None:
            raise self.side_effect


def _flow(
    config: WithdrawalFlowConfig,
    *,
    adapter: FakeWithdrawalAdapter | None = None,
    validator: FakeAddressValidator | None = None,
) -> tuple[WithdrawalRequestCreationFlow, FakeWithdrawalAdapter, FakeAddressValidator]:
    adapter = adapter or FakeWithdrawalAdapter(role=config.role)
    validator = validator or FakeAddressValidator()
    return (
        WithdrawalRequestCreationFlow(
            config=config,
            requester_adapter=adapter,
            address_validator=validator,
        ),
        adapter,
        validator,
    )


@pytest.mark.parametrize("config", [SELLER_WITHDRAWAL_CONFIG, BUYER_WITHDRAWAL_CONFIG])
@pytest.mark.asyncio
async def test_withdrawal_flow_prompts_manual_amount_for_role(config: WithdrawalFlowConfig) -> None:
    flow, _, _ = _flow(config)

    result = await flow.start_manual_amount_prompt(requester_user_id=101)

    prompt, screen = result.effects
    assert isinstance(prompt, SetPrompt)
    assert prompt.role == config.role
    assert prompt.prompt_type == config.amount_prompt_type
    assert prompt.data == {config.requester_id_key: 101}
    assert isinstance(screen, ReplaceText)
    assert screen.text == "Введите сумму вывода в USDT (например, 4.5)."
    assert screen.buttons[0][0].action == "balance"


@pytest.mark.parametrize("config", [SELLER_WITHDRAWAL_CONFIG, BUYER_WITHDRAWAL_CONFIG])
@pytest.mark.asyncio
async def test_withdrawal_flow_full_amount_uses_exact_available_balance(config: WithdrawalFlowConfig) -> None:
    flow, _, _ = _flow(config, adapter=FakeWithdrawalAdapter(role=config.role, available_balance=Decimal("1.234567")))

    result = await flow.start_full_amount_prompt(requester_user_id=101)

    prompt, screen = result.effects
    assert isinstance(prompt, SetPrompt)
    assert prompt.prompt_type == config.address_prompt_type
    assert prompt.data == {config.requester_id_key: 101, "amount_usdt": "1.234567"}
    assert isinstance(screen, ReplaceText)
    assert "1.234567 USDT" in screen.text


@pytest.mark.parametrize("config", [SELLER_WITHDRAWAL_CONFIG, BUYER_WITHDRAWAL_CONFIG])
@pytest.mark.asyncio
async def test_withdrawal_flow_blocks_new_request_when_active_request_exists(config: WithdrawalFlowConfig) -> None:
    adapter = FakeWithdrawalAdapter(role=config.role, active_request=object())
    flow, _, _ = _flow(config, adapter=adapter)

    manual = await flow.start_manual_amount_prompt(requester_user_id=101)
    full = await flow.start_full_amount_prompt(requester_user_id=101)

    assert isinstance(manual.effects[0], ReplaceText)
    assert manual.effects[0].text == config.manual_active_text
    assert isinstance(full.effects[0], ReplaceText)
    assert full.effects[0].text == config.full_active_text


@pytest.mark.parametrize("config", [SELLER_WITHDRAWAL_CONFIG, BUYER_WITHDRAWAL_CONFIG])
@pytest.mark.asyncio
async def test_withdrawal_flow_rejects_invalid_and_insufficient_manual_amounts(config: WithdrawalFlowConfig) -> None:
    adapter = FakeWithdrawalAdapter(role=config.role, available_balance=Decimal("1.000000"))
    flow, _, _ = _flow(config, adapter=adapter)
    prompt_state = {config.requester_id_key: 101}

    invalid = await flow.submit_manual_amount(prompt_state=prompt_state, text="abc")
    non_positive = await flow.submit_manual_amount(prompt_state=prompt_state, text="0")
    insufficient = await flow.submit_manual_amount(prompt_state=prompt_state, text="2.0")

    assert isinstance(invalid.effects[0], ReplyText)
    assert invalid.effects[0].text == "Неверный формат суммы. Повторите ввод."
    assert isinstance(non_positive.effects[0], ReplyText)
    assert non_positive.effects[0].text == "Сумма должна быть больше 0."
    assert isinstance(insufficient.effects[0], ReplyText)
    assert "Сумма превышает доступный баланс." in insufficient.effects[0].text
    assert adapter.create_calls == []


@pytest.mark.parametrize("config", [SELLER_WITHDRAWAL_CONFIG, BUYER_WITHDRAWAL_CONFIG])
@pytest.mark.asyncio
async def test_withdrawal_flow_validates_ton_address_before_request_creation(config: WithdrawalFlowConfig) -> None:
    validator = FakeAddressValidator(side_effect=ValueError("bad address"))
    flow, adapter, _ = _flow(config, validator=validator)

    result = await flow.submit_address(
        prompt_state={config.requester_id_key: 101, "amount_usdt": "1.500000"},
        text="bad-address",
        telegram_id=10001,
        username="user",
        update_id=501,
    )

    assert isinstance(result.effects[0], ReplyText)
    assert result.effects[0].text == "bad address"
    assert validator.calls == ["bad-address"]
    assert adapter.load_calls == []
    assert adapter.create_calls == []


@pytest.mark.parametrize("config", [SELLER_WITHDRAWAL_CONFIG, BUYER_WITHDRAWAL_CONFIG])
@pytest.mark.asyncio
async def test_withdrawal_flow_maps_tonapi_unavailable_without_creating_request(config: WithdrawalFlowConfig) -> None:
    validator = FakeAddressValidator(side_effect=AddressValidationUnavailable())
    flow, adapter, _ = _flow(config, validator=validator)

    result = await flow.submit_address(
        prompt_state={config.requester_id_key: 101, "amount_usdt": "1.500000"},
        text="UQ-wallet",
        telegram_id=10001,
        username="user",
        update_id=501,
    )

    assert isinstance(result.effects[0], ReplyText)
    assert result.effects[0].text == "Не удалось проверить адрес через TonAPI. Повторите попытку позже."
    assert adapter.create_calls == []


@pytest.mark.parametrize(
    ("config", "expected_key"),
    [(SELLER_WITHDRAWAL_CONFIG, "tg-seller-withdraw:101:501"), (BUYER_WITHDRAWAL_CONFIG, "tg-withdraw:101:501")],
)
@pytest.mark.asyncio
async def test_withdrawal_flow_creates_request_after_successful_address_validation(
    config: WithdrawalFlowConfig,
    expected_key: str,
) -> None:
    flow, adapter, validator = _flow(config)

    result = await flow.submit_address(
        prompt_state={config.requester_id_key: 101, "amount_usdt": "1.500000"},
        text="UQ-wallet",
        telegram_id=10001,
        username="user",
        update_id=501,
    )

    assert validator.calls == ["UQ-wallet"]
    assert adapter.load_calls == [(10001, "user")]
    assert adapter.create_calls[0]["idempotency_key"] == expected_key
    assert adapter.create_calls[0]["amount_usdt"] == Decimal("1.500000")
    assert isinstance(result.effects[0], ClearPrompt)
    assert isinstance(result.effects[-1], ReplyRoleMenuText)
    assert result.effects[-1].text.startswith("Заявка на вывод создана.")


@pytest.mark.parametrize(
    ("error", "expected_text"),
    [
        (InsufficientFundsError(), "Недостаточно доступного баланса для вывода."),
        (
            InvalidStateError(),
            "У вас уже есть активная заявка на вывод. Откройте баланс и отмените ее, если нужно создать новую.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_withdrawal_flow_maps_domain_creation_errors(error: Exception, expected_text: str) -> None:
    adapter = FakeWithdrawalAdapter(role="seller", create_side_effect=error)
    flow, _, _ = _flow(SELLER_WITHDRAWAL_CONFIG, adapter=adapter)

    result = await flow.submit_address(
        prompt_state={"seller_user_id": 101, "amount_usdt": "1.500000"},
        text="UQ-wallet",
        telegram_id=10001,
        username="seller",
        update_id=501,
    )

    assert isinstance(result.effects[0], ReplyRoleMenuText)
    assert result.effects[0].text == expected_text


@pytest.mark.parametrize("config", [SELLER_WITHDRAWAL_CONFIG, BUYER_WITHDRAWAL_CONFIG])
@pytest.mark.asyncio
async def test_withdrawal_flow_renders_cancel_prompt_for_owned_pending_request(config: WithdrawalFlowConfig) -> None:
    detail = SimpleNamespace(
        withdrawal_request_id=77,
        requester_user_id=101,
        requester_role=config.role,
        status="withdraw_pending_admin",
        amount_usdt=Decimal("1.250000"),
        payout_address="UQ-wallet",
    )
    adapter = FakeWithdrawalAdapter(role=config.role, detail=detail)
    flow, _, _ = _flow(config, adapter=adapter)

    result = await flow.start_cancel_prompt(requester_user_id=101, request_id=77)

    assert adapter.detail_calls == [77]
    assert isinstance(result.effects[0], ReplaceText)
    assert "<b>💳 Отмена вывода</b>" in result.effects[0].text
    assert "<b>Сумма:</b> 1.25 USDT" in result.effects[0].text
    assert config.cancel_return_line in result.effects[0].text
    assert result.effects[0].buttons[0][0].action == "withdraw_cancel_confirm"
    assert result.effects[0].buttons[0][0].entity_id == "77"


@pytest.mark.parametrize("config", [SELLER_WITHDRAWAL_CONFIG, BUYER_WITHDRAWAL_CONFIG])
@pytest.mark.asyncio
async def test_withdrawal_flow_cancel_prompt_rejects_wrong_owner_and_processed_request(
    config: WithdrawalFlowConfig,
) -> None:
    wrong_owner = SimpleNamespace(
        withdrawal_request_id=77,
        requester_user_id=999,
        requester_role=config.role,
        status="withdraw_pending_admin",
        amount_usdt=Decimal("1.250000"),
        payout_address="UQ-wallet",
    )
    processed = SimpleNamespace(
        withdrawal_request_id=77,
        requester_user_id=101,
        requester_role=config.role,
        status="withdraw_sent",
        amount_usdt=Decimal("1.250000"),
        payout_address="UQ-wallet",
    )

    wrong_owner_flow, _, _ = _flow(config, adapter=FakeWithdrawalAdapter(role=config.role, detail=wrong_owner))
    processed_flow, _, _ = _flow(config, adapter=FakeWithdrawalAdapter(role=config.role, detail=processed))

    wrong_owner_result = await wrong_owner_flow.start_cancel_prompt(requester_user_id=101, request_id=77)
    processed_result = await processed_flow.start_cancel_prompt(requester_user_id=101, request_id=77)

    assert isinstance(wrong_owner_result.effects[0], ReplaceText)
    assert wrong_owner_result.effects[0].text == "Эту заявку уже нельзя отменить."
    assert isinstance(processed_result.effects[0], ReplaceText)
    assert processed_result.effects[0].text == "Эту заявку уже нельзя отменить."


@pytest.mark.parametrize("config", [SELLER_WITHDRAWAL_CONFIG, BUYER_WITHDRAWAL_CONFIG])
@pytest.mark.asyncio
async def test_withdrawal_flow_cancel_prompt_handles_missing_request_id(config: WithdrawalFlowConfig) -> None:
    flow, adapter, _ = _flow(config)

    result = await flow.start_cancel_prompt(requester_user_id=101, request_id=None)

    assert adapter.detail_calls == []
    assert isinstance(result.effects[0], ReplaceText)
    assert result.effects[0].text == "Не удалось определить заявку на вывод. Откройте баланс заново."


@pytest.mark.parametrize(
    ("config", "expected_key"),
    [
        (SELLER_WITHDRAWAL_CONFIG, "tg-seller-withdraw-cancel:101:77"),
        (BUYER_WITHDRAWAL_CONFIG, "tg-buyer-withdraw-cancel:101:77"),
    ],
)
@pytest.mark.asyncio
async def test_withdrawal_flow_confirms_cancel_for_role(config: WithdrawalFlowConfig, expected_key: str) -> None:
    flow, adapter, _ = _flow(config, adapter=FakeWithdrawalAdapter(role=config.role))

    result = await flow.confirm_cancel(requester_user_id=101, request_id=77)

    assert adapter.cancel_calls == [
        {
            "request_id": 77,
            "requester_user_id": 101,
            "idempotency_key": expected_key,
        }
    ]
    assert isinstance(result.effects[0], ReplaceText)
    assert result.effects[0].text == config.cancel_success_text
    assert result.effects[0].buttons[0][0].text == config.balance_button_text


@pytest.mark.asyncio
async def test_withdrawal_flow_confirm_cancel_is_idempotent_when_domain_reports_no_change() -> None:
    adapter = FakeWithdrawalAdapter(role="seller", cancel_result=SimpleNamespace(changed=False))
    flow, _, _ = _flow(SELLER_WITHDRAWAL_CONFIG, adapter=adapter)

    result = await flow.confirm_cancel(requester_user_id=101, request_id=77)

    assert isinstance(result.effects[0], ReplaceText)
    assert result.effects[0].text == "Заявка уже была отменена ранее."


@pytest.mark.parametrize("error", [NotFoundError("missing"), InvalidStateError("processed")])
@pytest.mark.asyncio
async def test_withdrawal_flow_confirm_cancel_maps_no_longer_cancellable(error: Exception) -> None:
    adapter = FakeWithdrawalAdapter(role="seller", cancel_side_effect=error)
    flow, _, _ = _flow(SELLER_WITHDRAWAL_CONFIG, adapter=adapter)

    result = await flow.confirm_cancel(requester_user_id=101, request_id=77)

    assert isinstance(result.effects[0], ReplaceText)
    assert result.effects[0].text == "Эту заявку уже нельзя отменить."

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from libs.domain.errors import InsufficientFundsError, InvalidStateError, PayloadValidationError
from services.bot_api.admin_exceptions_flow import AdminExceptionsFlow
from services.bot_api.transport_effects import ClearPrompt, LogEvent, ReplaceText, ReplyText, SetPrompt


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def _pending_review(assignment_id: int = 31) -> SimpleNamespace:
    return _ns(
        assignment_id=assignment_id,
        task_uuid="11111111-1111-4111-8111-111111111111",
        listing_id=21,
        buyer_user_id=202,
        buyer_telegram_id=777001,
        buyer_username="buyer1",
        shop_title="Тушенка",
        display_title="Бумага A4 <премиум>",
        wb_product_id=552892532,
        reviewed_at=datetime(2026, 3, 18, 10, 30, 0, tzinfo=UTC),
        rating=4,
        review_text="Очень понравились, в размер.",
        review_phrases=["в размер", "не садятся после стирки"],
        verification_reason="Нужна оценка 5 из 5.",
    )


def _review_tx(chain_tx_id: int = 11, *, matched_intent_id: int | None = 22) -> SimpleNamespace:
    return _ns(
        chain_tx_id=chain_tx_id,
        tx_hash=f"0xtx{chain_tx_id}",
        amount_usdt=Decimal("1.200100"),
        review_reason="amount_mismatch",
        suffix_code=123,
        matched_intent_id=matched_intent_id,
    )


def _expired_intent(deposit_intent_id: int = 22) -> SimpleNamespace:
    return _ns(
        deposit_intent_id=deposit_intent_id,
        seller_telegram_id=10001,
        expected_amount_usdt=Decimal("1.200100"),
        suffix_code=123,
        expires_at=datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC),
    )


@dataclass
class FakeAdminReviewExceptionsAdapter:
    pending_reviews: list[Any] = field(default_factory=list)
    review_txs: list[Any] = field(default_factory=list)
    expired_intents: list[Any] = field(default_factory=list)
    verify_result: Any = field(
        default_factory=lambda: _ns(
            assignment_id=31,
            changed=True,
            verification_status="verified_manual",
        )
    )
    attach_result: Any = field(default_factory=lambda: _ns(changed=True, ledger_entry_id=801))
    cancel_result: bool = True
    verify_side_effect: Exception | None = None
    attach_side_effect: Exception | None = None
    cancel_side_effect: Exception | None = None
    verify_calls: list[dict[str, Any]] = field(default_factory=list)
    attach_calls: list[dict[str, Any]] = field(default_factory=list)
    cancel_calls: list[dict[str, Any]] = field(default_factory=list)

    async def list_pending_review_confirmations(self, *, limit: int = 1000) -> list[Any]:
        return self.pending_reviews[:limit]

    async def list_admin_review_txs(self, *, limit: int = 1000) -> list[Any]:
        return self.review_txs[:limit]

    async def list_admin_expired_intents(self, *, limit: int = 1000) -> list[Any]:
        return self.expired_intents[:limit]

    async def admin_verify_review_payload(
        self,
        *,
        admin_user_id: int,
        assignment_id: int,
        payload_base64: str,
        idempotency_key: str,
    ) -> Any:
        self.verify_calls.append(
            {
                "admin_user_id": admin_user_id,
                "assignment_id": assignment_id,
                "payload_base64": payload_base64,
                "idempotency_key": idempotency_key,
            }
        )
        if self.verify_side_effect is not None:
            raise self.verify_side_effect
        return self.verify_result

    async def credit_intent_from_chain_tx(
        self,
        *,
        deposit_intent_id: int,
        chain_tx_id: int,
        idempotency_key: str,
        admin_user_id: int,
        allow_expired: bool,
    ) -> Any:
        self.attach_calls.append(
            {
                "deposit_intent_id": deposit_intent_id,
                "chain_tx_id": chain_tx_id,
                "idempotency_key": idempotency_key,
                "admin_user_id": admin_user_id,
                "allow_expired": allow_expired,
            }
        )
        if self.attach_side_effect is not None:
            raise self.attach_side_effect
        return self.attach_result

    async def cancel_deposit_intent(
        self,
        *,
        deposit_intent_id: int,
        admin_user_id: int,
        reason: str,
        idempotency_key: str,
    ) -> bool:
        self.cancel_calls.append(
            {
                "deposit_intent_id": deposit_intent_id,
                "admin_user_id": admin_user_id,
                "reason": reason,
                "idempotency_key": idempotency_key,
            }
        )
        if self.cancel_side_effect is not None:
            raise self.cancel_side_effect
        return self.cancel_result


@pytest.mark.asyncio
async def test_admin_exceptions_flow_renders_empty_review_queue_with_deposit_counts() -> None:
    flow = AdminExceptionsFlow(
        adapter=FakeAdminReviewExceptionsAdapter(
            review_txs=[_review_tx(11), _review_tx(12, matched_intent_id=None)],
            expired_intents=[_expired_intent(22)],
        )
    )

    result = await flow.render_queue()

    [effect] = result.effects
    assert isinstance(effect, ReplaceText)
    assert "Отзывов на ручную проверку нет." in effect.text
    assert "Транзакция TX11" in effect.text
    assert "Счет: D22" in effect.text
    assert "Счет: не найден" in effect.text
    assert "Счет D22" in effect.text
    assert "Истек: 01.03.2026 15:00 MSK" in effect.text
    assert effect.buttons[0][0].text == "✅ Проверить отзыв · 0"
    assert effect.buttons[1][0].text == "🔗 Привязать платеж к счету · 2"
    assert effect.buttons[1][1].text == "🛑 Отменить счет · 1"


@pytest.mark.asyncio
async def test_admin_exceptions_flow_renders_populated_review_queue() -> None:
    flow = AdminExceptionsFlow(
        adapter=FakeAdminReviewExceptionsAdapter(pending_reviews=[_pending_review()])
    )

    result = await flow.render_queue()

    [effect] = result.effects
    assert isinstance(effect, ReplaceText)
    assert "Отзывы, требующие проверки:" in effect.text
    assert "Покупка P31" in effect.text
    assert "Товар: Бумага A4 &lt;премиум&gt;" in effect.text
    assert "Фразы: в размер, не садятся после стирки" in effect.text
    assert effect.buttons[0][0].text == "✅ Проверить отзыв · 1"


def test_admin_exceptions_flow_starts_sensitive_review_prompt() -> None:
    flow = AdminExceptionsFlow(adapter=FakeAdminReviewExceptionsAdapter())

    result = flow.start_review_verification_prompt(admin_user_id=9001)

    prompt, screen = result.effects
    assert isinstance(prompt, SetPrompt)
    assert prompt.role == "admin"
    assert prompt.prompt_type == "admin_review_verify"
    assert prompt.sensitive is True
    assert prompt.data == {"admin_user_id": 9001}
    assert isinstance(screen, ReplaceText)
    assert "Введите: <код_покупки> <base64_review_token>." in screen.text
    assert screen.parse_mode is None


def test_admin_exceptions_flow_starts_deposit_prompts() -> None:
    flow = AdminExceptionsFlow(adapter=FakeAdminReviewExceptionsAdapter())

    attach_prompt, attach_screen = flow.start_deposit_attach_prompt(admin_user_id=9001).effects
    cancel_prompt, cancel_screen = flow.start_deposit_cancel_prompt(admin_user_id=9001).effects

    assert isinstance(attach_prompt, SetPrompt)
    assert attach_prompt.prompt_type == "admin_deposit_attach"
    assert attach_prompt.sensitive is False
    assert "Введите: <код_транзакции> <код_счета>." in attach_screen.text
    assert isinstance(cancel_prompt, SetPrompt)
    assert cancel_prompt.prompt_type == "admin_deposit_cancel"
    assert cancel_prompt.sensitive is False
    assert "Введите: <код_счета> <причина>." in cancel_screen.text


@pytest.mark.asyncio
async def test_admin_exceptions_flow_verifies_review_with_stable_idempotency_key() -> None:
    adapter = FakeAdminReviewExceptionsAdapter()
    flow = AdminExceptionsFlow(adapter=adapter)

    result = await flow.submit_review_verification(
        prompt_state={"admin_user_id": 9001},
        text="P31 review-token==",
    )

    clear, reply, log = result.effects
    assert isinstance(clear, ClearPrompt)
    assert isinstance(reply, ReplyText)
    assert "Отзыв подтвержден вручную." in reply.text
    assert "Покупка: P31" in reply.text
    assert isinstance(log, LogEvent)
    assert log.event_name == "admin_review_verified"
    assert adapter.verify_calls == [
        {
            "admin_user_id": 9001,
            "assignment_id": 31,
            "payload_base64": "review-token==",
            "idempotency_key": "tg-admin-review-verify:9001:31",
        }
    ]


@pytest.mark.asyncio
async def test_admin_exceptions_flow_keeps_prompt_on_parse_errors() -> None:
    adapter = FakeAdminReviewExceptionsAdapter()
    flow = AdminExceptionsFlow(adapter=adapter)

    invalid_shape = await flow.submit_review_verification(
        prompt_state={"admin_user_id": 9001},
        text="P31",
    )
    invalid_ref = await flow.submit_review_verification(
        prompt_state={"admin_user_id": 9001},
        text="X31 token",
    )

    assert isinstance(invalid_shape.effects[0], ReplyText)
    assert invalid_shape.effects[0].text == "Формат: <код_покупки> <base64_review_token>"
    assert isinstance(invalid_ref.effects[0], ReplyText)
    assert invalid_ref.effects[0].text == "Используйте код покупки вида P31 или обычное число."
    assert adapter.verify_calls == []


@pytest.mark.asyncio
async def test_admin_exceptions_flow_maps_review_verification_failures() -> None:
    adapter = FakeAdminReviewExceptionsAdapter(verify_side_effect=PayloadValidationError("bad token"))
    flow = AdminExceptionsFlow(adapter=adapter)

    result = await flow.submit_review_verification(
        prompt_state={"admin_user_id": 9001},
        text="P31 bad-token",
    )

    clear, reply = result.effects
    assert isinstance(clear, ClearPrompt)
    assert isinstance(reply, ReplyText)
    assert "Проверьте, что токен относится к этой покупке" in reply.text
    assert reply.buttons[1][0].text == "⚠️ Исключения"


@pytest.mark.asyncio
async def test_admin_exceptions_flow_maps_stale_review_verification_state() -> None:
    adapter = FakeAdminReviewExceptionsAdapter(verify_side_effect=InvalidStateError("already processed"))
    flow = AdminExceptionsFlow(adapter=adapter)

    result = await flow.submit_review_verification(
        prompt_state={"admin_user_id": 9001},
        text="P31 token",
    )

    clear, reply = result.effects
    assert isinstance(clear, ClearPrompt)
    assert isinstance(reply, ReplyText)
    assert reply.text == "Не удалось подтвердить отзыв. Откройте исключения и попробуйте снова."


@pytest.mark.asyncio
async def test_admin_exceptions_flow_attaches_deposit_with_stable_idempotency_key() -> None:
    adapter = FakeAdminReviewExceptionsAdapter()
    flow = AdminExceptionsFlow(adapter=adapter)

    result = await flow.submit_deposit_attach(
        prompt_state={"admin_user_id": 9001},
        text="TX11 D22",
    )

    clear, reply, log = result.effects
    assert isinstance(clear, ClearPrompt)
    assert isinstance(reply, ReplyText)
    assert "Платеж привязан к счету и зачислен." in reply.text
    assert "Счет: D22" in reply.text
    assert "Транзакция: TX11" in reply.text
    assert isinstance(log, LogEvent)
    assert log.event_name == "admin_deposit_attach_processed"
    assert adapter.attach_calls == [
        {
            "deposit_intent_id": 22,
            "chain_tx_id": 11,
            "idempotency_key": "tg-admin-deposit-attach:9001:11:22",
            "admin_user_id": 9001,
            "allow_expired": True,
        }
    ]


@pytest.mark.asyncio
async def test_admin_exceptions_flow_maps_deposit_attach_failures() -> None:
    adapter = FakeAdminReviewExceptionsAdapter(attach_side_effect=InvalidStateError("processed"))
    flow = AdminExceptionsFlow(adapter=adapter)

    result = await flow.submit_deposit_attach(
        prompt_state={"admin_user_id": 9001},
        text="TX11 D22",
    )

    clear, reply = result.effects
    assert isinstance(clear, ClearPrompt)
    assert isinstance(reply, ReplyText)
    assert reply.text == "Не удалось привязать платеж к счету. Проверьте номера и попробуйте снова."

    adapter.attach_side_effect = InsufficientFundsError("system")
    insufficient = await flow.submit_deposit_attach(
        prompt_state={"admin_user_id": 9001},
        text="TX11 D22",
    )
    assert insufficient.effects[1].text == "Недостаточно средств на системном счете для зачисления."


@pytest.mark.asyncio
async def test_admin_exceptions_flow_cancels_deposit_invoice_with_reason() -> None:
    adapter = FakeAdminReviewExceptionsAdapter()
    flow = AdminExceptionsFlow(adapter=adapter)

    result = await flow.submit_deposit_cancel(
        prompt_state={"admin_user_id": 9001},
        text="D22 late_payment",
    )

    clear, reply, log = result.effects
    assert isinstance(clear, ClearPrompt)
    assert isinstance(reply, ReplyText)
    assert reply.text == "Счет D22 отменен."
    assert isinstance(log, LogEvent)
    assert log.event_name == "admin_deposit_cancel_processed"
    assert adapter.cancel_calls == [
        {
            "deposit_intent_id": 22,
            "admin_user_id": 9001,
            "reason": "late_payment",
            "idempotency_key": "tg-admin-deposit-cancel:9001:22",
        }
    ]


@pytest.mark.asyncio
async def test_admin_exceptions_flow_maps_deposit_cancel_failures() -> None:
    adapter = FakeAdminReviewExceptionsAdapter(cancel_side_effect=InvalidStateError("processed"))
    flow = AdminExceptionsFlow(adapter=adapter)

    result = await flow.submit_deposit_cancel(
        prompt_state={"admin_user_id": 9001},
        text="D22 late_payment",
    )

    clear, reply = result.effects
    assert isinstance(clear, ClearPrompt)
    assert isinstance(reply, ReplyText)
    assert reply.text == "Не удалось отменить счет. Проверьте номер и попробуйте снова."


@pytest.mark.asyncio
async def test_admin_exceptions_flow_keeps_deposit_prompts_on_parse_errors() -> None:
    adapter = FakeAdminReviewExceptionsAdapter()
    flow = AdminExceptionsFlow(adapter=adapter)

    attach_shape = await flow.submit_deposit_attach(prompt_state={"admin_user_id": 9001}, text="TX11")
    attach_ref = await flow.submit_deposit_attach(prompt_state={"admin_user_id": 9001}, text="bad D22")
    cancel_shape = await flow.submit_deposit_cancel(prompt_state={"admin_user_id": 9001}, text="D22")
    cancel_ref = await flow.submit_deposit_cancel(prompt_state={"admin_user_id": 9001}, text="bad reason")

    assert attach_shape.effects[0].text == "Формат: <код_транзакции> <код_счета>"
    assert attach_ref.effects[0].text == "Используйте коды вида TX11 D22 или обычные числа."
    assert cancel_shape.effects[0].text == "Формат: <код_счета> <причина>"
    assert cancel_ref.effects[0].text == "Код счета должен быть вида D22 или числом."
    assert adapter.attach_calls == []
    assert adapter.cancel_calls == []

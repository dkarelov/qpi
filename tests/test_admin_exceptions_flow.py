from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from libs.domain.errors import InvalidStateError, PayloadValidationError
from services.bot_api.admin_exceptions_flow import (
    AdminDepositExceptionSummary,
    AdminExceptionsFlow,
)
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


@dataclass
class FakeAdminReviewExceptionsAdapter:
    pending_reviews: list[Any] = field(default_factory=list)
    verify_result: Any = field(
        default_factory=lambda: _ns(
            assignment_id=31,
            changed=True,
            verification_status="verified_manual",
        )
    )
    verify_side_effect: Exception | None = None
    verify_calls: list[dict[str, Any]] = field(default_factory=list)

    async def list_pending_review_confirmations(self, *, limit: int = 1000) -> list[Any]:
        return self.pending_reviews[:limit]

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


@pytest.mark.asyncio
async def test_admin_exceptions_flow_renders_empty_review_queue_with_deposit_counts() -> None:
    flow = AdminExceptionsFlow(adapter=FakeAdminReviewExceptionsAdapter())

    result = await flow.render_queue(
        deposit_summary=AdminDepositExceptionSummary(
            lines=("⚠️ Пополнения, требующие проверки:", "Платежей на ручной разбор нет."),
            review_txs_count=2,
            expired_intents_count=1,
        )
    )

    [effect] = result.effects
    assert isinstance(effect, ReplaceText)
    assert "Отзывов на ручную проверку нет." in effect.text
    assert "Платежей на ручной разбор нет." in effect.text
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

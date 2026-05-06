from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Protocol

from libs.domain.errors import (
    InsufficientFundsError,
    InvalidStateError,
    ListingValidationError,
    NotFoundError,
)
from libs.domain.fx_rates import FxRateService
from libs.domain.listing_creation import parse_listing_create_csv, sanitize_buyer_display_title
from libs.domain.seller import SellerService
from libs.integrations.wb_public import WbObservedBuyerPrice, WbProductSnapshot

SELLER_LISTING_CREATE_PROMPT = "seller_listing_create"
SELLER_LISTING_MANUAL_PRICE_PROMPT = "seller_listing_manual_price"
SELLER_LISTING_TITLE_REVIEW_PROMPT = "seller_listing_create_review"
SELLER_LISTING_TITLE_EDIT_PROMPT = "seller_listing_title_edit"

SELLER_FLOW = "seller"
_USDT_EXACT_QUANT = Decimal("0.000001")
_USDT_SUMMARY_QUANT = Decimal("0.1")
_RUB_QUANT = Decimal("1")
_LISTING_COLLATERAL_FEE_MULTIPLIER = Decimal("1.01")


class SellerListingWorkflow(Protocol):
    async def load_listing_creation_snapshot(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
        wb_product_id: int,
    ) -> WbProductSnapshot: ...

    async def lookup_listing_buyer_price(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
        wb_product_id: int,
    ) -> WbObservedBuyerPrice | None: ...

    def reference_price_updated_at(
        self,
        *,
        observed_buyer_price: WbObservedBuyerPrice | None,
        reference_price_source: str,
    ) -> datetime: ...


@dataclass(frozen=True)
class ButtonSpec:
    text: str
    flow: str
    action: str
    entity_id: str = ""


@dataclass(frozen=True)
class ReplyText:
    text: str
    buttons: tuple[tuple[ButtonSpec, ...], ...] = ()
    parse_mode: str | None = "HTML"


@dataclass(frozen=True)
class ReplaceText:
    text: str
    buttons: tuple[tuple[ButtonSpec, ...], ...] = ()
    parse_mode: str | None = "HTML"


@dataclass(frozen=True)
class ReplyPhoto:
    photo_url: str | None


@dataclass(frozen=True)
class SetPrompt:
    prompt_type: str
    data: dict[str, Any]
    sensitive: bool = False


@dataclass(frozen=True)
class ClearPrompt:
    pass


@dataclass(frozen=True)
class LogEvent:
    event_name: str
    fields: dict[str, Any]


TransportEffect = ReplyText | ReplaceText | ReplyPhoto | SetPrompt | ClearPrompt | LogEvent


@dataclass(frozen=True)
class FlowResult:
    effects: tuple[TransportEffect, ...]


@dataclass(frozen=True)
class CommandListingCreateArgs:
    shop_id: int
    listing_input: str
    manual_price_rub: int | None
    display_title: str | None


@dataclass(frozen=True)
class CommandListingCreateResult:
    text: str


@dataclass(frozen=True)
class SellerListingCreationSession:
    seller_user_id: int
    shop_id: int
    shop_title: str
    wb_product_id: int
    cashback_rub: str
    reward_usdt: str
    slot_count: int
    search_phrase: str
    review_phrases: list[str]
    wb_source_title: str
    wb_subject_name: str | None
    wb_brand_name: str | None
    wb_vendor_code: str | None
    wb_description: str | None
    wb_photo_url: str | None
    wb_tech_sizes: list[str]
    wb_characteristics: list[dict[str, str]]
    reference_price_rub: int | None
    reference_price_source: str | None
    reference_price_updated_at: str | None
    seller_price_rub: int | None
    spp_percent: int | None
    suggested_display_title: str

    def to_prompt_data(self) -> dict[str, Any]:
        return {
            "seller_user_id": self.seller_user_id,
            "shop_id": self.shop_id,
            "shop_title": self.shop_title,
            "wb_product_id": self.wb_product_id,
            "cashback_rub": self.cashback_rub,
            "reward_usdt": self.reward_usdt,
            "slot_count": self.slot_count,
            "search_phrase": self.search_phrase,
            "review_phrases": self.review_phrases,
            "wb_source_title": self.wb_source_title,
            "wb_subject_name": self.wb_subject_name,
            "wb_brand_name": self.wb_brand_name,
            "wb_vendor_code": self.wb_vendor_code,
            "wb_description": self.wb_description,
            "wb_photo_url": self.wb_photo_url,
            "wb_tech_sizes": self.wb_tech_sizes,
            "wb_characteristics": self.wb_characteristics,
            "reference_price_rub": self.reference_price_rub,
            "reference_price_source": self.reference_price_source,
            "reference_price_updated_at": self.reference_price_updated_at,
            "seller_price_rub": self.seller_price_rub,
            "spp_percent": self.spp_percent,
            "suggested_display_title": self.suggested_display_title,
        }

    @classmethod
    def from_prompt_state(cls, prompt_state: dict[str, Any]) -> SellerListingCreationSession:
        return cls(
            seller_user_id=int(prompt_state.get("seller_user_id", 0)),
            shop_id=int(prompt_state.get("shop_id", 0)),
            shop_title=str(prompt_state.get("shop_title", "магазин")),
            wb_product_id=int(prompt_state.get("wb_product_id", 0)),
            cashback_rub=str(prompt_state.get("cashback_rub", "0")),
            reward_usdt=str(prompt_state.get("reward_usdt", "0")),
            slot_count=int(prompt_state.get("slot_count", 0)),
            search_phrase=str(prompt_state.get("search_phrase", "")).strip(),
            review_phrases=[str(item) for item in list(prompt_state.get("review_phrases") or [])],
            wb_source_title=str(prompt_state.get("wb_source_title", "")).strip(),
            wb_subject_name=_optional_prompt_text(prompt_state.get("wb_subject_name")),
            wb_brand_name=_optional_prompt_text(prompt_state.get("wb_brand_name")),
            wb_vendor_code=_optional_prompt_text(prompt_state.get("wb_vendor_code")),
            wb_description=_optional_prompt_text(prompt_state.get("wb_description")),
            wb_photo_url=_optional_prompt_text(prompt_state.get("wb_photo_url")),
            wb_tech_sizes=[str(item) for item in list(prompt_state.get("wb_tech_sizes") or [])],
            wb_characteristics=[
                {"name": str(item.get("name", "")), "value": str(item.get("value", ""))}
                for item in list(prompt_state.get("wb_characteristics") or [])
                if isinstance(item, dict)
            ],
            reference_price_rub=(
                int(prompt_state["reference_price_rub"])
                if prompt_state.get("reference_price_rub") is not None
                else None
            ),
            reference_price_source=_optional_prompt_text(prompt_state.get("reference_price_source")),
            reference_price_updated_at=_optional_prompt_text(prompt_state.get("reference_price_updated_at")),
            seller_price_rub=(
                int(prompt_state["seller_price_rub"]) if prompt_state.get("seller_price_rub") is not None else None
            ),
            spp_percent=(int(prompt_state["spp_percent"]) if prompt_state.get("spp_percent") is not None else None),
            suggested_display_title=str(prompt_state.get("suggested_display_title", "")).strip(),
        )

    def snapshot(self) -> WbProductSnapshot:
        return WbProductSnapshot(
            wb_product_id=self.wb_product_id,
            subject_name=self.wb_subject_name,
            vendor_code=self.wb_vendor_code,
            brand=self.wb_brand_name,
            name=self.wb_source_title,
            description=self.wb_description,
            photo_url=self.wb_photo_url,
            tech_sizes=self.wb_tech_sizes,
            characteristics=self.wb_characteristics,
        )

    def observed_buyer_price(self) -> WbObservedBuyerPrice | None:
        if self.reference_price_source != "orders":
            return None
        if self.reference_price_rub is None or self.seller_price_rub is None or self.spp_percent is None:
            return None
        return WbObservedBuyerPrice(
            buyer_price_rub=self.reference_price_rub,
            seller_price_rub=self.seller_price_rub,
            spp_percent=self.spp_percent,
            observed_at=(
                datetime.fromisoformat(self.reference_price_updated_at) if self.reference_price_updated_at else None
            ),
        )

    def with_manual_price(self, *, buyer_price_rub: int, now: datetime) -> SellerListingCreationSession:
        return SellerListingCreationSession(
            **{
                **self.to_prompt_data(),
                "reference_price_rub": buyer_price_rub,
                "reference_price_source": "manual",
                "reference_price_updated_at": now.isoformat(),
                "seller_price_rub": None,
                "spp_percent": None,
            }
        )

    def with_suggested_display_title(self, title: str) -> SellerListingCreationSession:
        return SellerListingCreationSession(
            **{
                **self.to_prompt_data(),
                "suggested_display_title": title.strip(),
            }
        )


class SellerListingCreationFlow:
    def __init__(
        self,
        *,
        seller_service: SellerService,
        seller_workflow: SellerListingWorkflow,
        display_rub_per_usdt: Decimal,
        fx_rate_service: FxRateService | None = None,
        fx_rate_ttl_seconds: int = 900,
    ) -> None:
        self._seller_service = seller_service
        self._seller_workflow = seller_workflow
        self._display_rub_per_usdt = display_rub_per_usdt
        self._fx_rate_service = fx_rate_service
        self._fx_rate_ttl_seconds = fx_rate_ttl_seconds

    def listing_create_usage_text(self) -> str:
        return (
            "Использование: /listing_create <shop_id> "
            "<артикул ВБ, кэшбэк в рублях, макс. заказов, поисковая фраза, "
            "фраза для отзыва 1, ... , фраза для отзыва 10> "
            "[|| <цена покупателя в рублях> [|| <название для покупателей>]]"
        )

    def instruction_text(self, *, shop_title: str) -> str:
        fx_text = _format_decimal(self._display_rub_per_usdt, quant=Decimal("0.01"))
        return _screen_text(
            title=f"Создание объявления для магазина «{html.escape(shop_title)}»",
            cta="Отправьте данные объявления одним сообщением в формате ниже.",
            lines=[
                (
                    "<b>Формат (через запятую):</b> "
                    "<code>артикул ВБ, кэшбэк в рублях, макс. заказов, поисковая фраза, "
                    "фраза для отзыва 1, ... , фраза для отзыва 10</code>"
                ),
                ("<b>Пример:</b> <code>12345678, 100, 5, женские джинсы, в размер, не садятся после стирки</code>"),
                f"<b>Кэшбэк:</b> сумма в ₽ для покупателя; бот зафиксирует ее в USDT по курсу ~{fx_text}.",
                "<b>Макс заказов:</b> количество покупателей по этому объявлению.",
                "<b>Поисковая фраза:</b> запрос, по которому покупатель будет искать товар.",
                (
                    "<b>Фразы для отзыва (необязательно):</b> 0-10 фраз; покупатель получит "
                    "до двух случайных фраз для отзыва."
                ),
            ],
            note=(
                "После этого бот загрузит карточку WB, определит цену покупателя по заказам за 30 дней "
                "или попросит ввести ее вручную."
            ),
        )

    def start_prompt(self, *, seller_user_id: int, shop_id: int, shop_title: str) -> FlowResult:
        return FlowResult(
            effects=(
                SetPrompt(
                    prompt_type=SELLER_LISTING_CREATE_PROMPT,
                    data={
                        "seller_user_id": seller_user_id,
                        "shop_id": shop_id,
                        "shop_title": shop_title,
                    },
                ),
                ReplaceText(
                    text=self.instruction_text(shop_title=shop_title),
                    buttons=_back_to_listings_buttons(),
                ),
            )
        )

    async def submit_listing_input(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
        shop_title: str,
        text: str,
    ) -> FlowResult:
        try:
            session = await self._prepare_session(
                seller_user_id=seller_user_id,
                shop_id=shop_id,
                shop_title=shop_title,
                listing_input=text,
                manual_price_rub=None,
                display_title=None,
            )
        except (ValueError, InvalidOperation):
            return FlowResult(
                effects=(
                    ReplyText(
                        text=self.instruction_text(shop_title=shop_title),
                        buttons=_back_to_listings_buttons(),
                    ),
                )
            )
        except ListingValidationError as exc:
            return FlowResult(effects=(ReplyText(text=str(exc), buttons=_back_to_listings_buttons(), parse_mode=None),))
        except (NotFoundError, InvalidStateError, InsufficientFundsError):
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Не удалось создать объявление.\nПроверьте токен магазина, баланс и введенные значения.",
                        buttons=_back_to_listings_buttons(),
                        parse_mode=None,
                    ),
                )
            )

        if session.reference_price_rub is None:
            return FlowResult(
                effects=(
                    SetPrompt(
                        prompt_type=SELLER_LISTING_MANUAL_PRICE_PROMPT,
                        data=session.to_prompt_data(),
                    ),
                    ReplyText(
                        text=self.manual_price_prompt_text(
                            wb_product_id=session.wb_product_id,
                            snapshot=session.snapshot(),
                        ),
                        buttons=_back_to_listings_buttons(),
                    ),
                )
            )

        return self._review_result(session=session)

    def submit_manual_price(
        self,
        *,
        prompt_state: dict[str, Any],
        text: str,
        now: datetime | None = None,
    ) -> FlowResult:
        try:
            buyer_price_rub = int(Decimal(text.strip()).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        except (InvalidOperation, ValueError):
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Неверный формат цены. Введите сумму в рублях, например 392.",
                        buttons=_back_to_listings_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        if buyer_price_rub < 1:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Цена должна быть больше 0.",
                        buttons=_back_to_listings_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        session = SellerListingCreationSession.from_prompt_state(prompt_state).with_manual_price(
            buyer_price_rub=buyer_price_rub,
            now=now or datetime.now(UTC),
        )
        return self._review_result(session=session)

    def title_edit_prompt(self, *, prompt_state: dict[str, Any]) -> FlowResult:
        session = SellerListingCreationSession.from_prompt_state(prompt_state)
        return FlowResult(
            effects=(
                SetPrompt(
                    prompt_type=SELLER_LISTING_TITLE_EDIT_PROMPT,
                    data=session.to_prompt_data(),
                ),
                ReplaceText(
                    text=self.title_edit_prompt_text(current_title=session.suggested_display_title),
                    buttons=_back_to_listings_buttons(),
                ),
            )
        )

    def submit_edited_title(self, *, prompt_state: dict[str, Any], text: str) -> FlowResult:
        suggested_display_title = text.strip()
        if not suggested_display_title:
            return FlowResult(
                effects=(
                    ReplyText(
                        text="Название для покупателей не может быть пустым. Отправьте новый текст.",
                        buttons=_back_to_listings_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        session = SellerListingCreationSession.from_prompt_state(prompt_state).with_suggested_display_title(
            suggested_display_title
        )
        return self._review_result(session=session)

    async def create_draft_from_prompt(self, *, prompt_state: dict[str, Any]) -> FlowResult:
        session = SellerListingCreationSession.from_prompt_state(prompt_state)
        try:
            listing = await self._create_listing_from_session(session=session)
        except (ValueError, NotFoundError, InvalidStateError, InsufficientFundsError):
            return FlowResult(
                effects=(
                    ReplaceText(
                        text="Не удалось создать объявление. Проверьте данные и попробуйте снова.",
                        buttons=_back_to_listings_buttons(),
                        parse_mode=None,
                    ),
                )
            )
        return FlowResult(
            effects=(
                ClearPrompt(),
                ReplaceText(
                    text=self.created_prompt_activation_text(
                        display_title=listing.display_title or listing.search_phrase,
                        wb_product_id=listing.wb_product_id,
                        wb_subject_name=listing.wb_subject_name,
                        wb_vendor_code=listing.wb_vendor_code,
                        wb_source_title=listing.wb_source_title,
                        wb_brand_name=listing.wb_brand_name,
                        reference_price_rub=listing.reference_price_rub,
                        reference_price_source=listing.reference_price_source,
                        search_phrase=listing.search_phrase,
                        review_phrases=getattr(listing, "review_phrases", []),
                        cashback_rub=(listing.reward_usdt * self._display_rub_per_usdt).quantize(
                            _RUB_QUANT, rounding=ROUND_HALF_UP
                        ),
                        reward_usdt=listing.reward_usdt,
                        slot_count=listing.slot_count,
                        collateral_required_usdt=listing.collateral_required_usdt,
                    ),
                    buttons=(
                        (
                            ButtonSpec(
                                text="✅ Активировать",
                                flow=SELLER_FLOW,
                                action="listing_activate",
                                entity_id=str(listing.listing_id),
                            ),
                        ),
                        (ButtonSpec(text="📦 К объявлениям", flow=SELLER_FLOW, action="listings"),),
                    ),
                ),
            )
        )

    async def create_from_command(
        self,
        *,
        seller_user_id: int,
        args: CommandListingCreateArgs,
    ) -> CommandListingCreateResult:
        session = await self._prepare_session(
            seller_user_id=seller_user_id,
            shop_id=args.shop_id,
            shop_title="магазин",
            listing_input=args.listing_input,
            manual_price_rub=args.manual_price_rub,
            display_title=args.display_title,
        )
        if session.reference_price_rub is None:
            return CommandListingCreateResult(
                text=(
                    "Не удалось определить цену покупателя по заказам WB за 30 дней.\n"
                    "Повторите команду и после данных объявления добавьте "
                    "`|| <цена покупателя в рублях>`."
                )
            )
        listing = await self._create_listing_from_session(session=session)
        review_phrases_text = ", ".join(session.review_phrases) if session.review_phrases else "—"
        return CommandListingCreateResult(
            text=(
                f"Листинг создан: id={listing.listing_id}, status={listing.status}\n"
                f"Название: {listing.display_title}\n"
                f"Артикул WB: {listing.wb_product_id}\n"
                f'Поиск: "{listing.search_phrase}"\n'
                f"Кэшбэк: {Decimal(session.cashback_rub).quantize(Decimal('1'), rounding=ROUND_HALF_UP)} ₽ "
                f"({listing.reward_usdt} USDT)\n"
                f"Цена покупателя: {session.reference_price_rub} ₽ ({session.reference_price_source})\n"
                f"Слоты: {listing.slot_count}\n"
                f"Фразы для отзыва: {review_phrases_text}"
            )
        )

    @staticmethod
    def parse_command_args(args: str) -> CommandListingCreateArgs:
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
            manual_price_rub = int(Decimal(segments[1]).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if manual_price_rub < 1:
                raise ValueError("manual_price_rub must be >= 1")

        display_title = None
        if len(segments) == 3:
            display_title = segments[2] or None

        return CommandListingCreateArgs(
            shop_id=shop_id,
            listing_input=segments[0],
            manual_price_rub=manual_price_rub,
            display_title=display_title,
        )

    def title_edit_prompt_text(self, *, current_title: str) -> str:
        return _screen_text(
            title="Название объявления",
            cta="Отправьте новое название следующим сообщением ниже.",
            lines=[
                f"<b>Текущее название:</b> {html.escape(current_title)}",
            ],
            note="Название увидят покупатели.",
        )

    def title_confirmation_text(
        self,
        *,
        wb_product_id: int,
        search_phrase: str,
        review_phrases: list[str] | None,
        cashback_rub: Decimal,
        slot_count: int,
        snapshot: WbProductSnapshot,
        suggested_display_title: str,
        buyer_price_rub: int,
        reference_price_source: str,
        observed_buyer_price: WbObservedBuyerPrice | None = None,
    ) -> str:
        reward_usdt = (cashback_rub / self._display_rub_per_usdt).quantize(
            _USDT_EXACT_QUANT,
            rounding=ROUND_HALF_UP,
        )
        collateral_required_usdt = (reward_usdt * Decimal(slot_count) * _LISTING_COLLATERAL_FEE_MULTIPLIER).quantize(
            _USDT_EXACT_QUANT, rounding=ROUND_HALF_UP
        )
        cashback_percent = _format_listing_cashback_percent(
            reference_price_rub=buyer_price_rub,
            cashback_rub=cashback_rub,
        )
        lines = [
            f"<b>Товар:</b> {html.escape(snapshot.name)}",
            f"<b>Артикул ВБ:</b> {wb_product_id}",
            f"<b>Поисковая фраза:</b> &quot;{html.escape(search_phrase)}&quot;",
            f"<b>Цена покупателя:</b> {_format_price_rub(buyer_price_rub)}",
            f"<b>Кэшбэк:</b> {_format_usdt_with_rub(reward_usdt, rub_per_usdt=self._display_rub_per_usdt)}",
            f"<b>Кэшбэк, %:</b> {cashback_percent}",
            f"<b>Макс. заказов:</b> {slot_count}",
            (
                "<b>Обеспечение:</b> "
                f"{_format_usdt_with_rub(collateral_required_usdt, rub_per_usdt=self._display_rub_per_usdt)}"
            ),
            f"<b>Фразы для отзыва:</b> {html.escape(_format_review_phrases_text(review_phrases))}",
            f"<b>Название для покупателей:</b> {html.escape(suggested_display_title)}",
        ]
        if observed_buyer_price is not None and reference_price_source == "orders":
            lines.append(
                "<b>Источник цены:</b> заказы за 30 дней, "
                f"цена продавца "
                f"{_format_price_rub(observed_buyer_price.seller_price_rub)}, "
                f"СПП {observed_buyer_price.spp_percent}%."
            )
        if reference_price_source == "manual":
            lines.append("<b>Источник цены:</b> введена вручную.")
        return _screen_text(
            title="Проверьте объявление",
            cta="Проверьте данные объявления и выберите следующее действие ниже.",
            lines=lines,
            note="Если название подходит, сохраните его. Если нет, отредактируйте название.",
        )

    def manual_price_prompt_text(self, *, wb_product_id: int, snapshot: WbProductSnapshot) -> str:
        return _screen_text(
            title="Нужна цена покупателя",
            cta="Введите текущую цену покупателя в рублях следующим сообщением ниже.",
            lines=[
                "Карточка товара найдена, но по заказам за 30 дней цену определить не удалось.",
                f"<b>Артикул ВБ:</b> {wb_product_id}",
                f"<b>Предмет:</b> {html.escape(snapshot.subject_name or '—')}",
                f"<b>Бренд:</b> {html.escape(snapshot.brand or '—')}",
                f"<b>Артикул продавца:</b> {html.escape(snapshot.vendor_code or '—')}",
                f"<b>Название WB:</b> {html.escape(snapshot.name)}",
            ],
            note="Укажите цену с учетом всех скидок. Пример: 392.",
        )

    def created_prompt_activation_text(
        self,
        *,
        display_title: str,
        wb_product_id: int,
        wb_subject_name: str | None,
        wb_vendor_code: str | None,
        wb_source_title: str | None,
        wb_brand_name: str | None,
        reference_price_rub: int | None,
        reference_price_source: str | None,
        search_phrase: str,
        review_phrases: list[str] | None,
        cashback_rub: Decimal,
        reward_usdt: Decimal,
        slot_count: int,
        collateral_required_usdt: Decimal,
    ) -> str:
        cashback_percent = _format_listing_cashback_percent(
            reference_price_rub=reference_price_rub,
            cashback_rub=cashback_rub,
        )
        lines = [
            f"<b>Товар:</b> {html.escape(display_title)}",
            f"<b>Артикул ВБ:</b> {wb_product_id}",
            f"<b>Поисковая фраза:</b> &quot;{html.escape(search_phrase)}&quot;",
            f"<b>Цена покупателя:</b> {_format_price_optional_rub(reference_price_rub)}",
            f"<b>Кэшбэк:</b> {_format_usdt_with_rub(reward_usdt, rub_per_usdt=self._display_rub_per_usdt)}",
            f"<b>Кэшбэк, %:</b> {cashback_percent}",
            f"<b>Макс. заказов:</b> {slot_count}",
            (
                "<b>Обеспечение:</b> "
                f"{_format_usdt_with_rub(collateral_required_usdt, rub_per_usdt=self._display_rub_per_usdt)}"
            ),
            f"<b>Фразы для отзыва:</b> {html.escape(_format_review_phrases_text(review_phrases))}",
        ]
        if wb_subject_name:
            lines.append(f"<b>Предмет:</b> {html.escape(wb_subject_name)}")
        if wb_vendor_code:
            lines.append(f"<b>Артикул продавца:</b> {html.escape(wb_vendor_code)}")
        if wb_source_title:
            lines.append(f"<b>Название WB:</b> {html.escape(wb_source_title)}")
        if wb_brand_name:
            lines.append(f"<b>Бренд WB:</b> {html.escape(wb_brand_name)}")
        if reference_price_source == "manual":
            lines.append("<b>Источник цены:</b> введена вручную.")
        elif reference_price_source == "orders":
            lines.append("<b>Источник цены:</b> рассчитана по заказам за 30 дней.")
        return (
            _screen_text(
                title="Проверьте объявление перед активацией",
                lines=lines,
                note="Если все верно, активируйте объявление и отправьте покупателям ссылку на магазин.",
            )
            + "\n\n<b>Активировать объявление сейчас?</b>"
        )

    async def _prepare_session(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
        shop_title: str,
        listing_input: str,
        manual_price_rub: int | None,
        display_title: str | None,
    ) -> SellerListingCreationSession:
        wb_product_id, cashback_rub, slot_count, search_phrase, review_phrases = parse_listing_create_csv(listing_input)
        if wb_product_id < 1:
            raise ValueError("wb_product_id must be >= 1")
        if cashback_rub <= Decimal("0"):
            raise ValueError("cashback_rub must be > 0")
        if slot_count < 1:
            raise ValueError("slot_count must be >= 1")
        if not search_phrase:
            raise ValueError("search_phrase must not be empty")

        fx_rate = await self._resolve_display_rub_per_usdt()
        reward_usdt = (cashback_rub / fx_rate).quantize(_USDT_EXACT_QUANT, rounding=ROUND_HALF_UP)
        if reward_usdt <= Decimal("0"):
            raise ValueError("reward_usdt must be > 0")

        snapshot = await self._seller_workflow.load_listing_creation_snapshot(
            seller_user_id=seller_user_id,
            shop_id=shop_id,
            wb_product_id=wb_product_id,
        )
        observed_buyer_price = await self._seller_workflow.lookup_listing_buyer_price(
            seller_user_id=seller_user_id,
            shop_id=shop_id,
            wb_product_id=wb_product_id,
        )
        reference_price_rub = (
            observed_buyer_price.buyer_price_rub if observed_buyer_price is not None else manual_price_rub
        )
        reference_price_source = (
            "orders" if observed_buyer_price is not None else ("manual" if manual_price_rub else None)
        )
        reference_price_updated_at = (
            self._seller_workflow.reference_price_updated_at(
                observed_buyer_price=observed_buyer_price,
                reference_price_source=reference_price_source,
            ).isoformat()
            if reference_price_source is not None
            else None
        )
        suggested_display_title = (display_title or "").strip() or sanitize_buyer_display_title(
            wb_product_id=wb_product_id,
            source_title=snapshot.name,
            brand_name=snapshot.brand,
        )
        return SellerListingCreationSession(
            seller_user_id=seller_user_id,
            shop_id=shop_id,
            shop_title=shop_title,
            wb_product_id=wb_product_id,
            cashback_rub=str(cashback_rub),
            reward_usdt=str(reward_usdt),
            slot_count=slot_count,
            search_phrase=search_phrase,
            review_phrases=review_phrases,
            wb_source_title=snapshot.name,
            wb_subject_name=snapshot.subject_name,
            wb_brand_name=snapshot.brand,
            wb_vendor_code=snapshot.vendor_code,
            wb_description=snapshot.description,
            wb_photo_url=snapshot.photo_url,
            wb_tech_sizes=snapshot.tech_sizes,
            wb_characteristics=snapshot.characteristics,
            reference_price_rub=reference_price_rub,
            reference_price_source=reference_price_source,
            reference_price_updated_at=reference_price_updated_at,
            seller_price_rub=observed_buyer_price.seller_price_rub if observed_buyer_price is not None else None,
            spp_percent=observed_buyer_price.spp_percent if observed_buyer_price is not None else None,
            suggested_display_title=suggested_display_title,
        )

    async def _create_listing_from_session(self, *, session: SellerListingCreationSession):
        return await self._seller_service.create_listing_draft(
            seller_user_id=session.seller_user_id,
            shop_id=session.shop_id,
            wb_product_id=session.wb_product_id,
            display_title=session.suggested_display_title,
            wb_source_title=session.wb_source_title,
            wb_subject_name=session.wb_subject_name,
            wb_brand_name=session.wb_brand_name,
            wb_vendor_code=session.wb_vendor_code,
            wb_description=session.wb_description,
            wb_photo_url=session.wb_photo_url,
            wb_tech_sizes=session.wb_tech_sizes,
            wb_characteristics=session.wb_characteristics,
            review_phrases=session.review_phrases,
            reference_price_rub=session.reference_price_rub,
            reference_price_source=session.reference_price_source,
            reference_price_updated_at=(
                datetime.fromisoformat(session.reference_price_updated_at)
                if session.reference_price_updated_at
                else None
            ),
            search_phrase=session.search_phrase,
            reward_usdt=Decimal(session.reward_usdt),
            slot_count=session.slot_count,
        )

    async def _resolve_display_rub_per_usdt(self) -> Decimal:
        if self._fx_rate_service is None:
            return self._display_rub_per_usdt
        try:
            rate = await self._fx_rate_service.get_usdt_rub_rate(
                max_age_seconds=self._fx_rate_ttl_seconds,
                fallback_rate=self._display_rub_per_usdt,
            )
        except Exception:
            return self._display_rub_per_usdt
        self._display_rub_per_usdt = rate
        return rate

    def _review_result(self, *, session: SellerListingCreationSession) -> FlowResult:
        effects: list[TransportEffect] = [
            SetPrompt(
                prompt_type=SELLER_LISTING_TITLE_REVIEW_PROMPT,
                data=session.to_prompt_data(),
            )
        ]
        if session.wb_photo_url:
            effects.append(ReplyPhoto(photo_url=session.wb_photo_url))
        effects.append(
            ReplyText(
                text=self.title_confirmation_text(
                    wb_product_id=session.wb_product_id,
                    search_phrase=session.search_phrase,
                    review_phrases=session.review_phrases,
                    cashback_rub=Decimal(session.cashback_rub),
                    slot_count=session.slot_count,
                    snapshot=session.snapshot(),
                    suggested_display_title=session.suggested_display_title,
                    buyer_price_rub=int(session.reference_price_rub or 0),
                    reference_price_source=str(session.reference_price_source or ""),
                    observed_buyer_price=session.observed_buyer_price(),
                ),
                buttons=_title_review_buttons(),
            )
        )
        return FlowResult(effects=tuple(effects))


def _screen_text(
    *,
    title: str,
    lines: list[str] | None = None,
    cta: str | None = None,
    note: str | None = None,
    warning: bool = False,
) -> str:
    title_prefix = "⚠️ " if warning else ""
    parts: list[str] = [f"<b>{title_prefix}{title}</b>"]
    if cta:
        parts.extend(["", f"<i>{cta}</i>"])
    if lines:
        parts.extend(["", "\n".join(lines)])
    if note:
        parts.extend(["", f"<i>{note}</i>"])
    return "\n".join(parts)


def _title_review_buttons() -> tuple[tuple[ButtonSpec, ...], ...]:
    return (
        (ButtonSpec(text="✅ Сохранить текущее название", flow=SELLER_FLOW, action="listing_title_keep"),),
        (ButtonSpec(text="✏️ Изменить название", flow=SELLER_FLOW, action="listing_title_edit_prompt"),),
        (ButtonSpec(text="↩️ К объявлениям", flow=SELLER_FLOW, action="listings"),),
    )


def _back_to_listings_buttons() -> tuple[tuple[ButtonSpec, ...], ...]:
    return ((ButtonSpec(text="↩️ Назад к объявлениям", flow=SELLER_FLOW, action="listings"),),)


def _format_listing_cashback_percent(
    *,
    reference_price_rub: int | Decimal | None,
    cashback_rub: Decimal,
) -> str:
    if reference_price_rub is None:
        return "—"
    reference = Decimal(str(reference_price_rub))
    if reference <= Decimal("0"):
        return "—"
    percent = (cashback_rub / reference * Decimal("100")).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )
    return f"~{percent}%"


def _format_review_phrases_text(review_phrases: list[str] | None) -> str:
    phrases = [str(item).strip() for item in (review_phrases or []) if str(item).strip()]
    return "; ".join(phrases) if phrases else "—"


def _format_decimal(value: Decimal, *, quant: Decimal) -> str:
    normalized = value.quantize(quant, rounding=ROUND_HALF_UP)
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _format_usdt_with_rub(amount: Decimal, *, rub_per_usdt: Decimal, precise: bool = False) -> str:
    quant = _USDT_EXACT_QUANT if precise else _USDT_SUMMARY_QUANT
    usdt = amount.quantize(quant, rounding=ROUND_HALF_UP)
    rub = (amount * rub_per_usdt).quantize(_RUB_QUANT, rounding=ROUND_HALF_UP)
    if usdt == Decimal("0.0") or usdt == Decimal("0.000000"):
        return "$0.0" if not precise else "$0.000000"
    usdt_text = format(usdt, "f")
    if not precise:
        usdt_text = usdt_text.rstrip("0").rstrip(".")
        if "." not in usdt_text:
            usdt_text += ".0"
    rub_text = _format_decimal(rub, quant=_RUB_QUANT)
    return f"${usdt_text} (~{rub_text} ₽)"


def _format_price_rub(amount: int | Decimal | None) -> str:
    if amount is None:
        return "—"
    rub = Decimal(str(amount)).quantize(_RUB_QUANT, rounding=ROUND_HALF_UP)
    text = _format_decimal(rub, quant=_RUB_QUANT)
    return f"{text} ₽"


def _format_price_optional_rub(amount: int | Decimal | None) -> str:
    return _format_price_rub(amount)


def _optional_prompt_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None

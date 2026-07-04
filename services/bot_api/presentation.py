"""Pure presentation formatting for bot screens.

Must stay free of flow, transport, and DB imports: this module is classified
as presentation-only (`marketplace_presentation` validation group) and deploys
through the hosted lane without DB-backed validation.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from libs.domain.public_refs import format_listing_ref, format_withdrawal_ref
from services.bot_api.transport_effects import ButtonSpec

DEFAULT_NUMBERED_PAGE_SIZE = 10
USDT_SUMMARY_QUANT = Decimal("0.1")
USDT_EXACT_QUANT = Decimal("0.000001")
RUB_QUANT = Decimal("1")
MSK_TZ = ZoneInfo("Europe/Moscow")

_TITLE_DECORATION_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("Инструкция", "Про "), "📘"),
    (("Кабинет продавца",), "🧑‍💼"),
    (("Кабинет покупателя",), "🛍️"),
    (
        (
            "Магазины",
            "Магазин",
            "Токен WB API",
            "Создание магазина",
            "Переименование магазина",
            "Удаление магазина",
        ),
        "🏪",
    ),
    (
        (
            "Объявления",
            "Название объявления",
            "Проверьте объявление",
            "Нужна цена покупателя",
            "Подтвердите изменения",
            "Редактирование объявления",
            "Новое объявление",
            "Удаление объявления",
            "Редактирование отключено",
            "🟢 ",
            "🔴 ",
        ),
        "📦",
    ),
    (("Покупки", "Покупка", "Токен-подтверждение", "Токен отзыва", "Отмена покупки"), "📋"),
    (("Счет на пополнение", "Как перевести USDT"), "💰"),
    (("Баланс", "Транзакции", "Отмена вывода"), "💳"),
)
_ALREADY_DECORATED_TITLE_PREFIXES = tuple(f"{emoji} " for _, emoji in _TITLE_DECORATION_RULES)
_STATUS_MARKERS = {
    "green": "🟢",
    "red": "🔴",
    "yellow": "🟡",
    "blue": "🔵",
}
_WITHDRAW_STATUS_LABELS = {
    "withdraw_pending_admin": "На проверке",
    "rejected": "Отклонено",
    "cancelled": "Отменено",
    "withdraw_sent": "Отправлено",
}
_WITHDRAW_STATUS_COLORS = {
    "withdraw_sent": "green",
    "rejected": "red",
    "withdraw_pending_admin": "yellow",
    "cancelled": "blue",
}


def screen_text(
    *,
    title: str,
    title_suffix_html: str | None = None,
    cta: str | None = None,
    lines: list[str] | None = None,
    note: str | None = None,
    warning: bool = False,
    separate_blocks: bool = False,
) -> str:
    decorated_title = decorate_screen_title(title)
    title_html = f"{'⚠️ ' if warning else ''}<b>{decorated_title}</b>"
    if title_suffix_html:
        title_html += title_suffix_html
    parts = [title_html]
    if cta:
        parts.append(f"<i>{cta}</i>")
    if lines:
        filtered = [line for line in lines if line]
        if filtered:
            parts.append(("\n\n" if separate_blocks else "\n").join(filtered))
    if note:
        parts.append(f"<i>{note}</i>")
    return "\n\n".join(parts)


def decorate_screen_title(title: str) -> str:
    plain_title = html.unescape(title)
    if title.startswith(_ALREADY_DECORATED_TITLE_PREFIXES):
        return title
    for prefixes, emoji in _TITLE_DECORATION_RULES:
        if plain_title.startswith(prefixes):
            return f"{emoji} {title}"
    return title


def status_badge(label: str, *, color: str) -> str:
    marker = _STATUS_MARKERS.get(color, "⚪")
    return f"{marker} {html.escape(label)}"


def humanize_withdraw_status(status: str) -> str:
    return _WITHDRAW_STATUS_LABELS.get(status, status)


def withdraw_status_badge(status: str) -> str:
    color = _WITHDRAW_STATUS_COLORS.get(status, "blue")
    return status_badge(humanize_withdraw_status(status), color=color)


def format_decimal(
    amount: Decimal,
    *,
    quant: Decimal,
    rounding=ROUND_HALF_UP,
) -> str:
    normalized = amount.quantize(quant, rounding=rounding)
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_usdt(amount: Decimal, *, precise: bool = False) -> str:
    if precise:
        return f"${format_decimal(amount, quant=USDT_EXACT_QUANT)}"
    normalized = amount.quantize(USDT_SUMMARY_QUANT, rounding=ROUND_HALF_UP)
    return f"${normalized:.1f}"


def format_usdt_value(amount: Decimal, *, precise: bool = False) -> str:
    quant = USDT_EXACT_QUANT if precise else USDT_SUMMARY_QUANT
    return format_decimal(amount, quant=quant)


def format_rub_approx(amount: Decimal, *, display_rub_per_usdt: Decimal) -> str:
    rub = amount * display_rub_per_usdt
    return f"~{format_decimal(rub, quant=RUB_QUANT)} ₽"


def format_usdt_with_rub(
    amount: Decimal,
    *,
    display_rub_per_usdt: Decimal,
    precise: bool = False,
) -> str:
    usdt = format_usdt(amount, precise=precise)
    if amount.quantize(USDT_EXACT_QUANT, rounding=ROUND_HALF_UP) == Decimal("0.000000"):
        return usdt
    return f"{usdt} ({format_rub_approx(amount, display_rub_per_usdt=display_rub_per_usdt)})"


def format_cashback_rub_value(amount: Decimal, *, display_rub_per_usdt: Decimal) -> str:
    return format_decimal(amount * display_rub_per_usdt, quant=RUB_QUANT)


def format_buyer_balance_amount(amount: Decimal, *, display_rub_per_usdt: Decimal) -> str:
    return format_rub_approx(amount, display_rub_per_usdt=display_rub_per_usdt)


def _cashback_percent_value(*, cashback_rub: Decimal, reference_price_rub: Decimal) -> Decimal:
    return (cashback_rub / reference_price_rub * Decimal("100")).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )


def cashback_percent(
    *,
    reward_usdt: Decimal,
    reference_price_rub: int | None,
    display_rub_per_usdt: Decimal,
) -> Decimal | None:
    if reward_usdt.quantize(USDT_EXACT_QUANT, rounding=ROUND_HALF_UP) == Decimal("0.000000"):
        return None
    if reference_price_rub is None or reference_price_rub < 1:
        return None
    cashback_rub = Decimal(format_cashback_rub_value(reward_usdt, display_rub_per_usdt=display_rub_per_usdt))
    return _cashback_percent_value(cashback_rub=cashback_rub, reference_price_rub=Decimal(reference_price_rub))


def format_buyer_cashback_with_percent(
    *,
    reward_usdt: Decimal,
    reference_price_rub: int | None,
    display_rub_per_usdt: Decimal,
) -> str:
    primary = format_rub_approx(reward_usdt, display_rub_per_usdt=display_rub_per_usdt)
    percent = cashback_percent(
        reward_usdt=reward_usdt,
        reference_price_rub=reference_price_rub,
        display_rub_per_usdt=display_rub_per_usdt,
    )
    if percent is None:
        return primary
    return f"{primary} (~{percent}%)"


def format_cashback_with_percent(
    *,
    reward_usdt: Decimal,
    reference_price_rub: int | None,
    display_rub_per_usdt: Decimal,
) -> str:
    percent = cashback_percent(
        reward_usdt=reward_usdt,
        reference_price_rub=reference_price_rub,
        display_rub_per_usdt=display_rub_per_usdt,
    )
    if percent is None:
        return format_usdt_with_rub(reward_usdt, display_rub_per_usdt=display_rub_per_usdt)
    rub_approx = format_rub_approx(reward_usdt, display_rub_per_usdt=display_rub_per_usdt)
    return f"{format_usdt(reward_usdt)} ({rub_approx}, ~{percent}%)"


def format_listing_cashback_percent(
    *,
    reference_price_rub: int | Decimal | None,
    cashback_rub: Decimal,
) -> str:
    if reference_price_rub is None:
        return "—"
    reference = Decimal(str(reference_price_rub))
    if reference <= Decimal("0"):
        return "—"
    return f"~{_cashback_percent_value(cashback_rub=cashback_rub, reference_price_rub=reference)}%"


def format_price_rub(amount: int | Decimal | None) -> str:
    if amount is None:
        return "0 ₽"
    rub = Decimal(str(amount)).quantize(RUB_QUANT, rounding=ROUND_CEILING)
    return f"{format_decimal(rub, quant=RUB_QUANT)} ₽"


def format_price_optional_rub(amount: int | Decimal | None) -> str:
    if amount is None:
        return "—"
    return format_price_rub(amount)


def format_listing_price_line(*, label: str, price_rub: int | None, source: str | None) -> str:
    if price_rub is None:
        return f"<b>{html.escape(label)}:</b> —"
    suffix = ""
    if source == "orders":
        suffix = " (из заказов)"
    elif source == "manual":
        suffix = " (вручную)"
    return f"<b>{html.escape(label)}:</b> {format_price_rub(price_rub)}{html.escape(suffix)}"


def format_datetime_msk(value: datetime | None) -> str:
    if value is None:
        return "—"
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    localized = normalized.astimezone(MSK_TZ)
    return localized.strftime("%d.%m.%Y %H:%M МСК")


def format_copyable_code(value: str) -> str:
    return f"<code>{html.escape(value.strip())}</code>"


def title_ref_suffix(value: str | None) -> str | None:
    if not value:
        return None
    return f" · {format_copyable_code(value)}"


def entity_block_heading(label: str) -> str:
    return f"<b>{html.escape(label)}</b>"


def entity_block_heading_with_ref(*, label: str, ref: str | None = None) -> str:
    heading = entity_block_heading(label)
    if not ref:
        return heading
    return f"{heading} · {format_copyable_code(ref)}"


def withdrawal_request_block_html(
    request: Any,
    *,
    label: str = "Активная заявка",
    ref: str | None = None,
) -> str:
    withdraw_ref = ref or format_withdrawal_ref(request.withdrawal_request_id)
    return "\n".join(
        [
            entity_block_heading_with_ref(label=label, ref=withdraw_ref),
            f"<b>Сумма:</b> {format_usdt_value(request.amount_usdt, precise=True)} USDT",
            f"<b>Статус:</b> {withdraw_status_badge(request.status)}",
            f"<b>Адрес:</b> {html.escape(request.payout_address)}",
            f"<b>Создана:</b> {format_datetime_msk(request.requested_at)}",
        ]
    )


def withdrawal_history_block_html(
    request: Any,
    *,
    label: str = "Вывод",
    ref: str | None = None,
) -> str:
    lines = [withdrawal_request_block_html(request, label=label, ref=ref)]
    if request.processed_at is not None:
        lines.append(f"<b>Обработана:</b> {format_datetime_msk(request.processed_at)}")
    if request.sent_at is not None:
        lines.append(f"<b>Отправлена:</b> {format_datetime_msk(request.sent_at)}")
    if request.note:
        lines.append(f"<b>Комментарий:</b> {html.escape(request.note)}")
    if request.tx_hash:
        lines.append(f"<b>Хэш перевода:</b> {html.escape(request.tx_hash)}")
    return "\n".join(lines)


def listing_display_title(*, display_title: str | None, fallback: str) -> str:
    normalized = (display_title or "").strip()
    return normalized or fallback.strip()


def _normalize_str_items(values: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        cleaned = str(value).strip()
        if cleaned:
            normalized.append(cleaned)
    return normalized


def normalize_sizes(sizes: list[str] | None) -> list[str]:
    return _normalize_str_items(sizes)


def normalize_review_phrases(review_phrases: list[str] | None) -> list[str]:
    return _normalize_str_items(review_phrases)


def format_review_phrases_text(
    review_phrases: list[str] | None,
    *,
    separator: str = "; ",
    empty_fallback: str = "не заданы",
) -> str:
    normalized = normalize_review_phrases(review_phrases)
    if not normalized:
        return empty_fallback
    return separator.join(normalized)


def should_show_buyer_sizes(sizes: list[str] | None) -> bool:
    return normalize_sizes(sizes) != ["0"]


def format_sizes_text(sizes: list[str] | None) -> str:
    normalized = normalize_sizes(sizes)
    if not normalized:
        return "—"
    return ", ".join(normalized)


def format_characteristics_block_html(characteristics: list[dict[str, str]] | None) -> str | None:
    if not characteristics:
        return None
    lines = []
    for item in characteristics:
        name = html.escape(str(item.get("name", "")).strip())
        value = html.escape(str(item.get("value", "")).strip())
        if not name or not value:
            continue
        lines.append(f"{name}: {value}")
    if not lines:
        return None
    return "<b>Характеристики</b>\n<blockquote expandable>" + "\n".join(lines) + "</blockquote>"


def format_expandable_block_html(*, title: str, body: str | None) -> str | None:
    normalized = (body or "").strip()
    if not normalized:
        return None
    return f"<b>{html.escape(title)}</b>\n<blockquote expandable>{html.escape(normalized)}</blockquote>"


def buyer_listing_detail_html(
    *,
    listing: Any,
    display_rub_per_usdt: Decimal,
    notice: str | None = None,
) -> str:
    display_title = listing_display_title(
        display_title=listing.display_title,
        fallback=listing.search_phrase,
    )
    lines: list[str] = []
    if notice:
        lines.append(html.escape(notice))
    cashback_text = format_buyer_cashback_with_percent(
        reward_usdt=listing.reward_usdt,
        reference_price_rub=listing.reference_price_rub,
        display_rub_per_usdt=display_rub_per_usdt,
    )
    lines.extend(
        [
            f"<b>Предмет:</b> {html.escape(listing.wb_subject_name or '—')}",
            format_listing_price_line(label="Цена", price_rub=listing.reference_price_rub, source=None),
            f"<b>Кэшбэк:</b> {html.escape(cashback_text)}",
            f"<b>Поисковая фраза:</b> &quot;{html.escape(listing.search_phrase)}&quot;",
        ]
    )
    if should_show_buyer_sizes(listing.wb_tech_sizes):
        lines.append(f"<b>Размеры:</b> {html.escape(format_sizes_text(listing.wb_tech_sizes))}")
    description_block = format_expandable_block_html(title="Описание", body=listing.wb_description)
    if description_block:
        lines.append(f"\n{description_block}")
    characteristics_block = format_characteristics_block_html(listing.wb_characteristics)
    if characteristics_block:
        lines.append(f"\n{characteristics_block}")
    return screen_text(
        title=f"📦 {display_title}",
        title_suffix_html=title_ref_suffix(format_listing_ref(listing.listing_id)),
        cta="Проверьте товар перед покупкой.",
        lines=lines,
        separate_blocks=True,
    )


def resolve_numbered_page(
    *,
    total_items: int,
    requested_page: int,
    page_size: int = DEFAULT_NUMBERED_PAGE_SIZE,
) -> tuple[int, int, int, int]:
    if total_items <= 0:
        return 1, 1, 0, 0
    total_pages = (total_items + page_size - 1) // page_size
    page = max(1, min(requested_page, total_pages))
    start_index = (page - 1) * page_size
    end_index = min(start_index + page_size, total_items)
    return page, total_pages, start_index, end_index


def page_nav_row(
    *,
    flow: str,
    page_action: str,
    page: int,
    total_pages: int,
    previous_label: str = "⬅️",
    next_label: str = "➡️",
) -> tuple[ButtonSpec, ...]:
    if total_pages <= 1:
        return ()
    nav_row: list[ButtonSpec] = []
    if page > 1:
        nav_row.append(ButtonSpec(text=previous_label, flow=flow, action=page_action, entity_id=str(page - 1)))
    if page < total_pages:
        nav_row.append(ButtonSpec(text=next_label, flow=flow, action=page_action, entity_id=str(page + 1)))
    return tuple(nav_row)


def numbered_page_buttons(
    *,
    flow: str,
    open_action: str,
    page_action: str,
    item_ids: list[int],
    start_number: int,
    page: int,
    total_pages: int,
    extra_rows: list[list[ButtonSpec]] | None = None,
) -> tuple[tuple[ButtonSpec, ...], ...]:
    rows: list[list[ButtonSpec]] = []
    current_row: list[ButtonSpec] = []
    for offset, item_id in enumerate(item_ids):
        current_row.append(
            ButtonSpec(text=str(start_number + offset), flow=flow, action=open_action, entity_id=str(item_id))
        )
        if len(current_row) == 5:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    nav_row = page_nav_row(flow=flow, page_action=page_action, page=page, total_pages=total_pages)
    if nav_row:
        rows.append(list(nav_row))

    if extra_rows:
        rows.extend(extra_rows)
    return tuple(tuple(row) for row in rows)


def button_label_with_count(label: str, count: int | None) -> str:
    if count is None:
        return label
    normalized_count = max(0, int(count))
    return f"{label} · {normalized_count}"

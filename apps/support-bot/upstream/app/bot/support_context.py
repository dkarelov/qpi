from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from app.bot.support_topics import SupportTopic, TelegramAccount

Role = Literal["buyer", "seller"]
Topic = Literal["generic", "shop", "listing", "purchase", "withdraw", "deposit"]

SUPPORTED_ROLES: set[str] = {"buyer", "seller"}
SUPPORTED_TOPICS: set[str] = {"generic", "shop", "listing", "purchase", "withdraw", "deposit"}
SUPPORTED_REF_KINDS: set[str] = {"S", "L", "P", "W", "D", "TX"}
TITLE_LIMIT = 128

_REF_RE = re.compile(r"^(TX|[SLPWD])([1-9][0-9]*)$")


@dataclass(frozen=True)
class SupportRef:
    kind: str
    id: int

    def render(self) -> str:
        return f"{self.kind}{self.id}"


@dataclass(frozen=True)
class SupportContext:
    role: Role | None = None
    topic: Topic = "generic"
    refs: tuple[SupportRef, ...] = ()

    @property
    def is_generic(self) -> bool:
        return self.role is None and self.topic == "generic" and not self.refs

    def label(self) -> str:
        if self.is_generic:
            return "generic"
        return f"{self.role}/{self.topic}"


GENERIC_CONTEXT = SupportContext()


def parse_support_ref(raw: str) -> SupportRef | None:
    match = _REF_RE.fullmatch(raw)
    if not match:
        return None
    kind, raw_id = match.groups()
    if kind not in SUPPORTED_REF_KINDS:
        return None
    return SupportRef(kind=kind, id=int(raw_id))


def parse_start_payload(payload: str | None) -> SupportContext:
    if not payload:
        return GENERIC_CONTEXT
    parts = [part for part in payload.strip().split("_") if part]
    if len(parts) < 2:
        return GENERIC_CONTEXT
    role, topic, *raw_refs = parts
    if role not in SUPPORTED_ROLES or topic not in SUPPORTED_TOPICS:
        return GENERIC_CONTEXT
    refs: list[SupportRef] = []
    for raw_ref in raw_refs:
        ref = parse_support_ref(raw_ref)
        if ref is None:
            return GENERIC_CONTEXT
        refs.append(ref)
    return SupportContext(role=cast(Role, role), topic=cast(Topic, topic), refs=tuple(refs))


def render_refs(refs: tuple[SupportRef, ...], *, separator: str = " ") -> str:
    return separator.join(ref.render() for ref in refs)


def _truncate_title(title: str, max_length: int) -> str:
    if len(title) <= max_length:
        return title
    if max_length <= 3:
        return "." * max_length
    return title[: max_length - 3].rstrip() + "..."


def render_topic_title(account: "TelegramAccount", context: SupportContext, max_length: int = TITLE_LIMIT) -> str:
    account_name = account.full_name.strip() or f"User {account.id}"
    if context.is_generic:
        title = account_name
    else:
        label = f"{context.role.title()} {context.topic}" if context.role else context.topic
        refs = render_refs(context.refs)
        prefix = f"{refs} · " if refs else ""
        title = f"{prefix}{label} · {account_name}"
    return _truncate_title(title, max_length=max_length)


def render_pinned_metadata(account: "TelegramAccount", topic: "SupportTopic") -> str:
    context = topic.context
    username = "-"
    if account.username:
        username = account.username if account.username.startswith("@") else f"@{account.username}"
    refs = render_refs(context.refs, separator=", ") or "-"
    flags = []
    if topic.is_banned:
        flags.append("banned")
    if topic.is_silent:
        flags.append("silent")
    return "\n".join(
        [
            "Support Topic",
            f"Telegram ID: {account.id}",
            f"Username: {username}",
            f"Name: {account.full_name}",
            f"Context: {context.label()}",
            f"Refs: {refs}",
            f"State: {topic.status}",
            f"Flags: {', '.join(flags) if flags else '-'}",
        ]
    )

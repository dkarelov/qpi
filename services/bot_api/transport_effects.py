from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ButtonSpec:
    text: str
    flow: str | None = None
    action: str | None = None
    entity_id: str = ""
    url: str | None = None

    def __post_init__(self) -> None:
        has_callback = bool(self.flow and self.action)
        has_url = bool(self.url)
        if has_callback == has_url:
            raise ValueError("ButtonSpec must describe exactly one callback button or URL button")


@dataclass(frozen=True)
class ReplyText:
    text: str
    buttons: tuple[tuple[ButtonSpec, ...], ...] = ()
    parse_mode: str | None = "HTML"


@dataclass(frozen=True)
class ReplyRoleMenuText:
    text: str
    role: str
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
    role: str | None = None


@dataclass(frozen=True)
class ClearPrompt:
    pass


@dataclass(frozen=True)
class SetUserData:
    key: str
    value: Any


@dataclass(frozen=True)
class AnswerCallback:
    text: str | None = None
    show_alert: bool = False


@dataclass(frozen=True)
class DeleteSourceMessage:
    pass


@dataclass(frozen=True)
class LogEvent:
    event_name: str
    fields: dict[str, Any]


TransportEffect = (
    ReplyText
    | ReplyRoleMenuText
    | ReplaceText
    | ReplyPhoto
    | SetPrompt
    | ClearPrompt
    | SetUserData
    | AnswerCallback
    | DeleteSourceMessage
    | LogEvent
)


@dataclass(frozen=True)
class FlowResult:
    effects: tuple[TransportEffect, ...]

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from services.bot_api.callback_data import build_callback


@dataclass
class TransportEvent:
    kind: str
    text: str | None = None
    parse_mode: str | None = None
    show_alert: bool | None = None
    reply_markup: Any | None = None
    chat_id: int | None = None


@dataclass
class FakeTransport:
    events: list[TransportEvent] = field(default_factory=list)

    def record(
        self,
        *,
        kind: str,
        text: str | None = None,
        parse_mode: str | None = None,
        show_alert: bool | None = None,
        reply_markup: Any | None = None,
        chat_id: int | None = None,
    ) -> None:
        self.events.append(
            TransportEvent(
                kind=kind,
                text=text,
                parse_mode=parse_mode,
                show_alert=show_alert,
                reply_markup=reply_markup,
                chat_id=chat_id,
            )
        )

    def since(self, offset: int) -> list[TransportEvent]:
        return self.events[offset:]

    def find(self, kind: str) -> list[TransportEvent]:
        return [event for event in self.events if event.kind == kind]


class FakeChat:
    def __init__(self, *, transport: FakeTransport, chat_id: int) -> None:
        self._transport = transport
        self.id = chat_id

    async def send_message(
        self,
        text: str,
        *,
        reply_markup: Any | None = None,
        parse_mode: str | None = None,
    ) -> None:
        self._transport.record(
            kind="chat_send",
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            chat_id=self.id,
        )


class FakeMessage:
    def __init__(
        self,
        *,
        transport: FakeTransport,
        chat: FakeChat,
        text: str | None = None,
        edit_fails: bool = False,
        from_user: SimpleNamespace | None = None,
    ) -> None:
        self._transport = transport
        self.chat = chat
        self.text = text
        self._edit_fails = edit_fails
        self.from_user = from_user

    async def reply_text(
        self,
        text: str,
        *,
        reply_markup: Any | None = None,
        parse_mode: str | None = None,
    ) -> None:
        self._transport.record(
            kind="reply",
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            chat_id=self.chat.id,
        )

    async def edit_text(
        self,
        text: str,
        *,
        reply_markup: Any | None = None,
        parse_mode: str | None = None,
    ) -> None:
        if self._edit_fails:
            raise RuntimeError("simulated edit failure")
        self._transport.record(
            kind="edit",
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            chat_id=self.chat.id,
        )

    async def delete(self) -> None:
        self._transport.record(kind="delete", chat_id=self.chat.id)


class FakeBot:
    def __init__(self, *, transport: FakeTransport) -> None:
        self._transport = transport

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: Any | None = None,
        parse_mode: str | None = None,
    ) -> None:
        self._transport.record(
            kind="bot_send",
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            chat_id=chat_id,
        )


class FakeCallbackQuery:
    def __init__(
        self,
        *,
        transport: FakeTransport,
        callback_data: str,
        from_user: SimpleNamespace,
        message: FakeMessage | None,
        query_id: str = "cbq-1",
    ) -> None:
        self._transport = transport
        self.data = callback_data
        self.from_user = from_user
        self.message = message
        self.id = query_id

    async def answer(
        self,
        text: str | None = None,
        *,
        show_alert: bool | None = None,
    ) -> None:
        self._transport.record(
            kind="callback_answer",
            text=text,
            show_alert=show_alert,
            chat_id=(self.message.chat.id if self.message is not None else None),
        )


@dataclass
class FakeContext:
    bot: FakeBot
    user_data: dict[str, Any] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)


class TelegramRuntimeHarness:
    def __init__(
        self,
        runtime,
        *,
        telegram_id: int,
        username: str,
        chat_id: int | None = None,
    ) -> None:
        self.runtime = runtime
        self.transport = FakeTransport()
        self.user = SimpleNamespace(id=telegram_id, username=username)
        self.chat = FakeChat(
            transport=self.transport,
            chat_id=chat_id if chat_id is not None else telegram_id,
        )
        self.context = FakeContext(bot=FakeBot(transport=self.transport))
        self._next_update_id = 1

    def _allocate_update_id(self) -> int:
        value = self._next_update_id
        self._next_update_id += 1
        return value

    async def start(self, *, start_arg: str | None = None) -> list[TransportEvent]:
        offset = len(self.transport.events)
        self.context.args = [start_arg] if start_arg else []
        message = FakeMessage(
            transport=self.transport,
            chat=self.chat,
            text="/start",
            from_user=self.user,
        )
        update = SimpleNamespace(
            update_id=self._allocate_update_id(),
            message=message,
            callback_query=None,
        )
        await self.runtime._handle_start(update, self.context)
        return self.transport.since(offset)

    async def text(self, text: str) -> list[TransportEvent]:
        offset = len(self.transport.events)
        message = FakeMessage(
            transport=self.transport,
            chat=self.chat,
            text=text,
            from_user=self.user,
        )
        update = SimpleNamespace(
            update_id=self._allocate_update_id(),
            message=message,
            callback_query=None,
        )
        await self.runtime._handle_text(update, self.context)
        return self.transport.since(offset)

    async def callback(
        self,
        *,
        flow: str,
        action: str,
        entity_id: str = "",
        edit_fails: bool = False,
        with_message: bool = True,
        query_id: str = "cbq-1",
    ) -> list[TransportEvent]:
        offset = len(self.transport.events)
        query_message = (
            FakeMessage(
                transport=self.transport,
                chat=self.chat,
                edit_fails=edit_fails,
                from_user=self.user,
            )
            if with_message
            else None
        )
        callback = FakeCallbackQuery(
            transport=self.transport,
            callback_data=build_callback(flow=flow, action=action, entity_id=entity_id),
            from_user=self.user,
            message=query_message,
            query_id=query_id,
        )
        update = SimpleNamespace(
            update_id=self._allocate_update_id(),
            message=None,
            callback_query=callback,
        )
        await self.runtime._handle_callback(update, self.context)
        return self.transport.since(offset)

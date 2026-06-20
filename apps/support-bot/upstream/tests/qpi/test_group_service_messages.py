import pytest
from aiogram.exceptions import TelegramBadRequest


@pytest.mark.asyncio
async def test_undeletable_topic_service_message_does_not_escape_to_error_handler() -> None:
    from app.bot.handlers.group import message as group_message

    cleanup_handlers = [
        handler.callback
        for handler in group_message.router.message.handlers
        if "Delete service messages" in (handler.callback.__doc__ or "")
    ]
    assert len(cleanup_handlers) == 1

    class FakeMessage:
        async def delete(self) -> None:
            raise TelegramBadRequest(method=object(), message="Bad Request: message can't be deleted")

    await cleanup_handlers[0](FakeMessage())

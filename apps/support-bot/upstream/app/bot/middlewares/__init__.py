from aiogram import Dispatcher

from .album import AlbumMiddleware
from .manager import ManagerMiddleware
from .redis import RedisMiddleware
from .throttling import ThrottlingMiddleware


def register_middlewares(dp: Dispatcher, **kwargs) -> None:
    """Register bot middlewares."""
    dp.update.outer_middleware.register(RedisMiddleware(kwargs["pool"], schema=kwargs.get("schema", "support_bot")))
    dp.update.outer_middleware.register(ManagerMiddleware())
    dp.message.middleware.register(AlbumMiddleware())
    dp.message.middleware.register(ThrottlingMiddleware())


__all__ = [
    "register_middlewares",
]

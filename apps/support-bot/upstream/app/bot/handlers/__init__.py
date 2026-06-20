from aiogram import Dispatcher

from . import errors, group, private


def include_routers(dp: Dispatcher) -> None:
    """
    Include bot routers.

    :param dp: Dispatcher object.
    :return: None
    """
    dp.include_routers(
        *[
            *group.routers,
            *private.routers,
            errors.router,
        ]
    )


__all__ = [
    "include_routers",
]

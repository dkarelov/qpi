ACTIVE_PURCHASE_LISTING_NOTICE = (
    "У вас уже есть активная покупка по этому товару. Продолжить можно в разделе «Покупки»."
)
ALREADY_PURCHASED_LISTING_NOTICE = (
    "Этот товар уже был куплен с вашего аккаунта. Повторно забронировать нельзя. "
    "Посмотреть покупку можно в разделе «Покупки»."
)


def repeat_purchase_listing_notice(action_state: str | None) -> str | None:
    if action_state == "active_purchase":
        return ACTIVE_PURCHASE_LISTING_NOTICE
    if action_state == "already_purchased":
        return ALREADY_PURCHASED_LISTING_NOTICE
    return None

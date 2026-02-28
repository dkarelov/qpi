from libs.domain.seller import _slugify


def test_slugify_transliterates_cyrillic_title() -> None:
    assert _slugify("тушенка для всех") == "tushenka_dlya_vseh"


def test_slugify_falls_back_to_shop_when_no_alnum() -> None:
    assert _slugify("!!!") == "shop"

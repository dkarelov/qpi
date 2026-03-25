from __future__ import annotations

from libs.domain.errors import ListingValidationError
from libs.domain.models import StatusChangeResult
from libs.domain.seller import SellerService
from libs.integrations.wb_public import WbProductSnapshot, WbPublicApiError, WbPublicCatalogClient
from libs.security.token_cipher import decrypt_token


class SellerWorkflowService:
    """Backend workflow facade for seller actions that need external WB validation."""

    def __init__(
        self,
        *,
        seller_service: SellerService,
        wb_public_client: WbPublicCatalogClient,
        token_cipher_key: str,
    ) -> None:
        self._seller_service = seller_service
        self._wb_public_client = wb_public_client
        self._token_cipher_key = token_cipher_key

    async def validate_listing_product_availability(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
        wb_product_id: int,
    ) -> WbProductSnapshot:
        token = await self._load_shop_wb_token(
            seller_user_id=seller_user_id,
            shop_id=shop_id,
        )
        try:
            return await self._wb_public_client.fetch_product_snapshot(
                token=token,
                wb_product_id=wb_product_id,
            )
        except WbPublicApiError as exc:
            raise ListingValidationError(
                "Товар сейчас недоступен на WB или его карточка не читается. Попробуйте позже."
            ) from exc

    async def activate_listing(
        self,
        *,
        seller_user_id: int,
        listing_id: int,
        idempotency_key: str,
    ) -> StatusChangeResult:
        listing = await self._seller_service.get_listing(
            seller_user_id=seller_user_id,
            listing_id=listing_id,
        )
        await self.validate_listing_product_availability(
            seller_user_id=seller_user_id,
            shop_id=listing.shop_id,
            wb_product_id=listing.wb_product_id,
        )
        return await self._seller_service.activate_listing(
            seller_user_id=seller_user_id,
            listing_id=listing_id,
            idempotency_key=idempotency_key,
        )

    async def unpause_listing(
        self,
        *,
        seller_user_id: int,
        listing_id: int,
    ) -> StatusChangeResult:
        listing = await self._seller_service.get_listing(
            seller_user_id=seller_user_id,
            listing_id=listing_id,
        )
        await self.validate_listing_product_availability(
            seller_user_id=seller_user_id,
            shop_id=listing.shop_id,
            wb_product_id=listing.wb_product_id,
        )
        return await self._seller_service.unpause_listing(
            seller_user_id=seller_user_id,
            listing_id=listing_id,
        )

    async def _load_shop_wb_token(self, *, seller_user_id: int, shop_id: int) -> str:
        ciphertext = await self._seller_service.get_validated_shop_token_ciphertext(
            seller_user_id=seller_user_id,
            shop_id=shop_id,
        )
        try:
            return decrypt_token(ciphertext, self._token_cipher_key)
        except Exception as exc:
            raise ListingValidationError(
                "Токен магазина не удалось прочитать. Загрузите токен заново."
            ) from exc

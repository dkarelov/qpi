from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class TransferResult:
    entry_id: int
    created: bool


@dataclass(frozen=True)
class AssignmentReservationResult:
    assignment_id: int
    created: bool
    reward_usdt: Decimal
    reservation_expires_at: datetime


@dataclass(frozen=True)
class WithdrawalRequestResult:
    withdrawal_request_id: int
    created: bool


@dataclass(frozen=True)
class ManualDepositResult:
    manual_deposit_id: int
    ledger_entry_id: int
    created: bool


@dataclass(frozen=True)
class StatusChangeResult:
    changed: bool


@dataclass(frozen=True)
class SellerBootstrapResult:
    user_id: int
    created_user: bool
    seller_available_account_id: int
    seller_collateral_account_id: int


@dataclass(frozen=True)
class ShopResult:
    shop_id: int
    slug: str
    title: str
    deleted_at: datetime | None


@dataclass(frozen=True)
class ListingResult:
    listing_id: int
    shop_id: int
    status: str
    reward_usdt: Decimal
    slot_count: int
    available_slots: int
    deleted_at: datetime | None


@dataclass(frozen=True)
class SellerBalanceSnapshot:
    seller_available_usdt: Decimal
    seller_collateral_usdt: Decimal


@dataclass(frozen=True)
class SellerListingCollateralView:
    listing_id: int
    shop_id: int
    status: str
    reward_usdt: Decimal
    slot_count: int
    available_slots: int
    collateral_required_usdt: Decimal
    collateral_locked_usdt: Decimal
    reserved_slot_usdt: Decimal
    deleted_at: datetime | None


@dataclass(frozen=True)
class DeletePreview:
    active_listings_count: int
    open_assignments_count: int
    assignment_linked_reserved_usdt: Decimal
    unassigned_collateral_usdt: Decimal


@dataclass(frozen=True)
class DeleteExecutionResult:
    changed: bool
    assignment_transfers_count: int
    assignment_transferred_usdt: Decimal
    unassigned_collateral_returned_usdt: Decimal


@dataclass(frozen=True)
class TokenInvalidationResult:
    changed: bool
    paused_listings_count: int


@dataclass(frozen=True)
class BuyerBootstrapResult:
    user_id: int
    created_user: bool
    buyer_available_account_id: int
    buyer_withdraw_pending_account_id: int


@dataclass(frozen=True)
class BuyerShopResult:
    shop_id: int
    slug: str
    title: str


@dataclass(frozen=True)
class BuyerListingResult:
    listing_id: int
    shop_id: int
    wb_product_id: int
    discount_percent: int
    reward_usdt: Decimal
    slot_count: int
    available_slots: int


@dataclass(frozen=True)
class BuyerOrderSubmitResult:
    assignment_id: int
    changed: bool
    status: str
    order_id: str
    wb_product_id: int
    ordered_at: datetime


@dataclass(frozen=True)
class BuyerAssignmentView:
    assignment_id: int
    listing_id: int
    shop_slug: str
    wb_product_id: int
    status: str
    reward_usdt: Decimal
    reservation_expires_at: datetime
    order_id: str | None
    ordered_at: datetime | None


@dataclass(frozen=True)
class BuyerBalanceSnapshot:
    buyer_available_usdt: Decimal
    buyer_withdraw_pending_usdt: Decimal


@dataclass(frozen=True)
class BuyerWithdrawalHistoryItem:
    withdrawal_request_id: int
    amount_usdt: Decimal
    status: str
    payout_address: str
    requested_at: datetime
    processed_at: datetime | None
    sent_at: datetime | None
    note: str | None
    tx_hash: str | None


@dataclass(frozen=True)
class PendingWithdrawalView:
    withdrawal_request_id: int
    buyer_user_id: int
    buyer_telegram_id: int
    buyer_username: str | None
    amount_usdt: Decimal
    payout_address: str
    requested_at: datetime


@dataclass(frozen=True)
class WithdrawalRequestDetail:
    withdrawal_request_id: int
    buyer_user_id: int
    buyer_telegram_id: int
    buyer_username: str | None
    from_account_id: int
    to_account_id: int
    amount_usdt: Decimal
    status: str
    payout_address: str
    requested_at: datetime
    processed_at: datetime | None
    sent_at: datetime | None
    note: str | None
    tx_hash: str | None


@dataclass(frozen=True)
class ReservationExpiryResult:
    processed_count: int
    expired_count: int

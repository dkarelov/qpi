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

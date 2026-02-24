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

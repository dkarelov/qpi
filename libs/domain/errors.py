from __future__ import annotations


class DomainError(Exception):
    """Base domain error."""


class NotFoundError(DomainError):
    """Raised when an expected domain object is missing."""


class InvalidStateError(DomainError):
    """Raised when a transition is not valid from current state."""


class InsufficientFundsError(DomainError):
    """Raised when source account cannot cover transfer amount."""


class NoSlotsAvailableError(DomainError):
    """Raised when listing has no free slots for reservation."""


class PayloadValidationError(DomainError):
    """Raised when buyer payload cannot be decoded or violates contract."""


class DuplicateOrderError(DomainError):
    """Raised when order_id is already linked to another assignment."""


class ListingValidationError(DomainError):
    """Raised when seller-provided listing data fails business validation."""

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

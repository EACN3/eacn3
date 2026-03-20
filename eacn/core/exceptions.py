"""Unified exception definitions for EACN."""


class EACNError(Exception):
    """Base exception for EACN."""


class TaskError(EACNError):
    """Task-related errors."""


class BidError(EACNError):
    """Bid-related errors."""


class RegistrationError(EACNError):
    """Agent/server registration errors."""


class BudgetError(EACNError):
    """Budget and economy errors."""


class DiscoveryError(EACNError):
    """Agent discovery errors."""


class ProtocolError(EACNError):
    """Protocol communication errors."""

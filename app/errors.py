from __future__ import annotations


class AppError(Exception):
    """Base application error."""


class ConfigurationError(AppError):
    """Raised when required configuration is missing or invalid."""


class AuthenticationError(AppError):
    """Raised when a request or socket frame fails authentication."""


class DuplicateEventError(AppError):
    """Raised when an inbound event has already been processed."""


class DeliveryError(AppError):
    """Raised when outbound delivery cannot be completed."""

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class RetryableDeliveryError(DeliveryError):
    """Raised when outbound delivery may succeed after retry."""


class FatalDeliveryError(DeliveryError):
    """Raised when outbound delivery should not be retried."""


class ExternalAPIError(AppError):
    """Raised when an upstream API call fails."""

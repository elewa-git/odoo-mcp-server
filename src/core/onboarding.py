"""Safe contracts for Odoo credential onboarding outside the MCP conversation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

from src.core.identity import Principal


class OnboardingStatus(StrEnum):
    """Non-secret lifecycle states an MCP caller may safely observe."""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"


class OnboardingFailureCode(StrEnum):
    """Bounded failures that cannot repeat a provider or Odoo error message."""

    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    VALIDATION_FAILED = "validation_failed"


@dataclass(frozen=True, slots=True)
class OnboardingContinuation:
    """A safe pointer to a browser or CLI flow that owns credential collection.

    The target must be a clean endpoint: query strings, fragments, and embedded
    user info often become accidental carriers for one-time tokens or secrets.
    The provider instead authenticates the user directly in the out-of-band flow.
    """

    onboarding_id: str
    url: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.onboarding_id.strip():
            raise ValueError("Onboarding ID must be non-empty.")
        if self.expires_at.tzinfo is None:
            raise ValueError("Onboarding continuation expiry must be timezone-aware.")

        parsed = urlsplit(self.url)
        is_loopback_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if parsed.scheme != "https" and not is_loopback_http:
            raise ValueError("Onboarding URL must use HTTPS or local loopback HTTP.")
        if not parsed.netloc:
            raise ValueError("Onboarding URL must include a host.")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Onboarding URL cannot include user info.")
        if parsed.query or parsed.fragment:
            raise ValueError("Onboarding URL cannot include a query string or fragment.")

    def to_public_dict(self) -> dict[str, str]:
        """Return the complete MCP-safe representation without provider state."""
        return {
            "onboarding_id": self.onboarding_id,
            "url": self.url,
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class OnboardingResult:
    """Non-secret completion state for a previously issued continuation."""

    onboarding_id: str
    status: OnboardingStatus
    profile_id: str | None = None
    failure_code: OnboardingFailureCode | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.onboarding_id.strip():
            raise ValueError("Onboarding ID must be non-empty.")
        if self.completed_at is not None and self.completed_at.tzinfo is None:
            raise ValueError("Onboarding completion time must be timezone-aware.")

        if self.status is OnboardingStatus.READY:
            if not self.profile_id or self.failure_code is not None:
                raise ValueError("Ready onboarding results require only a profile ID.")
        elif self.status in {OnboardingStatus.FAILED, OnboardingStatus.EXPIRED}:
            if self.profile_id is not None or self.failure_code is None:
                raise ValueError("Failed onboarding results require only a failure code.")
        elif self.profile_id is not None or self.failure_code is not None or self.completed_at is not None:
            raise ValueError("Pending onboarding results cannot include completion data.")

    def to_public_dict(self) -> dict[str, str | None]:
        """Return only bounded lifecycle data suitable for MCP output and logs."""
        return {
            "onboarding_id": self.onboarding_id,
            "status": self.status.value,
            "profile_id": self.profile_id,
            "failure_code": self.failure_code.value if self.failure_code is not None else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at is not None else None,
        }


class OnboardingProvider(Protocol):
    """Start and observe a provider-owned credential flow for one principal."""

    async def begin(self, principal: Principal) -> OnboardingContinuation:
        """Issue a short-lived continuation without handling credential values."""

    async def get_result(self, principal: Principal, onboarding_id: str) -> OnboardingResult:
        """Return the principal-bound non-secret state or fail without fallback."""


class OnboardingUnavailableError(RuntimeError):
    """Raised when a composition has no safe out-of-band onboarding authority."""


class UnavailableOnboardingProvider:
    """Fail closed until a local or hosted custody-backed adapter is configured."""

    async def begin(self, principal: Principal) -> OnboardingContinuation:
        raise OnboardingUnavailableError("Out-of-band onboarding is not configured.")

    async def get_result(self, principal: Principal, onboarding_id: str) -> OnboardingResult:
        raise OnboardingUnavailableError("Out-of-band onboarding is not configured.")

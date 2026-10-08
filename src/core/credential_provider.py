"""Secret-custody boundary for profile-bound Odoo credentials."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from src.core.identity import Principal
from src.core.profiles import OdooProfile


@dataclass(frozen=True, slots=True)
class CredentialLease:
    """Short-lived secret material that must never be serialized or logged."""

    profile_id: str
    credential_version: int
    api_key: str = field(repr=False)
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("Credential lease profile ID must be non-empty.")
        if self.credential_version < 1:
            raise ValueError("Credential lease version must be positive.")
        if not self.api_key:
            raise ValueError("Credential lease API key must be non-empty.")

    def __repr__(self) -> str:
        return (
            "CredentialLease("
            f"profile_id={self.profile_id!r}, "
            f"credential_version={self.credential_version!r}, "
            "api_key='***', "
            f"expires_at={self.expires_at!r})"
        )


class CredentialProvider(Protocol):
    """Resolve credentials only after exact principal and profile selection."""

    async def resolve(
        self,
        principal: Principal,
        profile: OdooProfile,
    ) -> CredentialLease:
        """Return a bounded lease or fail without fallback."""


class CredentialUnavailableError(RuntimeError):
    """Raised when custody cannot resolve the requested credential version."""


class UnavailableCredentialProvider:
    """Fail-closed default for compositions without credential custody."""

    async def resolve(
        self,
        principal: Principal,
        profile: OdooProfile,
    ) -> CredentialLease:
        raise CredentialUnavailableError(
            f"Credential custody is unavailable for profile {profile.id!r}."
        )

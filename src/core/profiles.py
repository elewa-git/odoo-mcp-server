"""Per-principal Odoo connection profiles and repository boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from src.core.identity import Principal


class OdooTransport(StrEnum):
    """Supported Odoo API generations."""

    XMLRPC = "xmlrpc"
    JSON2 = "json2"


class ProfileState(StrEnum):
    """Non-secret lifecycle surfaced by onboarding and diagnostics."""

    PENDING = "pending"
    READY = "ready"
    REVOKED = "revoked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class OdooProfile:
    """Metadata for one exact Odoo identity; credentials live behind a provider."""

    id: str
    principal_id: str
    label: str
    canonical_url: str
    database: str
    username: str
    company_id: int | None
    odoo_major: int
    transport: OdooTransport
    credential_version: int
    state: ProfileState

    def __post_init__(self) -> None:
        required = {
            "id": self.id,
            "principal_id": self.principal_id,
            "label": self.label,
            "canonical_url": self.canonical_url,
            "database": self.database,
            "username": self.username,
        }
        for name, value in required.items():
            if not value.strip():
                raise ValueError(f"Profile {name} must be non-empty.")
        if self.odoo_major < 1:
            raise ValueError("Odoo major version must be positive.")
        if self.credential_version < 1:
            raise ValueError("Credential version must be positive.")

        expected = OdooTransport.XMLRPC if self.odoo_major <= 18 else OdooTransport.JSON2
        if self.transport is not expected:
            raise ValueError(
                f"Odoo {self.odoo_major} requires {expected.value}; "
                f"received {self.transport.value}."
            )

        object.__setattr__(self, "canonical_url", self.canonical_url.rstrip("/"))


class ProfileNotFoundError(LookupError):
    """Raised instead of falling back to another principal or profile."""


class ProfileRepository(Protocol):
    """Persist profile metadata with exact principal ownership."""

    async def put(self, profile: OdooProfile) -> None:
        """Create or replace one profile for its declared owner."""

    async def get(self, principal: Principal, profile_id: str) -> OdooProfile:
        """Resolve an exact owned profile or fail closed."""

    async def list(self, principal: Principal) -> tuple[OdooProfile, ...]:
        """List only profiles owned by the verified principal."""

    async def set_default(self, principal: Principal, profile_id: str) -> None:
        """Persist the verified principal's default profile."""

    async def get_default(self, principal: Principal) -> OdooProfile:
        """Resolve the durable default without cross-principal fallback."""


class InMemoryProfileRepository:
    """Deterministic adapter for tests and local composition prototyping."""

    def __init__(self) -> None:
        self._profiles: dict[tuple[str, str], OdooProfile] = {}
        self._defaults: dict[str, str] = {}

    async def put(self, profile: OdooProfile) -> None:
        self._profiles[(profile.principal_id, profile.id)] = profile

    async def get(self, principal: Principal, profile_id: str) -> OdooProfile:
        profile = self._profiles.get((principal.id, profile_id))
        if profile is None:
            raise ProfileNotFoundError(
                f"Profile {profile_id!r} is not available to principal {principal.id!r}."
            )
        return profile

    async def list(self, principal: Principal) -> tuple[OdooProfile, ...]:
        owned = [
            profile
            for (principal_id, _), profile in self._profiles.items()
            if principal_id == principal.id
        ]
        return tuple(sorted(owned, key=lambda profile: (profile.label.casefold(), profile.id)))

    async def set_default(self, principal: Principal, profile_id: str) -> None:
        await self.get(principal, profile_id)
        self._defaults[principal.id] = profile_id

    async def get_default(self, principal: Principal) -> OdooProfile:
        profile_id = self._defaults.get(principal.id)
        if profile_id is None:
            raise ProfileNotFoundError(
                f"Principal {principal.id!r} has no default Odoo profile."
            )
        return await self.get(principal, profile_id)

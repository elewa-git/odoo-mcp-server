"""Principal-bound, exact-payload approval contracts for Odoo writes."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol


class ApprovalStatus(StrEnum):
    """Lifecycle states for an exact approval token."""

    PENDING = "pending"
    RESERVED = "reserved"
    EXPIRED = "expired"


class ApprovalRepositoryError(RuntimeError):
    """Raised when durable local approval state cannot be safely used."""


class ApprovalNotFoundError(ApprovalRepositoryError):
    """Raised for an unknown approval token without exposing store contents."""


class ApprovalRejectedError(ApprovalRepositoryError):
    """Raised when an approval cannot authorize this exact execution attempt."""


def canonicalize_payload(payload: Mapping[str, Any]) -> str:
    """Encode an exact JSON payload deterministically before hashing or storage."""
    try:
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Approval payload must contain only finite JSON values.") from exc


def payload_hash(canonical_payload: str) -> str:
    """Return the stable digest used for indexed equality checks."""
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """One short-lived authorization bound to a caller and immutable payload."""

    token: str = field(repr=False)
    principal_id: str
    profile_id: str
    credential_version: int
    canonical_payload: str = field(repr=False)
    payload_hash: str
    expires_at: datetime
    status: ApprovalStatus
    reserved_at: datetime | None = None

    def __post_init__(self) -> None:
        required = {
            "token": self.token,
            "principal_id": self.principal_id,
            "profile_id": self.profile_id,
            "canonical_payload": self.canonical_payload,
            "payload_hash": self.payload_hash,
        }
        for name, value in required.items():
            if not value.strip():
                raise ValueError(f"Approval {name} must be non-empty.")
        if self.credential_version < 1:
            raise ValueError("Approval credential version must be positive.")
        if self.expires_at.tzinfo is None:
            raise ValueError("Approval expiry must be timezone-aware.")
        if self.reserved_at is not None and self.reserved_at.tzinfo is None:
            raise ValueError("Approval reservation time must be timezone-aware.")
        if payload_hash(self.canonical_payload) != self.payload_hash:
            raise ValueError("Approval payload hash does not match its canonical payload.")
        if self.status is ApprovalStatus.RESERVED and self.reserved_at is None:
            raise ValueError("Reserved approvals require a reservation time.")
        if self.status is not ApprovalStatus.RESERVED and self.reserved_at is not None:
            raise ValueError("Only reserved approvals can have a reservation time.")

    @classmethod
    def issue(
        cls,
        *,
        principal_id: str,
        profile_id: str,
        credential_version: int,
        payload: Mapping[str, Any],
        expires_at: datetime,
        token: str | None = None,
    ) -> ApprovalRecord:
        """Construct a pending record while keeping raw payload input transient."""
        canonical_payload = canonicalize_payload(payload)
        return cls(
            token=token or secrets.token_urlsafe(32),
            principal_id=principal_id,
            profile_id=profile_id,
            credential_version=credential_version,
            canonical_payload=canonical_payload,
            payload_hash=payload_hash(canonical_payload),
            expires_at=expires_at,
            status=ApprovalStatus.PENDING,
        )


class ApprovalRepository(Protocol):
    """Persist and atomically reserve exact write approvals."""

    async def issue(
        self,
        *,
        principal_id: str,
        profile_id: str,
        credential_version: int,
        payload: Mapping[str, Any],
        expires_at: datetime,
    ) -> ApprovalRecord:
        """Store a pending approval with a fresh opaque token."""

    async def reserve(
        self,
        token: str,
        *,
        principal_id: str,
        profile_id: str,
        credential_version: int,
        payload: Mapping[str, Any],
    ) -> ApprovalRecord:
        """Atomically reserve an unexpired approval for its exact binding."""


class UnavailableApprovalRepository:
    """Fail closed when a composition has no durable approval authority."""

    async def issue(
        self,
        *,
        principal_id: str,
        profile_id: str,
        credential_version: int,
        payload: Mapping[str, Any],
        expires_at: datetime,
    ) -> ApprovalRecord:
        raise ApprovalRepositoryError("No approval repository is configured.")

    async def reserve(
        self,
        token: str,
        *,
        principal_id: str,
        profile_id: str,
        credential_version: int,
        payload: Mapping[str, Any],
    ) -> ApprovalRecord:
        raise ApprovalRepositoryError("No approval repository is configured.")


def utc_now() -> datetime:
    """Keep the expiry clock injectable at the adapter boundary when needed."""
    return datetime.now(UTC)

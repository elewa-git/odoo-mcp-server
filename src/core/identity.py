"""Trusted caller identity used by local and hosted server compositions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class PrincipalKind(StrEnum):
    """Deployment boundary that authenticated a caller."""

    LOCAL = "local"
    REMOTE = "remote"


@dataclass(frozen=True, slots=True)
class Principal:
    """One verified caller; tool arguments must never construct this value."""

    subject: str
    issuer: str | None
    kind: PrincipalKind

    def __post_init__(self) -> None:
        subject = self.subject.strip()
        issuer = self.issuer.strip() if self.issuer is not None else None
        if not subject:
            raise ValueError("Principal subject must be non-empty.")
        if self.kind is PrincipalKind.REMOTE and not issuer:
            raise ValueError("Remote principals require a trusted issuer.")
        if self.kind is PrincipalKind.LOCAL and issuer is not None:
            raise ValueError("Local principals cannot claim a remote issuer.")
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "issuer", issuer)

    @property
    def id(self) -> str:
        """Return a stable coordinate that cannot collide across trusted issuers."""
        issuer = self.issuer or "local-installation"
        return f"{self.kind.value}:{issuer}:{self.subject}"


class PrincipalProvider(Protocol):
    """Resolve identity exclusively from a trusted serving adapter."""

    async def resolve(self) -> Principal:
        """Return the authenticated caller or fail before profile lookup."""


class PrincipalUnavailableError(RuntimeError):
    """Raised when a serving adapter cannot authenticate the caller."""


class UnavailablePrincipalProvider:
    """Fail-closed default for compositions without a configured identity source."""

    async def resolve(self) -> Principal:
        raise PrincipalUnavailableError("No trusted principal provider is configured.")


class LocalInstallationPrincipalProvider:
    """Bind stdio calls to the operating-system account running the process."""

    def __init__(self, subject: str) -> None:
        # The composition root supplies this from local process context. Tool
        # arguments and MCP headers are deliberately not accepted here.
        self._principal = Principal(
            subject=subject,
            issuer=None,
            kind=PrincipalKind.LOCAL,
        )

    async def resolve(self) -> Principal:
        return self._principal

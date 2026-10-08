"""Profile-bound Odoo client construction with credential-version fences."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from src.core.credential_provider import CredentialProvider
from src.core.credentials import OdooCredentials
from src.core.identity import Principal
from src.core.profiles import OdooProfile, ProfileState
from src.mcp.odoo.connection.client import OdooClient


class ProfileClientUnavailableError(RuntimeError):
    """Raised when an exact profile cannot safely produce an authenticated client."""


class OdooClientFactory(Protocol):
    """Create clients only from verified identity and profile state."""

    async def connect(self, principal: Principal, profile: OdooProfile) -> OdooClient:
        """Return one authenticated, profile-bound client or fail closed."""


class ProfileOdooClientFactory:
    """Resolve custody and pin one Odoo transport for the client's lifetime."""

    def __init__(
        self,
        credential_provider: CredentialProvider,
        client_builder: Callable[..., OdooClient] = OdooClient,
    ) -> None:
        self._credential_provider = credential_provider
        self._client_builder = client_builder

    async def connect(self, principal: Principal, profile: OdooProfile) -> OdooClient:
        if profile.principal_id != principal.id:
            raise ProfileClientUnavailableError(
                "The selected Odoo profile is not owned by the authenticated principal."
            )
        if profile.state is not ProfileState.READY:
            raise ProfileClientUnavailableError(
                f"Odoo profile {profile.id!r} is not ready for use."
            )

        lease = await self._credential_provider.resolve(principal, profile)
        if lease.profile_id != profile.id:
            raise ProfileClientUnavailableError(
                "Credential custody returned a lease for a different profile."
            )
        if lease.credential_version != profile.credential_version:
            raise ProfileClientUnavailableError(
                "Credential custody returned a stale credential version."
            )
        if lease.expires_at is not None and lease.expires_at <= datetime.now(UTC):
            raise ProfileClientUnavailableError("Credential custody returned an expired lease.")

        credentials = OdooCredentials(
            url=profile.canonical_url,
            db=profile.database,
            username=profile.username,
            api_key=lease.api_key,
        )
        client = self._client_builder(
            credentials=credentials,
            transport=profile.transport.value,
        )
        try:
            await client.authenticate()
        except Exception:
            # A partially initialized HTTP client or XML-RPC proxy must not leak
            # when authentication rejects a rotated or revoked credential.
            await client.close()
            raise
        return client


class UnavailableOdooClientFactory:
    """Fail-closed default for compositions without profile-bound connectivity."""

    async def connect(self, principal: Principal, profile: OdooProfile) -> OdooClient:
        raise ProfileClientUnavailableError(
            "No profile-bound Odoo client factory is configured."
        )

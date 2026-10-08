from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.core.client_factory import ProfileClientUnavailableError, ProfileOdooClientFactory
from src.core.credential_provider import CredentialLease
from src.core.identity import Principal, PrincipalKind
from src.core.profiles import OdooProfile, OdooTransport, ProfileState


class StaticCredentialProvider:
    def __init__(self, lease: CredentialLease) -> None:
        self.lease = lease

    async def resolve(self, principal: Principal, profile: OdooProfile) -> CredentialLease:
        return self.lease


class FakeOdooClient:
    instances: list[FakeOdooClient] = []

    def __init__(self, *, credentials, transport: str) -> None:
        self.credentials = credentials
        self.transport = transport
        self.authenticated = False
        self.closed = False
        self.instances.append(self)

    async def authenticate(self) -> int:
        self.authenticated = True
        return 7

    async def close(self) -> None:
        self.closed = True


class RejectingOdooClient(FakeOdooClient):
    async def authenticate(self) -> int:
        raise ConnectionError("authentication rejected")


def _principal(subject: str = "user-1") -> Principal:
    return Principal(
        subject=subject,
        issuer="https://issuer.example",
        kind=PrincipalKind.REMOTE,
    )


def _profile(principal: Principal, *, state: ProfileState = ProfileState.READY) -> OdooProfile:
    return OdooProfile(
        id="profile-1",
        principal_id=principal.id,
        label="Production",
        canonical_url="https://odoo.example/",
        database="prod",
        username="user@example.com",
        company_id=None,
        odoo_major=19,
        transport=OdooTransport.JSON2,
        credential_version=2,
        state=state,
    )


def _lease(**overrides) -> CredentialLease:
    values = {
        "profile_id": "profile-1",
        "credential_version": 2,
        "api_key": "secret",
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }
    values.update(overrides)
    return CredentialLease(**values)


@pytest.mark.asyncio
async def test_factory_pins_profile_transport_and_credential() -> None:
    FakeOdooClient.instances.clear()
    principal = _principal()
    profile = _profile(principal)
    factory = ProfileOdooClientFactory(
        StaticCredentialProvider(_lease()),
        FakeOdooClient,
    )

    client = await factory.connect(principal, profile)

    assert client.authenticated is True
    assert client.transport == "json2"
    assert client.credentials.url == "https://odoo.example"
    assert client.credentials.api_key == "secret"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lease",
    [
        _lease(profile_id="other-profile"),
        _lease(credential_version=1),
        _lease(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
    ],
)
async def test_factory_rejects_mismatched_or_expired_credential_lease(
    lease: CredentialLease,
) -> None:
    principal = _principal()
    factory = ProfileOdooClientFactory(StaticCredentialProvider(lease), FakeOdooClient)

    with pytest.raises(ProfileClientUnavailableError):
        await factory.connect(principal, _profile(principal))


@pytest.mark.asyncio
async def test_factory_rejects_cross_principal_profile() -> None:
    owner = _principal("owner")
    outsider = _principal("outsider")
    factory = ProfileOdooClientFactory(
        StaticCredentialProvider(_lease()),
        FakeOdooClient,
    )

    with pytest.raises(ProfileClientUnavailableError, match="not owned"):
        await factory.connect(outsider, _profile(owner))


@pytest.mark.asyncio
async def test_factory_closes_client_when_authentication_fails() -> None:
    FakeOdooClient.instances.clear()
    principal = _principal()
    factory = ProfileOdooClientFactory(
        StaticCredentialProvider(_lease()),
        RejectingOdooClient,
    )

    with pytest.raises(ConnectionError, match="authentication rejected"):
        await factory.connect(principal, _profile(principal))

    assert FakeOdooClient.instances[-1].closed is True

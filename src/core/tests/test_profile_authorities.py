from __future__ import annotations

import pytest

from src.core.credential_provider import (
    CredentialLease,
    CredentialUnavailableError,
    UnavailableCredentialProvider,
)
from src.core.identity import LocalInstallationPrincipalProvider, Principal, PrincipalKind
from src.core.profiles import (
    InMemoryProfileRepository,
    OdooProfile,
    OdooTransport,
    ProfileNotFoundError,
    ProfileState,
)


def _principal(subject: str, issuer: str) -> Principal:
    return Principal(subject=subject, issuer=issuer, kind=PrincipalKind.REMOTE)


def _profile(
    principal: Principal,
    profile_id: str,
    *,
    url: str,
    major: int = 19,
    transport: OdooTransport = OdooTransport.JSON2,
) -> OdooProfile:
    return OdooProfile(
        id=profile_id,
        principal_id=principal.id,
        label="Production",
        canonical_url=url,
        database="prod",
        username="user@example.com",
        company_id=None,
        odoo_major=major,
        transport=transport,
        credential_version=1,
        state=ProfileState.READY,
    )


def test_remote_principal_identity_includes_the_trusted_issuer() -> None:
    first = _principal("user-1", "https://issuer-a.example")
    second = _principal("user-1", "https://issuer-b.example")

    assert first.id != second.id


@pytest.mark.asyncio
async def test_local_provider_resolves_only_its_process_bound_principal() -> None:
    provider = LocalInstallationPrincipalProvider("local-user")

    principal = await provider.resolve()

    assert principal == Principal(
        subject="local-user",
        issuer=None,
        kind=PrincipalKind.LOCAL,
    )


@pytest.mark.parametrize(
    ("major", "transport"),
    [
        (18, OdooTransport.JSON2),
        (19, OdooTransport.XMLRPC),
    ],
)
def test_profile_rejects_transport_for_the_wrong_odoo_generation(
    major: int,
    transport: OdooTransport,
) -> None:
    principal = _principal("user-1", "https://issuer.example")

    with pytest.raises(ValueError, match="requires"):
        _profile(
            principal,
            "profile-1",
            url="https://odoo.example",
            major=major,
            transport=transport,
        )


@pytest.mark.asyncio
async def test_same_database_name_is_isolated_by_principal_and_profile() -> None:
    repository = InMemoryProfileRepository()
    first = _principal("user-1", "https://issuer.example")
    second = _principal("user-2", "https://issuer.example")
    first_profile = _profile(first, "profile-1", url="https://one.example")
    second_profile = _profile(second, "profile-1", url="https://two.example")

    await repository.put(first_profile)
    await repository.put(second_profile)
    await repository.set_default(first, first_profile.id)
    await repository.set_default(second, second_profile.id)

    assert (await repository.get_default(first)).canonical_url == "https://one.example"
    assert (await repository.get_default(second)).canonical_url == "https://two.example"


@pytest.mark.asyncio
async def test_cross_principal_profile_lookup_fails_without_fallback() -> None:
    repository = InMemoryProfileRepository()
    owner = _principal("owner", "https://issuer.example")
    outsider = _principal("outsider", "https://issuer.example")
    profile = _profile(owner, "profile-1", url="https://odoo.example")
    await repository.put(profile)

    with pytest.raises(ProfileNotFoundError):
        await repository.get(outsider, profile.id)


def test_credential_lease_repr_never_contains_the_api_key() -> None:
    lease = CredentialLease(
        profile_id="profile-1",
        credential_version=1,
        api_key="super-secret-key",
    )

    assert "super-secret-key" not in repr(lease)
    assert "***" in repr(lease)


@pytest.mark.asyncio
async def test_unavailable_credential_provider_fails_closed() -> None:
    principal = _principal("user-1", "https://issuer.example")
    profile = _profile(principal, "profile-1", url="https://odoo.example")

    with pytest.raises(CredentialUnavailableError):
        await UnavailableCredentialProvider().resolve(principal, profile)

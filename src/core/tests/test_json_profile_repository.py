from __future__ import annotations

import json
import stat

import pytest

from src.core.identity import Principal, PrincipalKind
from src.core.json_profile_repository import JsonProfileRepository, ProfileRepositoryError
from src.core.profiles import OdooProfile, OdooTransport, ProfileNotFoundError, ProfileState


def _principal(subject: str) -> Principal:
    return Principal(
        subject=subject,
        issuer="https://issuer.example",
        kind=PrincipalKind.REMOTE,
    )


def _profile(principal: Principal, url: str) -> OdooProfile:
    return OdooProfile(
        id="profile-1",
        principal_id=principal.id,
        label="Production",
        canonical_url=url,
        database="prod",
        username="user@example.com",
        company_id=1,
        odoo_major=19,
        transport=OdooTransport.JSON2,
        credential_version=1,
        state=ProfileState.READY,
    )


@pytest.mark.asyncio
async def test_repository_persists_owner_only_metadata_and_defaults(tmp_path) -> None:
    path = tmp_path / "profiles.json"
    principal = _principal("user-1")
    profile = _profile(principal, "https://odoo.example/")
    repository = JsonProfileRepository(path)

    await repository.put(profile)
    await repository.set_default(principal, profile.id)
    reloaded = JsonProfileRepository(path)

    assert (await reloaded.get_default(principal)).canonical_url == "https://odoo.example"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_same_profile_id_is_isolated_between_principals(tmp_path) -> None:
    repository = JsonProfileRepository(tmp_path / "profiles.json")
    first = _principal("user-1")
    second = _principal("user-2")
    await repository.put(_profile(first, "https://one.example"))
    await repository.put(_profile(second, "https://two.example"))

    assert (await repository.get(first, "profile-1")).canonical_url == "https://one.example"
    assert (await repository.get(second, "profile-1")).canonical_url == "https://two.example"


@pytest.mark.asyncio
async def test_repository_rejects_cross_principal_default(tmp_path) -> None:
    repository = JsonProfileRepository(tmp_path / "profiles.json")
    owner = _principal("owner")
    outsider = _principal("outsider")
    await repository.put(_profile(owner, "https://odoo.example"))

    with pytest.raises(ProfileNotFoundError):
        await repository.set_default(outsider, "profile-1")


@pytest.mark.asyncio
async def test_repository_rejects_secret_fields_and_unsafe_modes(tmp_path) -> None:
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profiles": [],
                "defaults": {},
                "api_key": "must-not-be-here",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(ProfileRepositoryError, match="secret fields"):
        await JsonProfileRepository(path).list(_principal("user-1"))

    path.write_text(
        json.dumps({"schema_version": 1, "profiles": [], "defaults": {}}),
        encoding="utf-8",
    )
    path.chmod(0o644)

    with pytest.raises(ProfileRepositoryError, match="owner-only"):
        await JsonProfileRepository(path).list(_principal("user-1"))


@pytest.mark.asyncio
async def test_repository_rejects_symlink_metadata_path(tmp_path) -> None:
    target = tmp_path / "target.json"
    target.write_text(
        json.dumps({"schema_version": 1, "profiles": [], "defaults": {}}),
        encoding="utf-8",
    )
    target.chmod(0o600)
    link = tmp_path / "profiles.json"
    link.symlink_to(target)

    with pytest.raises(ProfileRepositoryError, match="symbolic link"):
        await JsonProfileRepository(link).list(_principal("user-1"))

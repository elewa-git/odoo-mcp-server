from __future__ import annotations

import asyncio
import stat
from datetime import UTC, datetime, timedelta

import pytest

from src.core import sqlite_approval_repository
from src.core.approvals import ApprovalNotFoundError, ApprovalRejectedError, ApprovalStatus
from src.core.sqlite_approval_repository import SqliteApprovalRepository


def _expiry() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=5)


def _payload() -> dict[str, object]:
    return {"method": "write", "model": "crm.lead", "values": {"name": "Updated"}}


async def _issue(repository: SqliteApprovalRepository):
    return await repository.issue(
        principal_id="remote:https://issuer.example:user-1",
        profile_id="profile-1",
        credential_version=3,
        payload=_payload(),
        expires_at=_expiry(),
    )


@pytest.mark.asyncio
async def test_issue_and_reserve_persists_an_owner_only_exact_approval(tmp_path) -> None:
    path = tmp_path / "approvals.sqlite3"
    repository = SqliteApprovalRepository(path)
    issued = await _issue(repository)

    reserved = await repository.reserve(
        issued.token,
        principal_id="remote:https://issuer.example:user-1",
        profile_id="profile-1",
        credential_version=3,
        payload={"values": {"name": "Updated"}, "model": "crm.lead", "method": "write"},
    )

    assert reserved.status is ApprovalStatus.RESERVED
    assert reserved.reserved_at is not None
    assert reserved.payload_hash == issued.payload_hash
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_reservation_is_single_use_even_across_repository_instances(tmp_path) -> None:
    path = tmp_path / "approvals.sqlite3"
    issuer = SqliteApprovalRepository(path)
    record = await _issue(issuer)
    first = SqliteApprovalRepository(path)
    second = SqliteApprovalRepository(path)

    async def reserve(repository: SqliteApprovalRepository):
        return await repository.reserve(
            record.token,
            principal_id="remote:https://issuer.example:user-1",
            profile_id="profile-1",
            credential_version=3,
            payload=_payload(),
        )

    results = await asyncio.gather(reserve(first), reserve(second), return_exceptions=True)

    assert sum(result.status is ApprovalStatus.RESERVED for result in results if not isinstance(result, Exception)) == 1
    assert sum(isinstance(result, ApprovalRejectedError) for result in results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("principal_id", "profile_id", "credential_version", "payload"),
    [
        ("remote:https://issuer.example:user-2", "profile-1", 3, _payload()),
        ("remote:https://issuer.example:user-1", "profile-2", 3, _payload()),
        ("remote:https://issuer.example:user-1", "profile-1", 4, _payload()),
        (
            "remote:https://issuer.example:user-1",
            "profile-1",
            3,
            {"method": "write", "model": "crm.lead", "values": {"name": "Changed"}},
        ),
    ],
)
async def test_reservation_rejects_every_binding_mismatch(
    tmp_path,
    principal_id: str,
    profile_id: str,
    credential_version: int,
    payload: dict[str, object],
) -> None:
    repository = SqliteApprovalRepository(tmp_path / "approvals.sqlite3")
    record = await _issue(repository)

    with pytest.raises(ApprovalRejectedError, match="does not match"):
        await repository.reserve(
            record.token,
            principal_id=principal_id,
            profile_id=profile_id,
            credential_version=credential_version,
            payload=payload,
        )


@pytest.mark.asyncio
async def test_expired_and_unknown_approvals_fail_closed(tmp_path, monkeypatch) -> None:
    repository = SqliteApprovalRepository(tmp_path / "approvals.sqlite3")
    expired = await repository.issue(
        principal_id="remote:https://issuer.example:user-1",
        profile_id="profile-1",
        credential_version=3,
        payload=_payload(),
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    monkeypatch.setattr(
        sqlite_approval_repository,
        "utc_now",
        lambda: expired.expires_at + timedelta(microseconds=1),
    )

    with pytest.raises(ApprovalRejectedError, match="expired"):
        await repository.reserve(
            expired.token,
            principal_id="remote:https://issuer.example:user-1",
            profile_id="profile-1",
            credential_version=3,
            payload=_payload(),
        )
    with pytest.raises(ApprovalNotFoundError):
        await repository.reserve(
            "unknown-token",
            principal_id="remote:https://issuer.example:user-1",
            profile_id="profile-1",
            credential_version=3,
            payload=_payload(),
        )


@pytest.mark.asyncio
async def test_repository_rejects_symlink_and_permissive_database_paths(tmp_path) -> None:
    target = tmp_path / "target.sqlite3"
    target.touch(mode=0o600)
    path = tmp_path / "approvals.sqlite3"
    path.symlink_to(target)

    with pytest.raises(Exception, match="symbolic link"):
        await _issue(SqliteApprovalRepository(path))

    path.unlink()
    path.touch(mode=0o600)
    path.chmod(0o644)

    with pytest.raises(Exception, match="owner-only"):
        await _issue(SqliteApprovalRepository(path))

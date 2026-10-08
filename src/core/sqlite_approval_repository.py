"""SQLite adapter for local, atomic, single-use write approvals."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.approvals import (
    ApprovalNotFoundError,
    ApprovalRecord,
    ApprovalRejectedError,
    ApprovalRepositoryError,
    ApprovalStatus,
    canonicalize_payload,
    payload_hash,
    utc_now,
)


class SqliteApprovalRepository:
    """Owner-only SQLite persistence with compare-and-set approval reservation."""

    def __init__(self, path: Path) -> None:
        self._path = path
        # This serializes one adapter's calls; SQLite BEGIN IMMEDIATE preserves
        # the same reservation invariant when separate processes share the DB.
        self._lock = asyncio.Lock()

    async def issue(
        self,
        *,
        principal_id: str,
        profile_id: str,
        credential_version: int,
        payload: Mapping[str, Any],
        expires_at: datetime,
    ) -> ApprovalRecord:
        record = ApprovalRecord.issue(
            principal_id=principal_id,
            profile_id=profile_id,
            credential_version=credential_version,
            payload=payload,
            expires_at=expires_at,
        )
        if record.expires_at <= utc_now():
            raise ValueError("Approval expiry must be in the future.")

        async with self._lock:
            return await asyncio.to_thread(self._issue_sync, record)

    async def reserve(
        self,
        token: str,
        *,
        principal_id: str,
        profile_id: str,
        credential_version: int,
        payload: Mapping[str, Any],
    ) -> ApprovalRecord:
        if not token.strip():
            raise ApprovalNotFoundError("Approval token was not found.")
        canonical_payload = canonicalize_payload(payload)

        async with self._lock:
            return await asyncio.to_thread(
                self._reserve_sync,
                token,
                principal_id,
                profile_id,
                credential_version,
                canonical_payload,
            )

    def _issue_sync(self, record: ApprovalRecord) -> ApprovalRecord:
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO approvals (
                        token, principal_id, profile_id, credential_version,
                        canonical_payload, payload_hash, expires_at, status, reserved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.token,
                        record.principal_id,
                        record.profile_id,
                        record.credential_version,
                        record.canonical_payload,
                        record.payload_hash,
                        _serialize_time(record.expires_at),
                        record.status.value,
                        None,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            # A generated token collision is extraordinarily unlikely, but an
            # existing row must never be overwritten or rebound silently.
            raise ApprovalRepositoryError("Failed to issue a unique approval token.") from exc
        finally:
            connection.close()
        return record

    def _reserve_sync(
        self,
        token: str,
        principal_id: str,
        profile_id: str,
        credential_version: int,
        canonical_payload: str,
    ) -> ApprovalRecord:
        connection = self._connect()
        try:
            # Acquiring the write lock before the read makes status validation
            # and the PENDING -> RESERVED transition one indivisible operation.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM approvals WHERE token = ?", (token,)).fetchone()
            if row is None:
                raise ApprovalNotFoundError("Approval token was not found.")

            record = _record_from_row(row)
            if record.status is not ApprovalStatus.PENDING:
                raise ApprovalRejectedError("Approval has already been reserved or expired.")
            if record.expires_at <= utc_now():
                connection.execute(
                    "UPDATE approvals SET status = ? WHERE token = ? AND status = ?",
                    (ApprovalStatus.EXPIRED.value, token, ApprovalStatus.PENDING.value),
                )
                raise ApprovalRejectedError("Approval has expired.")
            if (
                record.principal_id != principal_id
                or record.profile_id != profile_id
                or record.credential_version != credential_version
                or record.canonical_payload != canonical_payload
                or record.payload_hash != payload_hash(canonical_payload)
            ):
                # Keep the response deliberately generic: authorization checks
                # must not reveal another principal's profile or write payload.
                raise ApprovalRejectedError("Approval does not match this execution request.")

            reserved_at = utc_now()
            update = connection.execute(
                """
                UPDATE approvals
                SET status = ?, reserved_at = ?
                WHERE token = ? AND status = ?
                """,
                (
                    ApprovalStatus.RESERVED.value,
                    _serialize_time(reserved_at),
                    token,
                    ApprovalStatus.PENDING.value,
                ),
            )
            if update.rowcount != 1:
                # The conditional update is the final replay fence if another
                # process changed the row after this transaction acquired it.
                raise ApprovalRejectedError("Approval has already been reserved or expired.")
            connection.commit()
            return ApprovalRecord(
                token=record.token,
                principal_id=record.principal_id,
                profile_id=record.profile_id,
                credential_version=record.credential_version,
                canonical_payload=record.canonical_payload,
                payload_hash=record.payload_hash,
                expires_at=record.expires_at,
                status=ApprovalStatus.RESERVED,
                reserved_at=reserved_at,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        self._secure_path()
        try:
            connection = sqlite3.connect(self._path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            # Keep rollback journals beside the protected DB rather than using a
            # shared memory location that could weaken the local file boundary.
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    token TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    credential_version INTEGER NOT NULL,
                    canonical_payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reserved_at TEXT
                )
                """
            )
            return connection
        except sqlite3.Error as exc:
            raise ApprovalRepositoryError(f"Failed to open local approval storage at {self._path}.") from exc

    def _secure_path(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._path.parent.chmod(0o700)
        except OSError as exc:
            raise ApprovalRepositoryError(f"Failed to secure approval directory {self._path.parent}.") from exc

        if self._path.exists():
            if self._path.is_symlink():
                raise ApprovalRepositoryError("Approval database path cannot be a symbolic link.")
            mode = stat.S_IMODE(self._path.stat().st_mode)
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise ApprovalRepositoryError("Approval database file must be owner-only (mode 600).")
            return

        try:
            descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            # Another local process created the path first; its next use will
            # validate the resulting owner-only file before SQLite opens it.
            return
        except OSError as exc:
            raise ApprovalRepositoryError(f"Failed to create approval database {self._path}.") from exc
        else:
            os.close(descriptor)


def _serialize_time(value: datetime) -> str:
    return value.isoformat()


def _record_from_row(row: sqlite3.Row) -> ApprovalRecord:
    try:
        return ApprovalRecord(
            token=str(row["token"]),
            principal_id=str(row["principal_id"]),
            profile_id=str(row["profile_id"]),
            credential_version=int(row["credential_version"]),
            canonical_payload=str(row["canonical_payload"]),
            payload_hash=str(row["payload_hash"]),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
            status=ApprovalStatus(str(row["status"])),
            reserved_at=(
                datetime.fromisoformat(str(row["reserved_at"])) if row["reserved_at"] is not None else None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ApprovalRepositoryError("Approval database contains an invalid record.") from exc

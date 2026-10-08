"""Owner-only local persistence for non-secret Odoo profile metadata."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from src.core.identity import Principal
from src.core.profiles import (
    OdooProfile,
    OdooTransport,
    ProfileNotFoundError,
    ProfileState,
)

SCHEMA_VERSION = 1
FORBIDDEN_SECRET_KEYS = frozenset({"api_key", "password", "secret", "access_token", "refresh_token"})


class ProfileRepositoryError(RuntimeError):
    """Raised when local profile metadata is unsafe or malformed."""


class JsonProfileRepository:
    """Persist non-secret profile metadata without crossing principal ownership."""

    def __init__(self, path: Path) -> None:
        self._path = path
        # Serialize read-modify-write operations so concurrent local tool calls
        # cannot lose a profile or default selection.
        self._lock = asyncio.Lock()

    async def put(self, profile: OdooProfile) -> None:
        async with self._lock:
            data = self._load()
            profiles = [
                item
                for item in data["profiles"]
                if not (item.get("principal_id") == profile.principal_id and item.get("id") == profile.id)
            ]
            profiles.append(_serialize_profile(profile))
            data["profiles"] = profiles
            self._persist(data)

    async def get(self, principal: Principal, profile_id: str) -> OdooProfile:
        async with self._lock:
            return self._get_from_data(self._load(), principal, profile_id)

    async def list(self, principal: Principal) -> tuple[OdooProfile, ...]:
        async with self._lock:
            profiles = [
                _deserialize_profile(item)
                for item in self._load()["profiles"]
                if item.get("principal_id") == principal.id
            ]
        return tuple(sorted(profiles, key=lambda profile: (profile.label.casefold(), profile.id)))

    async def set_default(self, principal: Principal, profile_id: str) -> None:
        async with self._lock:
            data = self._load()
            self._get_from_data(data, principal, profile_id)
            data["defaults"][principal.id] = profile_id
            self._persist(data)

    async def get_default(self, principal: Principal) -> OdooProfile:
        async with self._lock:
            data = self._load()
            profile_id = data["defaults"].get(principal.id)
            if not isinstance(profile_id, str) or not profile_id:
                raise ProfileNotFoundError(f"Principal {principal.id!r} has no default Odoo profile.")
            return self._get_from_data(data, principal, profile_id)

    def _get_from_data(
        self,
        data: dict[str, Any],
        principal: Principal,
        profile_id: str,
    ) -> OdooProfile:
        for item in data["profiles"]:
            if item.get("principal_id") == principal.id and item.get("id") == profile_id:
                return _deserialize_profile(item)
        raise ProfileNotFoundError(f"Profile {profile_id!r} is not available to principal {principal.id!r}.")

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return _empty_document()
        if self._path.is_symlink():
            raise ProfileRepositoryError("Profile metadata path cannot be a symbolic link.")

        mode = stat.S_IMODE(self._path.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ProfileRepositoryError("Profile metadata file must be owner-only (mode 600).")

        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileRepositoryError(f"Failed to read profile metadata from {self._path}.") from exc
        _validate_document(data)
        return data

    def _persist(self, data: dict[str, Any]) -> None:
        _validate_document(data)
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._path.parent.chmod(0o700)
        except OSError as exc:
            raise ProfileRepositoryError(f"Failed to secure profile directory {self._path.parent}.") from exc

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            dir=self._path.parent,
            text=True,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self._path)
            self._path.chmod(0o600)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def _empty_document() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "profiles": [], "defaults": {}}


def _serialize_profile(profile: OdooProfile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "principal_id": profile.principal_id,
        "label": profile.label,
        "canonical_url": profile.canonical_url,
        "database": profile.database,
        "username": profile.username,
        "company_id": profile.company_id,
        "odoo_major": profile.odoo_major,
        "transport": profile.transport.value,
        "credential_version": profile.credential_version,
        "state": profile.state.value,
    }


def _deserialize_profile(data: dict[str, Any]) -> OdooProfile:
    try:
        return OdooProfile(
            id=str(data["id"]),
            principal_id=str(data["principal_id"]),
            label=str(data["label"]),
            canonical_url=str(data["canonical_url"]),
            database=str(data["database"]),
            username=str(data["username"]),
            company_id=data.get("company_id"),
            odoo_major=int(data["odoo_major"]),
            transport=OdooTransport(str(data["transport"])),
            credential_version=int(data["credential_version"]),
            state=ProfileState(str(data["state"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProfileRepositoryError("Profile metadata contains an invalid profile.") from exc


def _validate_document(data: object) -> None:
    if not isinstance(data, dict):
        raise ProfileRepositoryError("Profile metadata must be a JSON object.")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ProfileRepositoryError("Unsupported profile metadata schema version.")
    if not isinstance(data.get("profiles"), list) or not isinstance(data.get("defaults"), dict):
        raise ProfileRepositoryError("Profile metadata has an invalid document shape.")

    def _reject_secret_keys(value: object) -> None:
        if isinstance(value, dict):
            forbidden = FORBIDDEN_SECRET_KEYS.intersection(value)
            if forbidden:
                names = ", ".join(sorted(forbidden))
                raise ProfileRepositoryError(f"Profile metadata cannot contain secret fields: {names}.")
            for child in value.values():
                _reject_secret_keys(child)
        elif isinstance(value, list):
            for child in value:
                _reject_secret_keys(child)

    _reject_secret_keys(data)

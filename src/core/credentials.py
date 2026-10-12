"""
Credential management — config file per user.

Credentials are stored in ~/.config/odoo-mcp/credentials.json (mode 600).

Written by local browser or terminal onboarding after authentication. The MCP
setup tool returns a browser continuation and never receives the API key.

Each user in Claude Cowork gets their own sandbox home directory, so the
file is naturally per-user and invisible to other users.

File format (multi-database):
    {
        "databases": {
            "mydb": {
                "url": "https://my.odoo.com",
                "username": "jente@mydb.com",
                "api_key": "..."
            }
        },
        "default_db": "mydb"
    }

Usage:
    # In Claude Cowork: tell Claude "set up my Odoo credentials"
    # Claude calls the odoo_setup_credentials tool → writes config file

    # In code
    from src.core.credentials import get_odoo_credentials
    creds = get_odoo_credentials()          # uses default_db
    creds = get_odoo_credentials("mydb")    # explicit db
"""

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Config file path — per user, in their home directory
CONFIG_PATH = Path.home() / ".config" / "odoo-mcp" / "credentials.json"


def setup_advice() -> str:
    """Return the canonical user-facing credential setup instruction."""
    return (
        "Ask Claude to create an Odoo connection. "
        "Claude can run odoo_setup_credentials and provide a local browser setup link. "
        "Enter login details and the API key in that form, never in chat."
    )


@dataclass(frozen=True)
class OdooCredentials:
    """Immutable credential container. Never serialized, never logged."""

    url: str       # e.g. https://my.odoo.com
    db: str        # e.g. mydb
    username: str  # e.g. jente@mydb.com
    api_key: str   # Odoo API key (Settings > API Keys)
    transport: str | None = None  # Unspecified connections default to Odoo 19 / JSON-2.

    def __repr__(self) -> str:
        """Never expose credentials in logs or repr."""
        return f"OdooCredentials(url={self.url!r}, db={self.db!r}, username={self.username!r}, api_key='***')"


def _migrate_legacy_format(data: dict) -> dict | None:
    """Return a migrated dict if data is the old flat format, otherwise None."""
    if "databases" not in data and data.get("db"):
        db_name = data["db"]
        return {
            "databases": {
                db_name: {
                    "url": data.get("url", ""),
                    "username": data.get("username", ""),
                    "api_key": data.get("api_key", ""),
                }
            },
            "default_db": db_name,
        }
    return None


def _load_raw() -> dict:
    """Load and return the raw credentials file, migrating legacy flat format if needed."""
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Odoo credentials not found at {CONFIG_PATH}. "
            + setup_advice()
        )
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except Exception as exc:
        raise RuntimeError(f"Failed to read credentials file {CONFIG_PATH}: {exc}") from exc

    migrated = _migrate_legacy_format(data)
    if migrated is not None:
        _persist_credentials(migrated)
        return migrated

    return data


def _persist_credentials(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Create with owner-only permissions before writing any secret, then replace
    # atomically so a crash cannot truncate existing database profiles.
    fd, temporary = tempfile.mkstemp(dir=CONFIG_PATH.parent, prefix=".credentials-")
    try:
        with os.fdopen(fd, "w") as output:
            output.write(json.dumps(data, indent=2))
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, CONFIG_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def get_odoo_credentials(db: str | None = None) -> OdooCredentials:
    """Read credentials from ~/.config/odoo-mcp/credentials.json.

    Args:
        db: Database name to load. Defaults to the stored default_db.

    Raises:
        RuntimeError: File missing, db not found, or entry incomplete.
    """
    data = _load_raw()

    databases: dict = data.get("databases", {})
    if not databases:
        raise RuntimeError(
            "No databases configured in credentials file. "
            + setup_advice()
        )

    target_db = db or data.get("default_db")
    if not target_db:
        raise RuntimeError(
            "No default database set in credentials file. "
            + setup_advice()
        )

    if target_db not in databases:
        available = ", ".join(databases.keys())
        raise RuntimeError(
            f"Database '{target_db}' not found in credentials file. "
            f"Available: {available}. "
            + setup_advice()
        )

    entry = databases[target_db]
    url = entry.get("url")
    username = entry.get("username")
    api_key = entry.get("api_key")

    missing = [k for k, v in [("url", url), ("username", username), ("api_key", api_key)] if not v]
    if missing:
        raise RuntimeError(
            f"Credentials for database '{target_db}' are incomplete — missing: {', '.join(missing)}. "
            + setup_advice()
        )

    transport = entry.get("transport")
    if transport is not None and transport not in {"xmlrpc", "json2"}:
        raise RuntimeError("Stored Odoo transport must be xmlrpc or json2.")
    return OdooCredentials(url=url.rstrip("/"), db=target_db, username=username, api_key=api_key,
                           transport=transport)


def list_databases() -> list[str]:
    """Return the list of stored database names, or an empty list if no credentials file exists."""
    if not CONFIG_PATH.exists():
        return []
    try:
        data = _load_raw()
        return list(data.get("databases", {}).keys())
    except Exception:
        return []


def get_default_db() -> str | None:
    """Return the current default database name, or None."""
    if not CONFIG_PATH.exists():
        return None
    try:
        data = _load_raw()
        return data.get("default_db")
    except Exception:
        return None


def store_odoo_credentials_file(url: str, db: str, username: str, api_key: str, *, transport: str | None = None) -> None:
    """Add or update a database entry in ~/.config/odoo-mcp/credentials.json.

    The saved database becomes the new default_db.
    Used only by local browser/terminal onboarding, never by MCP credential arguments.
    """
    if transport is not None and transport not in {"xmlrpc", "json2"}:
        raise ValueError("Odoo transport must be xmlrpc or json2.")
    # An unreadable existing file must fail rather than erase unrelated profiles.
    data = _load_raw() if CONFIG_PATH.exists() else {"databases": {}, "default_db": None}

    databases: dict = data.setdefault("databases", {})
    databases[db] = {"url": url, "username": username, "api_key": api_key}
    if transport is not None:
        databases[db]["transport"] = transport
    data["default_db"] = db

    _persist_credentials(data)


def set_default_database(db: str) -> None:
    """Set the default database for future connections.

    Raises:
        RuntimeError: Database not found in credentials file.
    """
    data = _load_raw()
    databases = data.get("databases", {})
    if db not in databases:
        available = ", ".join(databases.keys())
        raise RuntimeError(
            f"Database '{db}' not found in credentials file. "
            f"Available: {available}."
        )
    data["default_db"] = db
    _persist_credentials(data)

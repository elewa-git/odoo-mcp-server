"""Secure terminal-only fallback for transitional local Odoo onboarding.

The MCP server must never receive an Odoo API key in a tool argument or an
elicitation response. This module is deliberately a separate local process:
the secret is read only from the controlling terminal with :func:`getpass`.
"""

from __future__ import annotations

import asyncio
import getpass
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.core.credentials import OdooCredentials, store_odoo_credentials_file
from src.mcp.odoo.connection.client import OdooClient

TerminalInput = Callable[[str], str]
TerminalSecretInput = Callable[[str], str]
TerminalOutput = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class LocalOnboardingResult:
    """Non-secret details that are safe to report after local onboarding."""

    url: str
    db: str
    username: str
    odoo_major: int
    transport: str


def transport_for_odoo_major(odoo_major: int) -> str:
    """Select the only supported transport for a declared Odoo generation."""
    if odoo_major < 1:
        raise ValueError("Odoo major version must be a positive integer.")
    # Odoo 19 removed XML-RPC endpoints, so accepting a caller-selected
    # transport would create an unsafe compatibility bypass.
    return "xmlrpc" if odoo_major <= 18 else "json2"


async def onboard_local_terminal(
    *,
    input_fn: TerminalInput = input,
    getpass_fn: TerminalSecretInput = getpass.getpass,
    output_fn: TerminalOutput = print,
    client_factory: Callable[..., OdooClient] = OdooClient,
    store_credentials: Callable[[str, str, str, str], None] = store_odoo_credentials_file,
) -> LocalOnboardingResult | None:
    """Validate terminal-entered credentials before using the legacy local store.

    This is intentionally transitional: the legacy file is owner-only, but the
    target architecture replaces it with an OS credential store/custody provider.
    The function returns only non-secret metadata and never includes an exception
    message in terminal output because upstream libraries may echo credentials.
    """
    url = input_fn("Odoo URL (for example https://my.odoo.com): ").strip().rstrip("/")
    db = input_fn("Odoo database name: ").strip()
    username = input_fn("Odoo login email: ").strip()
    major_text = input_fn("Odoo major version: ").strip()

    try:
        odoo_major = int(major_text)
        transport = transport_for_odoo_major(odoo_major)
    except ValueError:
        output_fn("Odoo major version must be a positive integer.")
        return None

    parsed_url = urlsplit(url)
    is_loopback_http = parsed_url.scheme == "http" and parsed_url.hostname in {
        "127.0.0.1",
        "::1",
        "localhost",
    }
    if (
        (parsed_url.scheme != "https" and not is_loopback_http)
        or not parsed_url.netloc
        or not db
        or not username
    ):
        output_fn(
            "URL, database name, and login email are required; use HTTPS or local loopback HTTP."
        )
        return None

    # getpass reads from the terminal without echoing; do not move this value
    # into a model, result object, exception, or terminal message.
    api_key = getpass_fn("Odoo API key (input hidden): ").strip()
    if not api_key:
        output_fn("An Odoo API key is required.")
        return None

    credentials = OdooCredentials(url=url, db=db, username=username, api_key=api_key)
    try:
        client = client_factory(credentials=credentials, transport=transport)
    except Exception:
        # A transport constructor can include request settings in its error, so
        # keep the terminal boundary as generic as the authentication failure.
        output_fn("Could not prepare an Odoo connection for validation.")
        return None
    authenticated = False
    try:
        await client.authenticate()
        authenticated = True
    except Exception:
        # Authentication errors may include HTTP request details. Suppress them
        # rather than risking an API key reflected by a remote or client error.
        output_fn("Authentication failed. Check the URL, database, email, API key, and Odoo version.")
        return None
    finally:
        # Close even after rejected authentication so a transient client cannot
        # keep the secret-bearing connection state alive longer than necessary.
        try:
            await client.close()
        except Exception:
            pass

    if not authenticated:  # Defensive fence for future client-factory changes.
        return None

    try:
        # This legacy JSON persistence call enforces mode 0600. It is reached
        # only after authentication, so a failed onboarding cannot alter state.
        store_credentials(credentials.url, credentials.db, credentials.username, credentials.api_key)
    except Exception:
        output_fn("Validated credentials could not be saved locally.")
        return None

    result = LocalOnboardingResult(
        url=credentials.url,
        db=credentials.db,
        username=credentials.username,
        odoo_major=odoo_major,
        transport=transport,
    )
    output_fn(f"Credentials validated and saved for database '{result.db}' using {result.transport}.")
    return result


def main() -> None:
    """Run the local fallback without importing the MCP server composition."""
    asyncio.run(onboard_local_terminal())

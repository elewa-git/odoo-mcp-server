from __future__ import annotations

import asyncio
import logging
import ssl
import xmlrpc.client
from functools import partial
from typing import Any

import certifi

from src.core.credentials import OdooCredentials, setup_advice
from src.mcp.odoo.connection.base_transport import BaseTransportClient, parse_odoo_major

logger = logging.getLogger(__name__)


class XmlRpcTransportClient(BaseTransportClient):
    """XML-RPC transport backend for Odoo 18 and below.

    This is the broad-compatibility path and remains the default transport.
    It preserves the old object-service calling convention: positional args plus
    a keyword-argument mapping passed to ``execute_kw``.
    """

    transport_name = "xmlrpc"

    def __init__(self, credentials: OdooCredentials):
        super().__init__(credentials)
        # XML-RPC needs a uid returned by authenticate before object calls can
        # be made, so we keep uid and server proxies on the transport itself.
        self._uid: int | None = None
        self._common: xmlrpc.client.ServerProxy | None = None
        self._models: xmlrpc.client.ServerProxy | None = None

    async def authenticate(self) -> int:
        # XML-RPC libraries are blocking, so all network interaction stays on a
        # worker thread to keep the async MCP server responsive.
        loop = asyncio.get_running_loop()
        ssl_context = ssl.create_default_context(cafile=certifi.where())

        # ``common`` handles authenticate/version-style calls; ``object`` is the
        # regular ORM surface used by execute_kw.
        self._common = xmlrpc.client.ServerProxy(
            f"{self._creds.url}/xmlrpc/2/common", context=ssl_context
        )
        self._models = xmlrpc.client.ServerProxy(
            f"{self._creds.url}/xmlrpc/2/object", context=ssl_context
        )

        # Odoo XML-RPC returns the numeric uid that must be supplied on every
        # subsequent object call.
        uid = await loop.run_in_executor(
            None,
            partial(
                self._common.authenticate,
                self._creds.db,
                self._creds.username,
                self._creds.api_key,
                {},
            ),
        )
        if not uid:
            raise ConnectionError(
                f"Odoo authentication failed for {self._creds.username} at {self._creds.url}. "
                + setup_advice()
            )

        # Enforce version-based transport policy:
        # Odoo 18 and below -> XML-RPC, Odoo 19 and above -> JSON-2.
        version_info = await loop.run_in_executor(None, self._common.version)
        version_text = ""
        if isinstance(version_info, dict):
            version_text = str(version_info.get("server_version", ""))
        major = parse_odoo_major(version_text)
        if major is not None and major >= 19:
            raise ConnectionError(
                f"Odoo {version_text or major} detected. XML-RPC is only allowed for Odoo 18 and below. "
                "Set ODOO_TRANSPORT=json2."
            )

        self._uid = uid
        logger.info("Authenticated over XML-RPC as uid=%s on %s", uid, self._creds.url)
        return uid

    async def close(self) -> None:
        # ServerProxy has no async close; dropping references is enough for this
        # lightweight transport object.
        self._common = None
        self._models = None

    async def execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        # Fail fast if callers try to use this transport before authenticate.
        if not self._uid or not self._models:
            raise RuntimeError("Not authenticated. " + setup_advice())

        loop = asyncio.get_running_loop()
        # Keep XML-RPC argument semantics intact: positional args remain a list,
        # kwargs remain the execute_kw mapping.
        return await loop.run_in_executor(
            None,
            partial(
                self._models.execute_kw,
                self._creds.db,
                self._uid,
                self._creds.api_key,
                model,
                method,
                list(args) if args else [],
                kwargs if kwargs else {},
            ),
        )

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.core.credentials import OdooCredentials, setup_advice
from src.mcp.odoo.connection.base_transport import BaseTransportClient, parse_odoo_major

logger = logging.getLogger(__name__)


class Json2TransportClient(BaseTransportClient):
    """External JSON-2 transport backend for Odoo 19+.

    Odoo's JSON-2 API is HTTP-based and named-parameter-only, so this transport
    is responsible for translating the facade's XML-RPC-shaped helper methods
    into JSON-2 endpoint paths and request bodies.
    """

    transport_name = "json2"

    def __init__(self, credentials: OdooCredentials):
        super().__init__(credentials)
        # JSON-2 uses bearer-token auth rather than uid/password pairs. The key
        # comes only from this client binding so one profile cannot inherit a
        # process-wide credential intended for another profile.
        self._api_key = credentials.api_key
        self._user_id: int | None = None
        # Keep one async HTTP client per transport instance so headers, base URL,
        # and connection pooling are pinned for the whole session.
        self._http = httpx.AsyncClient(
            base_url=credentials.url.rstrip("/"),
            headers={
                "Authorization": f"bearer {self._api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "odoo-mcp-server/json2",
                "X-Odoo-Database": credentials.db,
            },
            timeout=30.0,
        )

    @property
    def compatibility_hints(self) -> list[str]:
        # Surface current transport limitations explicitly so runtime diagnostics
        # can describe what is safe to expect from the JSON-2 implementation.
        return [
            "JSON-2 is supported in Odoo 19+.",
            "This transport maps core reads, internal note posting, and create/write mutations.",
            "create/write mapping targets Odoo 19 JSON-2 semantics; validate with a single record "
            "if your instance rejects the request body.",
        ]

    async def authenticate(self) -> int:
        # JSON-2 has no XML-RPC-style authenticate(uid/password) handshake. The
        # first real proof of validity is making authenticated HTTP requests.
        if not self._api_key:
            raise ConnectionError(
                "JSON-2 transport requires credentials.api_key. " + setup_advice()
            )

        # Query Odoo's replacement for the deprecated common-service version call
        # so we can sanity-check that the target instance is the expected vintage.
        version_response = await self._http.get("/web/version")
        version_response.raise_for_status()
        version_data = version_response.json()
        version_text = str(version_data.get("version", ""))
        major = parse_odoo_major(version_text)
        if major is not None and major <= 18:
            raise ConnectionError(
                f"Odoo {version_text or major} detected. JSON-2 is only allowed for Odoo 19 and above. "
                "Set ODOO_TRANSPORT=xmlrpc."
            )

        # JSON-2 identifies the caller through the bearer token itself. The docs
        # recommend ``res.users/context_get`` to recover the current user context.
        context = await self.execute("res.users", "context_get")
        uid = context.get("uid") if isinstance(context, dict) else None
        if not isinstance(uid, int) or uid < 1:
            raise ConnectionError(
                "JSON-2 authentication succeeded but current user id could not be resolved via res.users/context_get."
            )

        self._user_id = uid
        logger.info("Authenticated over JSON-2 as uid=%s on %s", uid, self._creds.url)
        return uid

    async def close(self) -> None:
        # Unlike XML-RPC, HTTP clients own sockets and need an explicit async close.
        await self._http.aclose()

    async def execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        # Convert the facade's normalized call shape into the exact JSON-2 HTTP
        # endpoint and named JSON body expected by Odoo.
        payload = self._json2_payload(model, method, args, kwargs)
        response = await self._http.post(f"/json/2/{model}/{method}", json=payload)
        response.raise_for_status()
        return response.json()

    def _json2_payload(
        self,
        model: str,
        method: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        # JSON-2 has no positional-argument transport contract. This mapper is
        # the single place where we translate supported high-level methods into
        # named request bodies.
        payload: dict[str, Any] = {}

        if method in {"search", "search_count", "search_read"}:
            # Search-family methods all start from a domain and then accept named
            # options such as fields/limit/offset/order.
            if not args:
                raise ValueError(f"{method} requires a domain argument.")
            payload["domain"] = args[0]
            payload.update(kwargs)
            return payload

        if method == "read":
            # Read needs explicit record ids in the JSON body, then any optional
            # named parameters like fields/load.
            if not args:
                raise ValueError("read requires ids.")
            payload["ids"] = args[0]
            payload.update(kwargs)
            return payload

        if method == "fields_get":
            # ``fields_get`` is model-level and relies only on named parameters.
            payload.update(kwargs)
            return payload

        if method == "context_get":
            # ``context_get`` is used here for auth/session discovery and takes
            # no positional ids in the supported flow.
            payload.update(kwargs)
            return payload

        if method == "message_post":
            # Internal notes stay directly executable, so JSON-2 must also map
            # chatter posting for the safe message_post subset.
            if not args:
                raise ValueError("message_post requires record ids.")
            payload["ids"] = args[0]
            payload.update(kwargs)
            return payload

        # JSON-2 bodies are named arguments matching the ORM method signature, plus
        # optional ids/context (per Odoo 19 external_api docs). The authoritative,
        # per-database contract is the instance's /doc endpoint — confirm there (or
        # via a single-record smoke test) before relying on these in bulk.
        if method == "create":
            # ORM signature create(vals_list); no recordset to bind. The facade passes
            # a single values dict — left as-is since create accepts a dict or a list.
            if not args:
                raise ValueError("create requires values.")
            payload["vals_list"] = args[0]
            payload.update(kwargs)
            return payload

        if method == "write":
            # ORM signature write(vals); ids bind the target recordset, the same
            # convention used by read/message_post above.
            if len(args) < 2:
                raise ValueError("write requires ids and values.")
            payload["ids"] = args[0]
            payload["vals"] = args[1]
            payload.update(kwargs)
            return payload

        # Fail closed for anything we have not explicitly mapped yet. This keeps
        # transport support honest and prevents accidental mixed-semantics calls.
        raise NotImplementedError(
            f"JSON-2 transport does not yet support method {model}.{method}. "
            "Use XML-RPC for this operation."
        )

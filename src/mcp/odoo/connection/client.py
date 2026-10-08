"""
Odoo API Client.

Transport-selected Odoo client with built-in safety guard.
Each OdooClient instance binds to exactly one transport backend for its whole
session so a single connection never mixes XML-RPC and JSON-2 calls.

Usage:
    from src.mcp.odoo.connection.client import OdooClient

    async with OdooClient() as odoo:
        # Read operations — no approval needed
        leads = await odoo.search_read("crm.lead", [("user_id", "=", uid)], ["name", "stage_id"])

        # Write operations — safety-checked, still needs human approval upstream
        await odoo.write("crm.lead", [lead_id], {"stage_id": new_stage_id})

        # Internal notes — safe
        await odoo.log_note("crm.lead", lead_id, "Updated stage based on email analysis")
"""

from typing import Any

from src.core.credentials import OdooCredentials, get_odoo_credentials, setup_advice
from src.mcp.odoo.connection.base_transport import transport_from_env
from src.mcp.odoo.connection.transport_factory import build_transport_client
from src.mcp.odoo.utils.safety import SafetyGuard


class OdooClient:
    """
    Async-compatible Odoo client facade with transport-specific backends.

    Each instance binds to one transport backend at construction time so callers
    do not mix XML-RPC and JSON-2 within the same session.
    """

    def __init__(self, credentials: OdooCredentials | None = None, transport: str | None = None):
        """
        Initialize from legacy local configuration or an explicit connection binding.

        Args:
            credentials: Explicit credentials for a profile-bound connection. If
                omitted, the legacy local configuration is used.
            transport: Transport pinned by the profile-bound factory. It is
                required when explicit credentials are supplied; the no-argument
                legacy local path still reads ODOO_TRANSPORT.
        """
        # An explicit credential object must carry its profile-selected transport.
        # Falling back to process state here could connect the same credentials
        # through a transport selected for an unrelated profile.
        if credentials is not None and transport is None:
            raise ValueError("Explicit Odoo credentials require an explicit transport.")

        # Keep the zero-argument path during the local-stdio migration. It is
        # deliberately the only path allowed to read credentials or transport
        # selection from this process's environment and local configuration.
        self._creds = credentials if credentials is not None else get_odoo_credentials()
        self._transport_name = (transport if transport is not None else transport_from_env()).strip().lower()
        # Safety checks stay at the facade level so the policy is applied no
        # matter which transport backend ultimately sends the request.
        self._safety = SafetyGuard()
        # The factory hides the concrete class selection from callers and keeps
        # transport-specific construction logic out of this business-facing API.
        self._transport = build_transport_client(self._creds, self._transport_name)
        self._uid: int | None = None
        # Odoo model IDs are database-specific. Keep the cache on this exact
        # client so one profile cannot reuse another profile's model identifier.
        self._model_id_cache: dict[str, int] = {}

    @property
    def db(self) -> str:
        return self._creds.db

    @property
    def transport_name(self) -> str:
        return self._transport.transport_name

    @property
    def compatibility_hints(self) -> list[str]:
        return self._transport.compatibility_hints

    async def __aenter__(self) -> "OdooClient":
        """Authenticate and return client."""
        # Support ``async with OdooClient()`` ergonomics for callers that want
        # transport setup/teardown bundled into one scope.
        await self.authenticate()
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Cleanup."""
        await self.close()

    async def close(self) -> None:
        """Release any transport resources held by this client."""
        # The facade does not know whether the backend owns sockets, proxies, or
        # something else; it just delegates shutdown to the transport instance.
        await self._transport.close()

    async def authenticate(self) -> int:
        """
        Authenticate with Odoo and return user ID.

        Returns:
            User ID (uid) for the authenticated user.

        Raises:
            ConnectionError: If authentication fails.
        """
        # Persist the resolved current-user id on the facade because higher-level
        # helpers such as ``get_current_user`` rely on it independent of backend.
        uid = await self._transport.authenticate()
        self._uid = uid
        return uid

    async def _execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        """Execute a model call via the transport selected for this client instance."""
        # Refuse to proxy calls before the transport has authenticated. This
        # keeps the facade state machine explicit and predictable for callers.
        if not self._uid:
            raise RuntimeError("Not authenticated. " + setup_advice())
        return await self._transport.execute(model, method, *args, **kwargs)

    async def search(self, model: str, domain: list, limit: int = 80, offset: int = 0) -> list[int]:
        """Search for record IDs matching a domain filter."""
        # Search is intentionally thin; transport-specific argument encoding is
        # handled below the facade.
        return await self._execute(model, "search", domain, limit=limit, offset=offset)

    async def read(self, model: str, ids: list[int], fields: list[str] | None = None) -> list[dict]:
        """Read specific records by ID."""
        # Preserve the existing high-level method signature used throughout the
        # server while letting each transport map it to its native wire format.
        kwargs = {"fields": fields} if fields else {}
        return await self._execute(model, "read", ids, **kwargs)

    async def search_read(
        self,
        model: str,
        domain: list,
        fields: list[str] | None = None,
        limit: int = 80,
        offset: int = 0,
        order: str | None = None,
    ) -> list[dict]:
        """Search and read in one call. Most common read operation."""
        # ``search_read`` is especially important to preserve as one method call
        # because Odoo documents the transaction-safety advantage explicitly.
        kwargs: dict[str, Any] = {"limit": limit, "offset": offset}
        if fields:
            kwargs["fields"] = fields
        if order:
            kwargs["order"] = order
        return await self._execute(model, "search_read", domain, **kwargs)

    async def search_count(self, model: str, domain: list) -> int:
        """Count records matching a domain filter."""
        return await self._execute(model, "search_count", domain)

    async def fields_get(self, model: str, attributes: list[str] | None = None) -> dict:
        """Get field definitions for a model."""
        # This helper feeds both schema introspection and write-validation flows,
        # so we keep the public method transport-neutral.
        attrs = attributes or ["string", "type", "help", "required", "selection"]
        return await self._execute(model, "fields_get", attributes=attrs)

    async def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        """Update records. Safety-checked against outbound communication rules."""
        # Safety is enforced before any backend-specific call mapping so policy
        # violations are rejected consistently across transports.
        self._safety.validate_write(model, "write", values)
        return await self._execute(model, "write", ids, values)

    async def create(self, model: str, values: dict[str, Any]) -> int:
        """Create a record. Safety-checked."""
        # Create follows the same pattern as write: validate first, then forward.
        self._safety.validate_write(model, "create", values)
        return await self._execute(model, "create", values)

    async def log_note(self, model: str, record_id: int, body: str) -> int:
        """Post an internal log note using the safe internal-note variants."""
        # Default to the Odoo 19 internal-note pattern because it is the current
        # supported path for direct safe note posting.
        kwargs = {
            "body": body,
            "message_type": "comment",
            "subtype_id": 2,
        }
        self._safety.validate_write(model, "message_post", kwargs)
        try:
            # Try the Odoo 19-safe path first.
            return await self._execute(model, "message_post", [record_id], **kwargs)
        except Exception:
            # Fall back for older servers that still expect the Odoo 18 note form.
            kwargs_v18 = {
                "body": body,
                "message_type": "note",
                "subtype_xmlid": "mail.mt_note",
            }
            self._safety.validate_write(model, "message_post", kwargs_v18)
            return await self._execute(model, "message_post", [record_id], **kwargs_v18)

    async def schedule_activity(
        self,
        model: str,
        record_id: int,
        summary: str,
        date_deadline: str,
        activity_type_id: int = 4,
        user_id: int | None = None,
        note: str = "",
    ) -> int:
        """Schedule an internal activity (to-do / reminder)."""
        # Activity creation is safe at the model-policy level but still goes
        # through the selected transport for the actual ORM call.
        self._safety.validate_model_access("mail.activity", "create")

        # Odoo activity creation needs the numeric ir.model id of the target
        # model, so we resolve that here once per model and cache it.
        values: dict[str, Any] = {
            "res_model_id": await self._get_model_id(model),
            "res_id": record_id,
            "summary": summary,
            "date_deadline": date_deadline,
            "activity_type_id": activity_type_id,
            "note": note,
        }
        if user_id:
            values["user_id"] = user_id

        return await self._execute("mail.activity", "create", values)

    async def _get_model_id(self, model_name: str) -> int:
        """Get the ir.model ID for a model name (cached)."""
        # Cache model IDs only for this bound client. Numeric IDs are not safe to
        # share between databases even when the Odoo model name is identical.
        if model_name in self._model_id_cache:
            return self._model_id_cache[model_name]
        result = await self.search_read("ir.model", [("model", "=", model_name)], ["id"], limit=1)
        if not result:
            raise ValueError(f"Model '{model_name}' not found in Odoo")
        self._model_id_cache[model_name] = result[0]["id"]
        return result[0]["id"]

    async def get_current_user(self) -> dict:
        """Get the currently authenticated user's profile."""
        # Use the stored uid from authenticate so callers can ask for current-user
        # data in a transport-neutral way after session setup.
        if not self._uid:
            raise RuntimeError("Not authenticated. " + setup_advice())
        users = await self.read("res.users", [self._uid], ["name", "email", "company_id"])
        return users[0] if users else {}

    async def get_company_ids(self) -> list[dict]:
        """Get all companies the user has access to."""
        # Keep this as a tiny convenience method because multiple tool surfaces
        # may need the same read pattern and transport-agnostic behavior.
        return await self.search_read("res.company", [], ["name", "id"])

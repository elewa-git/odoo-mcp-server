"""
Odoo MCP Server — unified endpoint for all Odoo domain tools.

This server exposes foundational (generic) Odoo tools plus domain-specific tools
registered from separate modules under ``src.mcp.odoo.tools``.

A single ``OdooClient`` instance is shared via the lifespan context — all domain
tool modules reuse it, avoiding duplicate connections. Legacy local credentials
may hold more than one database; an explicit durable default selects among them.

Prompts (read automatically by the assistant):
- ``odoo_write_flow``         — Mandatory 3-step create/update flow.
- ``odoo_database_selection`` — How to inspect and switch databases.
- ``odoo_safety_policy``      — What the server will and will not do.

Resources:
- ``odoo://models/{database}``    — List models in an explicitly selected database.
- ``odoo://model/{model_name}``   — Introspect a specific model's fields.

Read tools:
- ``odoo_ping``             — Validate connectivity and show authenticated user + active db.
- ``odoo_search_read``      — Read records with an Odoo domain filter.
- ``odoo_read_records``     — Read explicit records by model + IDs.
- ``odoo_fields_get``       — Introspect field definitions for a model.
- ``odoo_search_count``     — Count records matching a domain filter.
- ``odoo_diagnose_call``    — Non-executing analysis of a planned call.

Write tools (3-step, approval-gated, fail-closed):
- ``odoo_preview_write``          — Step 1: build a payload draft.
- ``odoo_validate_write``         — Step 2: safety + schema check, issue approval token.
- ``odoo_execute_approved_write`` — Step 3: execute after explicit approval.
- ``odoo_log_internal_note``      — Post chatter notes with ``message_type='note'`` only.
- ``odoo_schedule_activity``      — Create internal reminder activities.

Writes are off by default and require the ``ODOO_MCP_ENABLE_WRITES`` server
policy flag plus a durable exact approval. Protocol sessions never grant authority.
Deletes and outbound email are never permitted.

Database compatibility tools:
- ``odoo_setup_credentials``      — Add/update credentials for a database.
- ``odoo_list_databases``         — List stored databases and the default.
- ``odoo_switch_database``        — Persist and activate a legacy local default.
- ``odoo_enable_session_writes``  — Deprecated compatibility alias; never grants authority.
- ``odoo_disable_session_writes`` — Deprecated compatibility alias; no session state exists.
- ``odoo_runtime_info``           — Runtime diagnostics and write status.

Run:
    python -m src.mcp.odoo.server
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
from importlib import metadata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date as _date, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.core.approvals import (
    ApprovalRepository,
    ApprovalRepositoryError,
    UnavailableApprovalRepository,
)
from src.mcp.odoo.connection.client import OdooClient
from src.mcp.odoo.connection.base_transport import transport_from_env
from src.core.identity import (
    LocalInstallationPrincipalProvider,
    PrincipalProvider,
    PrincipalUnavailableError,
    UnavailablePrincipalProvider,
)
from src.core.onboarding import (
    OnboardingProvider,
    OnboardingUnavailableError,
    UnavailableOnboardingProvider,
)
from src.core.sqlite_approval_repository import SqliteApprovalRepository
from src.core.credentials import (
    setup_advice,
    list_databases,
    get_default_db,
    set_default_database,
)
from src.mcp.odoo.utils.diagnostics import diagnose_odoo_call
from src.mcp.odoo.utils.safety import SafetyGuard, SafetyViolation
from src.mcp.odoo.utils.update_manager import (
    apply_self_update,
    build_upgrade_command,
    check_for_update,
    default_repo,
    fetch_latest_release_or_tag,
)
APPROVAL_PATH = Path.home() / ".config" / "odoo-mcp" / "approvals.sqlite3"


def _truthy_env(name: str) -> bool:
    # Standard truthy env parser used for runtime safety gates.
    value = os.environ.get(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


# ── Lifespan ──


@dataclass
class AppContext:
    """Application context holding the shared OdooClient."""

    odoo: OdooClient | None
    auth_error: str | None = None
    session_db: str | None = None  # Transitional local profile coordinate; not write authority.
    principal_provider: PrincipalProvider = field(default_factory=UnavailablePrincipalProvider)
    onboarding_provider: OnboardingProvider = field(default_factory=UnavailableOnboardingProvider)
    approval_repository: ApprovalRepository = field(default_factory=UnavailableApprovalRepository)


@asynccontextmanager
async def app_lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
    """Initialize and share a single OdooClient for the server session.

    Never crash on auth failure — store the error on AppContext so every
    tool can return it to Claude, who will relay the setup instruction.
    If multiple legacy databases are configured, defer connection until the
    first tool call resolves the durable default.
    """
    client: OdooClient | None = None
    auth_error: str | None = None
    session_db: str | None = None
    dbs = list_databases()
    if len(dbs) == 1:
        try:
            client = OdooClient()
            await client.authenticate()
            session_db = client.db
        except (RuntimeError, Exception) as exc:
            auth_error = f"Odoo is not authenticated: {exc}"
            client = None
    elif len(dbs) == 0:
        # No credentials at all — let odoo_setup_credentials handle it.
        auth_error = None
    # len(dbs) > 1: defer; _require_odoo resolves only the durable default.
    try:
        yield AppContext(
            odoo=client,
            auth_error=auth_error,
            session_db=session_db,
            principal_provider=LocalInstallationPrincipalProvider(getpass.getuser()),
            approval_repository=SqliteApprovalRepository(APPROVAL_PATH),
        )
    finally:
        if client is not None:
            await client.close()


mcp = MCPServer(
    "odoo_mcp",
    lifespan=app_lifespan,
)


@mcp.prompt()
def odoo_write_flow() -> str:
    """System instructions for the mandatory 3-step write flow."""
    return """
# Odoo write flow — MANDATORY

Any operation that creates, updates, or calls records in Odoo MUST follow this exact 3-step sequence.
Never attempt to skip or combine steps.

## Step 1 — Preview: `odoo_preview_write`
Build a canonical payload draft. Does not touch Odoo. Returns a `payload` object and instructs you to proceed to step 2.

## Step 2 — Validate: `odoo_validate_write`
Pass the payload from step 1. Runs safety checks and live schema validation against Odoo.
On success returns an `approval.token` (short-lived, single-use). **Show the payload to the user and ask for explicit approval before proceeding.**

## Step 3 — Execute: `odoo_execute_approved_write`
Pass the `approval_token` and exact validated `payload` from step 2, then set `confirm=true`.
This is the only step that writes to Odoo.
`confirm=true` must reflect genuine user approval — never set it speculatively.
Writes must also be enabled in the extension configuration; if not, execution fails with a clear error.

## Important
- Approval tokens expire and are single-use. If a token expires before step 3, restart from step 1.
- If `writes_currently_allowed: false` appears in `odoo_runtime_info`, writes are off.
  An administrator must enable the server policy gate; a protocol session cannot do so.
""".strip()


@mcp.prompt()
def odoo_database_selection() -> str:
    """How to work with multiple Odoo databases in a session."""
    return """
# Working with legacy local Odoo databases

This server may hold credentials for more than one Odoo database.

- **Which database am I connected to?** Call `odoo_ping` — the `db` field is the active local default.
- **What databases are available?** Call `odoo_list_databases` — it returns all stored databases and the file-level default.
- **Switch databases:** Call `odoo_switch_database` with the target `db`. This reconnects the local process and persists the new default.
- **First call:** If multiple legacy databases are configured, the server uses the durable local default. Use `odoo_switch_database` to change it explicitly.
- **Add a new database:** Call `odoo_setup_credentials`. The newly added database becomes the active default.

When a request is database-specific and the durable default is ambiguous, require an explicit selection before reading or writing.
""".strip()


@mcp.prompt()
def odoo_safety_policy() -> str:
    """What this server will and will not do to Odoo."""
    return """
# Odoo safety policy

This server is deliberately constrained:

- **Reads are unrestricted** — search, read, count, and schema introspection are always available.
- **Writes are gated** — create/update only via the 3-step write flow (`odoo_preview_write` → `odoo_validate_write` → `odoo_execute_approved_write`), each requiring explicit user approval and the server policy gate. Protocol sessions never enable writes.
- **No deletes** — there is no delete/unlink capability. Do not promise to delete records; suggest archiving, or doing it manually in Odoo.
- **No outbound communication** — the SafetyGuard blocks email-type messages and other outbound channels. Only internal chatter notes (`message_type='note'`) are permitted, via `odoo_log_internal_note`. Do not attempt to send customer-facing email through write tools; it will be rejected.

If an operation is blocked, explain the constraint to the user rather than retrying.
""".strip()


async def _require_odoo(ctx: Context) -> OdooClient | str:
    """Return the active local Odoo client using only durable explicit selection.

    Returns the client, or an error string the tool should return directly.
    """
    from src.core.credentials import get_odoo_credentials

    context: AppContext = ctx.request_context.lifespan_context

    if context.auth_error:
        return context.auth_error
    if context.odoo is not None:
        return context.odoo

    dbs = list_databases()
    if not dbs:
        return "No Odoo credentials configured. " + setup_advice()

    db = dbs[0] if len(dbs) == 1 else get_default_db()
    if db is None or db not in dbs:
        return (
            f"Multiple Odoo databases are configured: {', '.join(dbs)}. "
            "Use odoo_switch_database to set a durable default explicitly."
        )

    try:
        creds = get_odoo_credentials(db)
        client = OdooClient(credentials=creds, transport=transport_from_env())
        await client.authenticate()
    except Exception as exc:
        return f"Failed to connect to database '{db}': {exc}"

    context.odoo = client
    context.session_db = db
    return client


# ── MCP Resources ──


@mcp.resource(
    "odoo://models/{database}",
    description="List all available models in one explicitly selected Odoo database",
)
async def list_models(database: str, ctx: Context) -> str:
    """Resource: list installed model names without relying on prior session state."""
    from src.core.credentials import get_odoo_credentials

    try:
        credentials = get_odoo_credentials(database)
        odoo = OdooClient(credentials=credentials, transport=transport_from_env())
        await odoo.authenticate()
    except Exception as exc:
        return json.dumps({"error": f"Failed to connect to database {database!r}: {exc}"})

    try:
        records = await odoo.search_read(
            "ir.model", [], fields=["model", "name"], limit=0
        )
        models = sorted(records, key=lambda record: record["model"])
        return json.dumps(
            {
                "count": len(models),
                "models": [
                    {"model": record["model"], "name": record["name"]}
                    for record in models
                ],
            },
            indent=2,
        )
    finally:
        await odoo.close()


@mcp.resource(
    "odoo://model/{model_name}",
    description="Get information about a specific model including field definitions",
)
async def get_model_info(model_name: str, ctx: Context) -> str:
    """Resource: introspect a single Odoo model's metadata and fields."""
    odoo = await _require_odoo(ctx)
    if isinstance(odoo, str):
        return odoo
    model_info = await odoo.search_read(
        "ir.model", [("model", "=", model_name)], fields=["model", "name"], limit=1
    )
    if not model_info:
        return json.dumps({"error": f"Model '{model_name}' not found"}, indent=2)

    fields = await odoo.fields_get(model=model_name)
    return json.dumps(
        {
            "model": model_info[0]["model"],
            "name": model_info[0]["name"],
            "fields": fields,
        },
        indent=2,
    )


# ── Pydantic Input Schemas ──


class OdooSearchReadInput(BaseModel):
    """Input schema for read-only search_read queries."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., description="Odoo model name, e.g. 'crm.lead'")
    domain: list[Any] = Field(
        default_factory=list,
        description="Odoo domain filter as a list of tuples/lists, e.g. [['active','=',True]]",
    )
    fields: list[str] = Field(
        default_factory=list,
        description="Fields to return. Empty list returns Odoo defaults.",
    )
    limit: int = Field(default=80, ge=1, le=500, description="Maximum records to return")
    offset: int = Field(default=0, ge=0, description="Pagination offset")
    order: str | None = Field(default=None, description="Optional sort order, e.g. 'create_date desc'")


class OdooGetRecordInput(BaseModel):
    """Input schema for reading records by ID."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., description="Odoo model name")
    ids: list[int] = Field(..., description="Record IDs to read", min_length=1)
    fields: list[str] = Field(default_factory=list, description="Fields to include in the response")


class OdooLogNoteInput(BaseModel):
    """Input schema for internal chatter notes."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., description="Odoo model name, e.g. 'crm.lead'")
    record_id: int = Field(..., ge=1, description="Target record ID")
    body: str = Field(..., min_length=1, description="HTML or text body for the internal note")


class OdooScheduleActivityInput(BaseModel):
    """Input schema for scheduling internal activities."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., description="Odoo model name, e.g. 'crm.lead'")
    record_id: int = Field(..., ge=1, description="Target record ID")
    summary: str = Field(..., min_length=1, description="Short activity summary")
    date_deadline: str = Field(..., description="Deadline date in YYYY-MM-DD format")
    activity_type_id: int = Field(default=4, ge=1, description="Odoo activity type ID (4=To-Do by default)")
    user_id: int | None = Field(default=None, ge=1, description="Optional assignee user ID")
    note: str = Field(default="", description="Optional longer description")

    @field_validator("date_deadline")
    @classmethod
    def validate_date(cls, v: str) -> str:
        try:
            _date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"Invalid date format: {v}. Use YYYY-MM-DD.")
        return v


@mcp.tool(
    name="odoo_ping",
    description="Validate Odoo connectivity and return the authenticated user profile.",
    annotations={"title": "Ping Odoo", "readOnlyHint": True, "destructiveHint": False},
)
async def odoo_ping(ctx: Context) -> dict[str, Any] | str:
    """Tool: check Odoo connectivity and identity."""
    odoo = await _require_odoo(ctx)
    if isinstance(odoo, str):
        return odoo
    await ctx.info(f"[db: {odoo.db}] ping")
    user = await odoo.get_current_user()
    return {
        "ok": True,
        "db": odoo.db,
        "message": "Connected to Odoo successfully.",
        "user": user,
    }


@mcp.tool(
    name="odoo_search_read",
    description="Read Odoo records using a domain filter (safe, read-only).",
    annotations={"title": "Search and Read Records", "readOnlyHint": True, "destructiveHint": False},
)
async def odoo_search_read(input: OdooSearchReadInput, ctx: Context) -> list[dict[str, Any]] | str:
    """Tool: perform Odoo search_read against any model with strict input validation."""
    odoo = await _require_odoo(ctx)
    if isinstance(odoo, str):
        return odoo
    await ctx.info(f"[db: {odoo.db}] search_read {input.model}")
    fields = input.fields or None
    return await odoo.search_read(
        model=input.model,
        domain=input.domain,
        fields=fields,
        limit=input.limit,
        offset=input.offset,
        order=input.order,
    )


@mcp.tool(
    name="odoo_read_records",
    description="Read specific Odoo records by ID (safe, read-only).",
    annotations={"title": "Read Records By ID", "readOnlyHint": True, "destructiveHint": False},
)
async def odoo_read_records(input: OdooGetRecordInput, ctx: Context) -> list[dict[str, Any]] | str:
    """Tool: read one or more records for a given model and ID list."""
    odoo = await _require_odoo(ctx)
    if isinstance(odoo, str):
        return odoo
    await ctx.info(f"[db: {odoo.db}] read {input.model} ids={input.ids}")
    fields = input.fields or None
    return await odoo.read(model=input.model, ids=input.ids, fields=fields)


@mcp.tool(
    name="odoo_log_internal_note",
    description="Post an internal chatter note directly (never outbound email).",
    annotations={"title": "Log Internal Note", "readOnlyHint": False, "destructiveHint": False},
)
async def odoo_log_internal_note(input: OdooLogNoteInput, ctx: Context) -> dict[str, Any] | str:
    """Tool: create a safe internal note on an Odoo record."""
    odoo = await _require_odoo(ctx)
    if isinstance(odoo, str):
        return odoo
    message_id = await odoo.log_note(model=input.model, record_id=input.record_id, body=input.body)
    return {
        "ok": True,
        "tool": "odoo_log_internal_note",
        "message_id": message_id,
        "model": input.model,
        "record_id": input.record_id,
    }


@mcp.tool(
    name="odoo_schedule_activity",
    description="Build an internal activity proposal payload for staged approval. Does not execute directly.",
    annotations={"title": "Schedule Activity", "readOnlyHint": False, "destructiveHint": False},
)
async def odoo_schedule_activity(input: OdooScheduleActivityInput, ctx: Context) -> dict[str, Any] | str:
    """Tool: build an internal activity create payload for staged approval flow."""
    odoo = await _require_odoo(ctx)
    if isinstance(odoo, str):
        return odoo

    # Resolve model id now so the staged payload can be executed as plain create.
    model_ref = await odoo.search_read("ir.model", [("model", "=", input.model)], ["id"], limit=1)
    if not model_ref:
        return {
            "ok": False,
            "tool": "odoo_schedule_activity",
            "error": f"Model '{input.model}' not found in ir.model.",
        }

    values: dict[str, Any] = {
        "res_model_id": model_ref[0]["id"],
        "res_id": input.record_id,
        "summary": input.summary,
        "date_deadline": input.date_deadline,
        "activity_type_id": input.activity_type_id,
        "note": input.note,
    }
    if input.user_id:
        values["user_id"] = input.user_id

    payload = {
        "model": "mail.activity",
        "operation": "create",
        "record_ids": [],
        "values": values,
    }
    return {
        "ok": True,
        "tool": "odoo_schedule_activity",
        "mode": "proposal_only",
        "payload": payload,
        "next": [
            "Run odoo_preview_write with this payload.",
            "Run odoo_validate_write with the same payload.",
            "After explicit user approval, run odoo_execute_approved_write.",
        ],
    }


# ── Additional tools ──


class OdooFieldsGetInput(BaseModel):
    """Input schema for model field introspection."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., description="Odoo model name, e.g. 'crm.lead'")
    attributes: list[str] = Field(
        default_factory=lambda: ["string", "type", "help", "required", "selection"],
        description="Field metadata attributes to return",
    )


class OdooSearchCountInput(BaseModel):
    """Input schema for counting records."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., description="Odoo model name, e.g. 'crm.lead'")
    domain: list[Any] = Field(
        default_factory=list,
        description="Odoo domain filter as a list of tuples/lists",
    )


class OdooDiagnoseCallInput(BaseModel):
    """Input schema for non-executing call diagnostics."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., description="Odoo model name, e.g. 'crm.lead'")
    method: str = Field(..., description="Odoo method name, e.g. 'search_read', 'write'")
    args: list[Any] = Field(default_factory=list, description="Positional args you plan to pass")
    kwargs: dict[str, Any] = Field(default_factory=dict, description="Keyword args you plan to pass")


class OdooPreviewWriteInput(BaseModel):
    """Input schema for staged write preview."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., description="Odoo model name")
    operation: str = Field(..., description="One of: create, write, call")
    record_ids: list[int] = Field(default_factory=list, description="Required for write")
    values: dict[str, Any] = Field(default_factory=dict, description="Field payload for create/write")
    method: str | None = Field(default=None, description="Required for call operation, e.g. message_post")
    args: list[Any] = Field(default_factory=list, description="Positional args for call operation")
    kwargs: dict[str, Any] = Field(default_factory=dict, description="Keyword args for call operation")


class OdooValidateWriteInput(BaseModel):
    """Input schema for staged write validation."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., description="Odoo model name")
    operation: str = Field(..., description="One of: create, write, call")
    record_ids: list[int] = Field(default_factory=list, description="Required for write")
    values: dict[str, Any] = Field(default_factory=dict, description="Field payload for create/write")
    method: str | None = Field(default=None, description="Required for call operation, e.g. message_post")
    args: list[Any] = Field(default_factory=list, description="Positional args for call operation")
    kwargs: dict[str, Any] = Field(default_factory=dict, description="Keyword args for call operation")


class OdooExecuteApprovedWriteInput(BaseModel):
    """Input schema for approved write execution."""

    model_config = ConfigDict(extra="forbid")

    approval_token: str = Field(..., description="Token returned by odoo_validate_write")
    payload: OdooValidateWriteInput = Field(
        ...,
        description="Exact validated payload shown to and approved by the user",
    )
    confirm: bool = Field(..., description="Must be true to execute")


def _canonical_write_payload(
    input: OdooPreviewWriteInput | OdooValidateWriteInput,
) -> dict[str, Any]:
    """Build the one payload shape shared by preview, approval, and execution."""
    return {
        "model": input.model,
        "operation": input.operation.strip().lower(),
        "record_ids": input.record_ids,
        "values": input.values,
        "method": input.method,
        "args": input.args,
        "kwargs": input.kwargs,
    }


class OdooCheckForUpdateInput(BaseModel):
    """Input schema for MCP-native update checks."""

    model_config = ConfigDict(extra="forbid")

    repo: str | None = Field(
        default=None,
        description="Optional GitHub repo (owner/name). Defaults to canonical project source.",
    )
    timeout_seconds: int = Field(default=10, ge=3, le=60, description="GitHub API timeout in seconds")


class OdooApplySelfUpdateInput(BaseModel):
    """Input schema for guarded MCP self-update execution."""

    model_config = ConfigDict(extra="forbid")

    confirm: bool = Field(
        ...,
        description="Must be true to run a local package manager update command.",
    )
    repo: str | None = Field(
        default=None,
        description="Optional GitHub repo (owner/name). Defaults to canonical project source.",
    )
    ref: str | None = Field(
        default=None,
        description="Optional tag/branch/ref to install. Defaults to latest release/tag.",
    )
    timeout_seconds: int = Field(default=300, ge=30, le=900, description="Local update command timeout in seconds")


@mcp.tool(
    name="odoo_fields_get",
    description="Introspect field definitions for an Odoo model (names, types, help text).",
    annotations={"title": "Get Model Fields", "readOnlyHint": True, "destructiveHint": False},
)
async def odoo_fields_get(input: OdooFieldsGetInput, ctx: Context) -> dict[str, Any] | str:
    """Tool: retrieve field metadata for a given Odoo model."""
    odoo = await _require_odoo(ctx)
    if isinstance(odoo, str):
        return odoo
    await ctx.info(f"[db: {odoo.db}] fields_get {input.model}")
    return await odoo.fields_get(model=input.model, attributes=input.attributes)


@mcp.tool(
    name="odoo_search_count",
    description="Count Odoo records matching a domain filter (safe, read-only).",
    annotations={"title": "Count Records", "readOnlyHint": True, "destructiveHint": False},
)
async def odoo_search_count(input: OdooSearchCountInput, ctx: Context) -> dict[str, Any] | str:
    """Tool: count records matching a domain filter."""
    odoo = await _require_odoo(ctx)
    if isinstance(odoo, str):
        return odoo
    await ctx.info(f"[db: {odoo.db}] search_count {input.model}")
    count = await odoo.search_count(model=input.model, domain=input.domain)
    return {"model": input.model, "count": count}


@mcp.tool(
    name="odoo_diagnose_call",
    description="Dry-run analysis of a planned Odoo model call: validates basic payload shape, classifies method risk (read/destructive/side-effect), and returns issues/recommendations without executing anything.",
    annotations={"title": "Diagnose Odoo Call", "readOnlyHint": True, "destructiveHint": False},
)
async def odoo_diagnose_call(input: OdooDiagnoseCallInput) -> dict[str, Any]:
    """Tool: return non-executing diagnostics for a planned Odoo call."""
    return diagnose_odoo_call(
        model=input.model,
        method=input.method,
        args=input.args,
        kwargs=input.kwargs,
    )


@mcp.tool(
    name="odoo_preview_write",
    description="Write flow step 1 of 3 — start here for any Odoo create/write/call. Builds a canonical payload draft for review. Does not validate against live schema and does not execute. Pass the returned payload to odoo_validate_write.",
    annotations={"title": "Preview Write", "readOnlyHint": True, "destructiveHint": False},
)
async def odoo_preview_write(input: OdooPreviewWriteInput) -> dict[str, Any]:
    """Tool: generate a non-executing write draft for staged approval flow."""
    # Step 1: normalize and return a canonical payload draft for human/agent review.
    operation = input.operation.strip().lower()
    if operation not in {"create", "write", "call"}:
        return {
            "success": False,
            "tool": "odoo_preview_write",
            "error": "Unsupported operation. Use 'create', 'write', or 'call'.",
        }

    if operation == "write" and not input.record_ids:
        return {
            "success": False,
            "tool": "odoo_preview_write",
            "error": "record_ids are required for write operations.",
        }

    if operation == "call" and not (input.method and input.method.strip()):
        return {
            "success": False,
            "tool": "odoo_preview_write",
            "error": "method is required for call operations.",
        }

    payload = _canonical_write_payload(input)
    return {
        "success": True,
        "tool": "odoo_preview_write",
        "payload": payload,
        "next": "Run odoo_validate_write with the same payload.",
    }


@mcp.tool(
    name="odoo_validate_write",
    description="Write flow step 2 of 3 — call after odoo_preview_write. Runs SafetyGuard and live schema checks, then issues a short-lived single-use approval token. Show the payload to the user and obtain explicit approval before proceeding to step 3. Does not execute.",
    annotations={"title": "Validate Write", "readOnlyHint": True, "destructiveHint": False},
)
async def odoo_validate_write(input: OdooValidateWriteInput, ctx: Context) -> dict[str, Any] | str:
    """Tool: validate staged write payload and issue an approval token without execution."""
    # Step 2: syntactic operation validation.
    operation = input.operation.strip().lower()
    if operation not in {"create", "write", "call"}:
        return {
            "success": False,
            "tool": "odoo_validate_write",
            "error": "Unsupported operation. Use 'create', 'write', or 'call'.",
        }

    if operation == "write" and not input.record_ids:
        return {
            "success": False,
            "tool": "odoo_validate_write",
            "error": "record_ids are required for write operations.",
        }

    if operation == "call" and not (input.method and input.method.strip()):
        return {
            "success": False,
            "tool": "odoo_validate_write",
            "error": "method is required for call operations.",
        }

    # Apply outbound communication safety policy before any metadata checks.
    guard = SafetyGuard()
    try:
        guard.validate_write(
            input.model,
            input.method.strip() if operation == "call" and input.method else operation,
            input.kwargs if operation == "call" else input.values,
        )
    except SafetyViolation as exc:
        return {
            "success": False,
            "tool": "odoo_validate_write",
            "error": str(exc),
            "metadata_used": {"live_odoo": False},
        }

    odoo = await _require_odoo(ctx)
    if isinstance(odoo, str):
        return odoo

    metadata_used: dict[str, Any] = {"live_odoo": False}
    if operation in {"create", "write"}:
        # Live metadata is required so approval is based on current Odoo schema, not assumptions.
        field_meta = await odoo.fields_get(model=input.model, attributes=["string", "type", "required", "readonly"])
        if not field_meta:
            return {
                "success": False,
                "tool": "odoo_validate_write",
                "error": "fields_get returned empty metadata; refusing approval.",
                "metadata_used": {"live_odoo": False},
            }

        # Unknown fields are rejected to prevent drift and typo-based writes.
        unknown_fields = sorted(set(input.values.keys()) - set(field_meta.keys()))
        if unknown_fields:
            return {
                "success": False,
                "tool": "odoo_validate_write",
                "error": f"Unknown fields for model {input.model}: {', '.join(unknown_fields)}",
                "metadata_used": {"live_odoo": True},
            }
        metadata_used = {
            "live_odoo": True,
            "field_count": len(field_meta),
        }

    payload = _canonical_write_payload(input)
    context: AppContext = ctx.request_context.lifespan_context
    try:
        principal = await context.principal_provider.resolve()
        profile_id = context.session_db or odoo.db
        # Legacy local credentials have no rotation metadata. Version 1 is an
        # explicit migration fence until every call resolves an OdooProfile.
        approval = await context.approval_repository.issue(
            principal_id=principal.id,
            profile_id=profile_id,
            credential_version=1,
            payload=payload,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    except (PrincipalUnavailableError, ApprovalRepositoryError):
        return {
            "success": False,
            "tool": "odoo_validate_write",
            "error": "Write approval authority is unavailable.",
        }
    return {
        "success": True,
        "tool": "odoo_validate_write",
        "approval": {
            "token": approval.token,
            "expires_at": approval.expires_at.isoformat(),
            "requires_confirm": True,
        },
        "metadata_used": metadata_used,
        "next": (
            "Run odoo_execute_approved_write with approval_token, this exact payload, "
            "and confirm=true."
        ),
    }


@mcp.tool(
    name="odoo_execute_approved_write",
    description=(
        "Write flow step 3 of 3 — call only after the user explicitly approves the payload from step 2. "
        "Requires the approval_token, the exact validated payload, confirm=true, and the server write gate. "
        "Fails closed on any mismatch, replay, expiry, or unavailable authority."
    ),
    annotations={"title": "Execute Approved Write", "readOnlyHint": False, "destructiveHint": True},
)
async def odoo_execute_approved_write(input: OdooExecuteApprovedWriteInput, ctx: Context) -> dict[str, Any] | str:
    """Tool: execute a previously validated write under strict runtime gates."""
    # Step 3 gate A: explicit confirmation required at execution time.
    if not input.confirm:
        return {
            "success": False,
            "tool": "odoo_execute_approved_write",
            "error": "confirm must be true.",
        }

    # Step 3 gate B: a server policy gate is required for any mutating
    # execution. Protocol sessions cannot create or retain write authority.
    context: AppContext = ctx.request_context.lifespan_context
    if not _truthy_env("ODOO_MCP_ENABLE_WRITES"):
        return {
            "success": False,
            "tool": "odoo_execute_approved_write",
            "error": (
                "Write execution is disabled by server policy. An administrator must enable "
                "the ODOO_MCP_ENABLE_WRITES gate. Protocol sessions cannot enable it."
            ),
        }

    odoo = await _require_odoo(ctx)
    if isinstance(odoo, str):
        return odoo

    # Step 3 gate C: atomically reserve an exact principal/profile/payload
    # approval. No process-local map or protocol session grants authority.
    payload = _canonical_write_payload(input.payload)
    try:
        principal = await context.principal_provider.resolve()
        profile_id = context.session_db or odoo.db
        await context.approval_repository.reserve(
            input.approval_token,
            principal_id=principal.id,
            profile_id=profile_id,
            credential_version=1,
            payload=payload,
        )
    except (PrincipalUnavailableError, ApprovalRepositoryError):
        return {
            "success": False,
            "tool": "odoo_execute_approved_write",
            "error": "Approval token invalid, expired, already used, or does not match payload.",
        }

    # Only validated operations are executable, with operation-specific execution path.
    operation = payload["operation"]
    if operation == "create":
        created_id = await odoo.create(payload["model"], payload["values"])
        return {
            "success": True,
            "tool": "odoo_execute_approved_write",
            "operation": operation,
            "model": payload["model"],
            "created_id": created_id,
        }

    if operation == "write":
        ok = await odoo.write(payload["model"], payload["record_ids"], payload["values"])
        return {
            "success": True,
            "tool": "odoo_execute_approved_write",
            "operation": operation,
            "model": payload["model"],
            "record_ids": payload["record_ids"],
            "ok": ok,
        }

    if operation == "call":
        result = await odoo._execute(
            payload["model"],
            payload.get("method", ""),
            *(payload.get("args") or []),
            **(payload.get("kwargs") or {}),
        )
        return {
            "success": True,
            "tool": "odoo_execute_approved_write",
            "operation": operation,
            "model": payload["model"],
            "method": payload.get("method"),
            "result": result,
        }

    return {
        "success": False,
        "tool": "odoo_execute_approved_write",
        "error": f"Unsupported operation in approved payload: {operation}",
    }


# ── Credential Setup Tool (Claude Cowork per-user onboarding) ──


class OdooSetupCredentialsInput(BaseModel):
    """Start a safe onboarding flow or inspect one previously started flow."""

    model_config = ConfigDict(extra="forbid")

    onboarding_id: str | None = Field(
        default=None,
        description=(
            "Opaque ID from a previous call. Omit to start onboarding; provide it "
            "to inspect the non-secret completion status."
        ),
    )


@mcp.tool(
    name="odoo_setup_credentials",
    description=(
        "Begin or inspect credential onboarding outside the MCP conversation. "
        "The returned HTTPS or loopback URL is owned by the configured custody provider; "
        "Odoo API keys must be entered there and never in tool arguments or form elicitation."
    ),
    annotations={"title": "Set Up Odoo Credentials", "readOnlyHint": False, "destructiveHint": False},
)
async def odoo_setup_credentials(
    input: OdooSetupCredentialsInput,
    ctx: Context,
) -> dict[str, str | None]:
    """Start or inspect provider-owned onboarding without handling credentials."""
    context: AppContext = ctx.request_context.lifespan_context
    try:
        principal = await context.principal_provider.resolve()
        if input.onboarding_id is not None:
            result = await context.onboarding_provider.get_result(
                principal,
                input.onboarding_id,
            )
            return result.to_public_dict()

        continuation = await context.onboarding_provider.begin(principal)
        return continuation.to_public_dict()
    except PrincipalUnavailableError:
        return {"status": "unavailable", "failure_code": "identity_unavailable"}
    except OnboardingUnavailableError:
        return {"status": "unavailable", "failure_code": "onboarding_unavailable"}


class OdooSwitchDatabaseInput(BaseModel):
    """Input schema for switching the active database."""

    model_config = ConfigDict(extra="forbid")

    db: str = Field(..., description="Database name to switch to (must already be stored)")


@mcp.tool(
    name="odoo_list_databases",
    description="List all Odoo databases that have stored credentials, and show which is currently active.",
    annotations={"title": "List Odoo Databases", "readOnlyHint": True, "destructiveHint": False},
)
async def odoo_list_databases() -> dict[str, Any]:
    """Tool: list stored Odoo databases and the current default."""
    databases = list_databases()
    default_db = get_default_db()
    return {
        "databases": databases,
        "default_db": default_db,
        "count": len(databases),
    }


@mcp.tool(
    name="odoo_switch_database",
    description=(
        "Switch the active Odoo database. The selected database must already have stored credentials "
        "(use odoo_list_databases to see available options, odoo_setup_credentials to add a new one)."
    ),
    annotations={"title": "Switch Odoo Database", "readOnlyHint": False, "destructiveHint": False},
)
async def odoo_switch_database(input: OdooSwitchDatabaseInput, ctx: Context | None = None) -> str:
    """Tool: change the default database and reconnect the active session."""
    try:
        set_default_database(input.db)
    except RuntimeError as exc:
        return f"Error: {exc}"

    if ctx is not None:
        from src.core.credentials import get_odoo_credentials
        try:
            new_creds = get_odoo_credentials(input.db)
        except RuntimeError as exc:
            return f"Default updated but failed to load credentials: {exc}"

        new_client = OdooClient(credentials=new_creds, transport=transport_from_env())
        try:
            await new_client.authenticate()
        except Exception as exc:
            await new_client.close()
            return f"Switched default to '{input.db}' but authentication failed: {exc}"

        context: AppContext = ctx.request_context.lifespan_context
        previous_client = context.odoo
        context.odoo = new_client
        context.auth_error = None
        context.session_db = input.db
        if previous_client is not None and previous_client is not new_client:
            await previous_client.close()

    all_dbs = list_databases()
    return (
        f"Switched active database to '{input.db}'. "
        f"Stored databases: {', '.join(all_dbs)}."
    )


@mcp.tool(
    name="odoo_enable_session_writes",
    description=(
        "Deprecated compatibility alias. MCP protocol sessions are not an authority boundary, "
        "so this tool never enables writes. Use the server policy gate and exact approval flow."
    ),
    annotations={"title": "Enable Session Writes", "readOnlyHint": False, "destructiveHint": False},
)
async def odoo_enable_session_writes() -> str:
    """Compatibility tool that deliberately cannot create session authority."""
    return (
        "Session write enablement was removed in MCP 2. Protocol sessions cannot grant write authority. "
        "Use the administrator-controlled server policy gate and exact durable approval flow."
    )


@mcp.tool(
    name="odoo_disable_session_writes",
    description=(
        "Deprecated compatibility alias. MCP 2 keeps no session write authority, "
        "so this tool performs no state change."
    ),
    annotations={"title": "Disable Session Writes", "readOnlyHint": False, "destructiveHint": False},
)
async def odoo_disable_session_writes() -> str:
    """Compatibility tool for clients that still call the removed session API."""
    return "No session write authority exists in MCP 2; no state change was necessary."


@mcp.tool(
    name="odoo_runtime_info",
    description="Show runtime diagnostics: package version, server module path, and credential file status.",
    annotations={"title": "Odoo Runtime Diagnostics", "readOnlyHint": True, "destructiveHint": False},
)
async def odoo_runtime_info(ctx: Context) -> dict[str, Any]:
    """Tool: report bounded runtime state without inspecting credential material."""
    context: AppContext = ctx.request_context.lifespan_context
    try:
        package_version = metadata.version("odoo-mcp-server")
    except Exception:
        package_version = "unknown"

    configured_transport = os.environ.get("ODOO_TRANSPORT", "xmlrpc").strip().lower() or "xmlrpc"
    compatibility_hints: list[str] = []
    if configured_transport == "json2":
        compatibility_hints = [
            "JSON-2 requires Odoo 19+ and a credential lease from the configured custody provider.",
            "JSON-2 support covers core reads, internal note posting, and create/write mutations.",
        ]
    else:
        compatibility_hints = [
            "XML-RPC remains the default compatibility transport for Odoo 16-19.",
        ]

    return {
        "package": "odoo-mcp-server",
        "package_version": package_version,
        "server_module": __file__,
        "credential_custody": "provider",
        "credential_onboarding_available": not isinstance(
            context.onboarding_provider,
            UnavailableOnboardingProvider,
        ),
        "odoo_transport": configured_transport,
        "transport_compatibility_hints": compatibility_hints,
        "write_execution_enabled": _truthy_env("ODOO_MCP_ENABLE_WRITES"),
        "writes_currently_allowed": _truthy_env("ODOO_MCP_ENABLE_WRITES"),
        "self_update_enabled": _truthy_env("ODOO_MCP_ENABLE_SELF_UPDATE"),
    }


@mcp.tool(
    name="odoo_check_for_update",
    description=(
        "Check whether a newer odoo-mcp-server build is available from GitHub and "
        "return a suggested upgrade command. Read-only: does not modify the local environment."
    ),
    annotations={"title": "Check MCP Update", "readOnlyHint": True, "destructiveHint": False},
)
async def odoo_check_for_update(input: OdooCheckForUpdateInput) -> dict[str, Any]:
    """Tool: MCP-native update check for assistant-driven maintenance guidance."""
    repo = (input.repo or "").strip() or default_repo()

    # Network check is offloaded to a worker thread so the MCP event loop remains responsive.
    result = await asyncio.to_thread(check_for_update, repo, input.timeout_seconds)
    suggested_ref = result["latest_version"]
    suggested_command = build_upgrade_command(repo=repo, ref=str(suggested_ref))

    return {
        "ok": True,
        "tool": "odoo_check_for_update",
        "repo": result["repo"],
        "local_version": result["local_version"],
        "latest_version": result["latest_version"],
        "source": result["source"],
        "update_available": result["update_available"],
        "release_url": result["latest_url"],
        "self_update_enabled": _truthy_env("ODOO_MCP_ENABLE_SELF_UPDATE"),
        "suggested_upgrade_command": suggested_command,
        "next": (
            "If update_available is true and user approves, run odoo_apply_self_update "
            "with confirm=true."
        ),
    }


@mcp.tool(
    name="odoo_apply_self_update",
    description=(
        "Run a local self-update for odoo-mcp-server using pip + git ref. "
        "Requires explicit confirm=true and ODOO_MCP_ENABLE_SELF_UPDATE=1."
    ),
    annotations={"title": "Apply MCP Update", "readOnlyHint": False, "destructiveHint": True},
)
async def odoo_apply_self_update(input: OdooApplySelfUpdateInput) -> dict[str, Any]:
    """Tool: guarded local self-update execution for MCP runtime."""
    if not input.confirm:
        return {
            "ok": False,
            "tool": "odoo_apply_self_update",
            "error": "confirm must be true.",
        }

    # Separate gate from Odoo write execution; local package updates are sensitive too.
    if not _truthy_env("ODOO_MCP_ENABLE_SELF_UPDATE"):
        return {
            "ok": False,
            "tool": "odoo_apply_self_update",
            "error": "Self-update is disabled. Set ODOO_MCP_ENABLE_SELF_UPDATE=1.",
        }

    repo = (input.repo or "").strip() or default_repo()
    ref = (input.ref or "").strip()
    if not ref:
        latest = await asyncio.to_thread(fetch_latest_release_or_tag, repo, 15)
        ref = str(latest["version"])

    result = await asyncio.to_thread(
        apply_self_update,
        repo,
        ref,
        input.timeout_seconds,
    )

    return {
        "ok": result["ok"],
        "tool": "odoo_apply_self_update",
        "repo": repo,
        "ref": ref,
        "command": result["command"],
        "returncode": result["returncode"],
        "stdout_tail": result["stdout_tail"],
        "stderr_tail": result["stderr_tail"],
        "restart_required": bool(result["ok"]),
        "next": (
            "Restart the MCP host/client process to load the updated package." if result["ok"]
            else "Review stderr_tail and retry with a different ref or environment permissions."
        ),
    }


# ── Domain tool registration ──
from src.mcp.odoo.tools import sales, projects  # noqa: E402

sales.register(mcp, _require_odoo)
projects.register(mcp, _require_odoo)


def main() -> None:
    """Console script entry point — called by odoo-mcp-server command."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

# Odoo MCP Server — Operational Concepts

This package exposes Odoo tools, resources, and prompts through the official
MCP Python SDK 2 `MCPServer`. The local composition uses stdio. A hosted
Cowork/OpenCrane composition must provide a separately qualified remote
Streamable HTTP endpoint, verified principal adapter, and credential custody.

## Authority boundaries

- Tool arguments never carry Odoo API keys. Credential onboarding happens in
  an out-of-band provider flow or the local `odoo-mcp-onboard` terminal fallback.
- A verified principal owns one or more Odoo profiles. Profile metadata is
  non-secret; credential material stays behind `CredentialProvider`.
- Odoo 18 and below uses XML-RPC. Odoo 19 and above uses JSON-2. A profile-bound
  client pins one transport and one credential version for its lifetime.
- Model-ID caches belong to one client instance and are never shared between
  principals or databases.
- Missing identity, profile, custody, onboarding, or approval adapters fail
  closed. They never fall back to another user, group, or process credential.

## Write governance

Writes remain disabled by default. The generic write path is:

1. `odoo_preview_write` creates the canonical payload.
2. `odoo_validate_write` applies `SafetyGuard`, checks live schema where
   applicable, and persists a short-lived approval bound to principal, profile,
   credential version, and exact payload.
3. The client presents that exact operation for explicit user approval.
4. `odoo_execute_approved_write` requires the token, exact payload, confirmation,
   and runtime write gate. The approval repository atomically reserves the token
   before Odoo I/O, preventing replay across processes.

`SafetyGuard` always blocks deletes and outbound email, SMS, follower, and
calendar-invite behavior. Client permission modes are additional safeguards;
they are not server authority.

## Composition

Domain modules register tools on the shared SDK server:

```python
from mcp.server.mcpserver import Context, MCPServer


def register(mcp: MCPServer, get_odoo: object) -> None:
    @mcp.tool(name="odoo_example")
    async def example(ctx: Context) -> dict[str, object] | str:
        odoo = await get_odoo(ctx)
        if isinstance(odoo, str):
            return odoo
        return {"database": odoo.db}
```

Reusable identity, profile, custody, onboarding, approval, and client-factory
contracts live under `src/core/`. `server.py` is the current local composition
and compatibility surface while remaining session-era database selection is
migrated to per-request profile resolution.

## Validation

Run the safety suite and full test suite before release:

```bash
pytest src/mcp/odoo/tests/test_safety.py -q
pytest -q
```

The SDK protocol smoke tests cover both modern `2026-07-28` discovery and the
legacy handshake mode. Packaging also verifies generated client descriptors and
excludes tests, caches, bytecode, credentials, and local paths from artifacts.

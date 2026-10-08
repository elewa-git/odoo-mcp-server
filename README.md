# Odoo MCP Server

A standalone **Model Context Protocol (MCP) server** for Odoo, providing Claude with safe, human-approved access to Odoo CRM, projects, and other business domains.

## Overview

This MCP server exposes Odoo as a set of modular, domain-specific tools that Claude can safely call:

- **Sales & Pre-Sales** — CRM pipeline, leads, contacts, solution design
- **Project Delivery** — Fixed-scope projects, tasks, milestones  
- **Domain-extensible** — Easy to add support for other Odoo apps (Support, Finance, Knowledge, ERP Implementation, etc.)

All write operations are **safety-checked** to prevent accidental outbound communication (emails, SMS, calendar invites). Internal-only notes, activities, and field updates are safe by design.

## Disclaimer

- This project is an independent, community-maintained MCP server and is **not affiliated with, endorsed by, or sponsored by Odoo S.A.**
- **Odoo** is a trademark of Odoo S.A. Any product names, logos, and brands are used for identification only.
- You are responsible for validating security, compliance, and access controls before using this server in production environments.
- Always test write-enabled workflows in a non-production environment first.

## Key Features

- ✅ **Safety-first**: Outbound communication blocker prevents emails, SMS, and invites
- ✅ **Human-in-the-loop**: Write operations require explicit staged approval and runtime gates
- ✅ **Multi-company**: Supports Odoo's multi-company architecture
- ✅ **Per-user credential boundary**: Odoo credentials are selected for one user/profile and kept out of prompts and tool arguments
- ✅ **Version-aware Odoo transport**: XML-RPC for Odoo 18 and below; JSON-2 for Odoo 19 and above
- ✅ **Fully typed**: Pydantic validation on all tool inputs
- ✅ **Easy deployment**: Single `pip install` entry point

## Installation

### Via pip

```bash
pip install odoo-mcp-server
```

### Via uv (fast Python installer)

```bash
uvx --from https://github.com/elewa-git/odoo-mcp-server odoo-mcp-server
```

## Setup

Setup is centralized in [setup.md](setup.md).

The primary organisation path is Claude Cowork through a reachable, OpenCrane-managed remote MCP connector with per-user OAuth and per-user Odoo credential custody. The secondary path is local stdio, which remains local-first and needs no hosted relay. A plugin repository is not remote hosting.

See [setup.md](setup.md) for the current setup, onboarding, transport, rotation, and fail-closed availability rules.

## Hosted MCP Server Warning

Do not expose a shared MCP process that can select or read arbitrary users' Odoo tokens. A remote deployment is acceptable only when OpenCrane authenticates each user, binds profile selection and credential release to that identity, and fails closed when the adapter or qualification is unavailable.

## Tools

### Generic Odoo Tools

- `odoo_ping` — Validate connectivity and show your profile
- `odoo_search_read` — Find records with flexible filters
- `odoo_read_records` — Read specific records by ID
- `odoo_log_internal_note` — Add internal chatter notes directly (safe)
- `odoo_schedule_activity` — Build staged payload for an internal reminder activity
- `odoo_fields_get` — Introspect model field definitions
- `odoo_search_count` — Count records matching a filter
- `odoo_diagnose_call` — Diagnose planned model calls without executing them
- `odoo_preview_write` — Build a canonical non-executing mutation payload
- `odoo_validate_write` — Validate payload against SafetyGuard and live metadata (when applicable)
- `odoo_execute_approved_write` — Execute only with token + confirm + env gate
- `odoo_setup_credentials` — Start or inspect secure credential onboarding
- `odoo_check_for_update` — Check latest GitHub release/tag and suggest update command
- `odoo_apply_self_update` — Apply local package update (guarded by explicit confirm + env gate)

## Write Approval Flow

All mutating operations follow a strict staged approval flow, except internal notes.
Internal notes are the only direct mutation endpoint.

1. `odoo_preview_write`
  - Builds a canonical draft payload (`model`, `operation`, `record_ids`, `values`, `method`, `args`, `kwargs`)
  - Does not validate against live Odoo metadata
  - Does not execute anything
2. `odoo_validate_write`
  - Applies `SafetyGuard` policy checks
  - Validates payload fields against live `fields_get` metadata for `create`/`write`
  - Issues a short-lived, single-use `approval_token`
3. Explicit user approval
  - The client presents the validated operation for approval before any execution step
  - The validated payload must be accepted by the user as-is
4. `odoo_execute_approved_write`
  - Requires `confirm=true`
  - Requires the exact validated payload alongside the approval token
  - Requires a valid unused token from step 2
  - Requires `ODOO_MCP_ENABLE_WRITES=1`
  - Executes only the validated operation (`create`, `write`, or `call`)

Fail-closed behavior:

- Missing confirmation, invalid/expired token, or disabled runtime write gate blocks execution.
- Tokens are single-use and payload-bound; replay and drift attempts are rejected.

## MCP Self-Update Flow (Optional)

The MCP can suggest and apply its own update locally, but this is gated by default.

1. `odoo_check_for_update`
  - Read-only update check against GitHub releases/tags
  - Returns current version, latest version, and a suggested upgrade command
2. `odoo_apply_self_update`
  - Requires `confirm=true`
  - Requires `ODOO_MCP_ENABLE_SELF_UPDATE=1`
  - Runs a local pip upgrade from the selected git ref/tag

After successful self-update, restart the MCP host/client so the new package is loaded.

### Sales & Pre-Sales Tools

- `odoo_search_opportunities` — Find CRM deals with flexible filters
- `odoo_get_opportunity` — Get full deal context (chatter, activities, all fields)
- `odoo_propose_stage_change` — Build staged payload to move deals to another stage
- `odoo_propose_log_note` — Add internal notes to deals directly (safe)
- `odoo_propose_activity` — Build staged payload for a follow-up activity
- `odoo_propose_field_update` — Build staged payload for allowed field updates
- `odoo_search_contacts` — Find contacts/companies
- `odoo_get_pipeline_summary` — See pipeline health: deal counts, total revenue by stage

### Project Delivery Tools

- `odoo_search_projects` — Find projects by name, client, status, staleness
- `odoo_get_project` — Get full project context (tasks grouped by stage)
- `odoo_search_tasks` — Find overdue, stale, and at-risk tasks
- (Future: task updates, notes, activities — not yet implemented)

### MCP Resources

- `odoo://models/{database}` — List models in an explicitly selected database
- `odoo://model/{model_name}` — Introspect a specific model's fields and metadata

## Safety Guarantees

The **SafetyGuard** in `src/mcp/odoo/utils/safety.py` enforces blocklists defined in `src/mcp/odoo/utils/safety_blocked_methods.py`:

- ❌ All email/SMS sending models: `mail.mail`, `sms.sms`, mailing lists
- ❌ Direct email methods: `action_send_mail`, `action_quotation_send`, `send_mail`, etc.
- ❌ `message_post` with external message types (`comment`, `email`, `notification`)
- ❌ Calendar events with attendees (would send invites)
- ❌ Any method that subscribes followers or changes notification routing

✅ **Safe operations allowed**:
- Field updates on CRM leads, projects, contacts, etc.
- Internal chatter notes using `message_type='note'` (Odoo 18) or `message_type='comment' + subtype_id=2` (Odoo 19)
- Activity creation (internal reminders, never external)
- Record creation and searches (read-only)

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         MCP Client                                  │
│            (ClawdBot, Claude Desktop or Terminal)                   │
└────────────────────────────┬────────────────────────────────────────┘
                             │ stdio
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│                       Odoo MCP Server                              │
│  (MCPServer + Domain Tool Modules + SafetyGuard)                   │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ Generic Tools (search, read, diagnostics, staged writes, etc.)│  │
│  └──────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ Domain Tool Modules                                          │  │
│  │  • sales.py (8 CRM tools)                                    │  │
│  │  • projects.py (6 project tools)                             │  │
│  │  • (others: support, finance, erp, knowledge — future)       │  │
│  └──────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ OdooClient + SafetyGuard                                     │  │
│  │  • XML-RPC (Odoo <=18) or JSON-2 (Odoo >=19)                │  │
│  │  • Profile-bound credential custody                          │  │
│  │  • Safety validation before all writes                       │  │
│  └──────────────────────────────────────────────────────────────┘  │
└────────────────────────────┬───────────────────────────────────────┘
                             │ Version-aware Odoo transport
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          Odoo Instance                              │
│           (https://my.odoo.com or your custom URL)                  │
└─────────────────────────────────────────────────────────────────────┘
```

## Testing

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run safety tests (CRITICAL — always run these)
pytest src/mcp/odoo/tests/test_safety.py -v

# Run all tests
pytest tests/ src/mcp/odoo/tests/ -v
```

## Development

### Project Structure

```
src/
  core/
    credentials.py          ← Per-user credential management
  mcp/
    odoo/
      server.py             ← Main MCP server & generic tools
      utils/
        client.py           ← Async Odoo XML-RPC client
        safety.py           ← SafetyGuard: outbound blocker
      tools/
        sales.py            ← CRM domain tools (8 tools)
        projects.py         ← Project management tools (6 tools)
        _shared.py          ← Shared types (ResponseFormat enum)
      tests/
        test_safety.py      ← Safety validation tests (critical!)
tests/                      ← Integration tests (future)
pyproject.toml              ← Package config
README.md                   ← This file
```

### Adding a New Domain Tool Module

1. Create `src/mcp/odoo/tools/my_domain.py`:
   ```python
   def register(mcp: MCPServer, get_odoo: Any) -> None:
       """Register my domain tools on the shared MCP server."""
       
       @mcp.tool(name="odoo_my_tool", ...)
       async def my_tool(input: MyInput, ctx: Context) -> str:
           odoo = get_odoo(ctx)
           if isinstance(odoo, str):
               return odoo
           # Your implementation here
           return result
   ```

2. Import and register in `src/mcp/odoo/server.py`:
   ```python
   from src.mcp.odoo.tools import my_domain
   
   my_domain.register(mcp, _odoo)
   ```

3. Add tests in `src/mcp/odoo/tests/`.

### Safety Validation

All writes go through staged validation and execution gates. **Never bypass this** — these layers prevent accidental outbound communication:

1. **Code layer** (`SafetyGuard`) — blocks dangerous models/methods
2. **Validation layer** (`odoo_validate_write`) — enforces live schema checks and token issuance
3. **Human layer** (approved client interaction) — explicit user approval before execution
4. **Execution gates** (`odoo_execute_approved_write`) — requires `confirm=true`, valid token, and runtime env gate
5. **Odoo layer** — Odoo's own permissions

## Limitations & Roadmap

### Current (v0.1)

- ✅ CRM pipeline (leads, opportunities, contacts)
- ✅ Project delivery (projects, tasks)
- ✅ Generic Odoo tools (search, read, diagnostics, staged write flow)
- ✅ Internal notes & activities (safe only)

### Planned (v0.2+)

- 🔜 Support & Retainers (helpdesk tickets, SLAs)
- 🔜 Finance & Admin (invoices, payments — read-only)
- 🔜 Knowledge & Context (articles, workspaces)
- 🔜 ERP Implementation (hour-based packages, Odoo config)
- 🔜 Orchestration pipelines (daily reports, approval flow, debriefs)

## Troubleshooting

### "Credentials not found"

```
Use the secure local loopback/CLI onboarding flow, or the OpenCrane profile onboarding flow for Cowork.
```

For local stdio, run `odoo-mcp-onboard`; the current transitional fallback writes the validated key to the existing owner-only local file. For Cowork, credentials must remain in the selected user's OpenCrane-backed custody. Do not paste an API key into chat to repair either path.

### "Odoo authentication failed"

Double-check:
- Odoo URL (must include `https://`)
- Database name (case-sensitive)
- Email matches your Odoo login
- API key is valid (generate new one in Odoo Settings > Users > API Keys)

### "SafetyViolation: BLOCKED"

This is intentional! The tool attempted an operation that could send external communication. If you believe this is a false positive, please file an issue. Do NOT disable SafetyGuard.

## Contributing

Contributions welcome! Please:

1. Add tests for new tools (especially safety tests)
2. Update docs and examples
3. Run `pytest src/mcp/odoo/tests/test_safety.py` before submitting PRs
4. Follow Pydantic & type annotation patterns

## License

MIT Open Source

## Support

For questions, issues, or feature requests:
- GitHub Issues: https://github.com/elewa-git/odoo-mcp-server/issues
- Email: jente@elewa.ke

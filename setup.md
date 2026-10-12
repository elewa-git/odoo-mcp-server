# Setup

This is the setup guide for the MCP 2.0 deployment model. The preferred organisation path is a **reachable OpenCrane remote MCP connector** for Claude Cowork. Local stdio remains a fully supported, local-first path and does not require a hosted relay.

## Choose a path

| Path | Use it when | Credential boundary |
| --- | --- | --- |
| Claude Cowork through OpenCrane (primary) | Your organisation needs a managed Cowork connector | OpenCrane OAuth identifies the user; the user's Odoo credential is held and selected only for that exact user/profile. |
| Local stdio (secondary) | You want a personal or offline-friendly setup | The MCP process runs on the user's machine; onboarding uses a loopback or CLI flow, not the model conversation. |

A plugin repository is **not** a remote MCP host. Importing a plugin into Cowork can distribute instructions or configuration, but it does not make this repository reachable from Cowork and must not be represented as remote hosting.

## 1. Claude Cowork through OpenCrane (primary)

### Organisation onboarding

An OpenCrane administrator must first provide a reachable remote MCP endpoint and configure it as the Cowork connector. The connector must:

1. Require per-user OAuth; do not use a shared service identity for users' Odoo access.
2. Resolve a user identity before selecting an Odoo profile or releasing any credential.
3. Keep raw Odoo API keys behind the per-user credential provider; the MCP runtime receives only the credential lease needed for the selected profile.
4. Enforce the existing write approval policy server-side. Cowork sandboxing does not make arbitrary files, prompts, or credentials safe.
5. Restrict network access and audit connector/profile selection according to your organisation's OpenCrane policy.

The current repository does **not** ship a live-qualified OpenCrane adapter or managed remote endpoint. Until those adapters and live qualification are wired, connector setup must fail closed rather than silently falling back to a shared or local credential.

### Cowork connector configuration

After the organisation has deployed and qualified its endpoint, add that endpoint in the Claude Cowork admin connector flow. Use the OpenCrane-issued OAuth configuration for your environment; do not point Cowork at this Git repository or rely on a plugin import as a substitute.

Validate with a non-production Odoo profile first:

1. Sign in to Cowork and complete OpenCrane OAuth.
2. Open the Odoo MCP connector and confirm that it identifies the signed-in user.
3. Complete first-use Odoo onboarding below.
4. Run `odoo_runtime_info` and then `odoo_ping`.
5. Keep writes disabled until the staged write flow is approved in your environment.

### First use: create an Odoo profile

For each user, onboarding collects an Odoo URL, database, login email, Odoo version/transport, and API key through the organisation's secure credential flow. The API key must never be pasted into chat, tool arguments, prompts, or a plugin configuration file.

The profile is bound to the authenticated user and an explicit profile identifier. The server must reject an absent, ambiguous, revoked, or cross-user profile selection. A successful onboarding validates the credential with Odoo before it is activated.

### Day two: profiles, rotation, and revocation

- Create separate profiles for separate databases, companies, or Odoo identities; choose one explicitly for each session or request.
- Rotate an Odoo API key by updating the selected profile through the secure credential flow, then revalidate it before activation.
- Revoke access by disabling the OpenCrane connector entitlement and revoking/deleting the affected profile credential. Existing leases must expire quickly and be rechecked before external Odoo I/O.
- Removing a user from the organisation must prevent new profile selection and credential release. Review audit records and rotate any Odoo key where policy requires it.

## 2. Local stdio (secondary, local-first)

Local stdio runs the server on the user's device. It does not require OpenCrane, a hosted relay, or a central shared Odoo-token store.

### Install

macOS or Linux:

```bash
uv tool install odoo-mcp-server
odoo-mcp-server
```

Windows PowerShell:

```powershell
winget install --id=astral-sh.uv -e
uv tool install odoo-mcp-server
odoo-mcp-server
```

For a source checkout instead:

```bash
uv run odoo-mcp-server
```

Add the generated client snippet from `dist/client-configs/` to your local MCP client configuration. The available snippets are generated artifacts; regenerate them whenever the manifest or configuration generator changes.

### Local credential onboarding

In Claude Desktop (macOS or Windows), ask **"Create an Odoo connection"**.
Claude calls `odoo_setup_credentials` and provides a link to a form on your own
computer. Open that link in your browser and enter the Odoo URL, database name,
login email, major version (defaults to 19), and API key or password. Enter the secret only in the form, never in
chat. The form authenticates before saving and selects XML-RPC for Odoo 18 and
below or JSON-2 for Odoo 19 and above.

After saving, return to Claude and say **"Check the connection"**. Claude polls
`odoo_setup_credentials` with the returned `onboarding_id` and then calls
`odoo_ping`. No terminal setup or Claude restart is required for this flow.
The setup link expires after ten minutes and only works on the computer running
the extension. If it has expired, ask Claude to create a new connection.

If the page shows **Request rejected**, reopen the link and submit from that
page. Ask Claude to call `odoo_setup_credentials` with `restart: true` if the
session is stuck; this cancels only pending setup and issues a fresh form.
Polling a cancelled request returns `failure_code: cancelled`.

Use the database **instance URL**, such as `https://your-database.odoo.com` or
your instance's custom domain, not the Odoo.sh dashboard/backups URL at
`www.odoo.sh/project/...`. An Odoo.sh database name and instance URL are distinct.
Rejected URLs return `invalid_url`; authentication failures return
`authentication_failed`; local save failures return `storage_failed`.
These codes also appear on the page without echoing entered secrets or Odoo
response bodies. Re-run setup after correcting the reported problem.

Credentials are stored locally at `Path.home() / ".config/odoo-mcp/credentials.json"`
(Windows: `%USERPROFILE%\.config\odoo-mcp\credentials.json`; macOS:
`~/.config/odoo-mcp/credentials.json`). `credential_custody: provider` describes
the onboarding adapter interface, not hosted storage. Local stdio now also
reports `credential_storage: local_file`. It does not upload credentials to a
relay. Existing database entries are preserved.

Repeat the flow to add another database; previously saved databases are retained.
Ask Claude to list connections with `odoo_list_databases` and select one with
`odoo_switch_database`. Each browser-created database retains its validated
transport, so an Odoo 13 and Odoo 19 database can coexist. The most recently
saved/explicitly selected database is the durable default. Browser onboarding
does not enable writes or self-update. The **Configure** extension button controls
write and self-update policy settings; transport is configured for each connection
in the browser form, not in the extension's global settings.

If browser setup is unavailable, run the terminal fallback in the same
operating-system user account as the MCP server:

```bash
odoo-mcp-onboard
```

For a Claude MCPB installation, use the installed bundle's code instead of
installing a potentially different package from PyPI:

Windows PowerShell:

```powershell
uv run --directory "$env:APPDATA\Claude\Claude Extensions\local.mcpb.elewa.odoo-mcp-server" python -m src.core.local_onboarding
```

macOS Terminal:

```bash
uv run --directory "$HOME/Library/Application Support/Claude/Claude Extensions/local.mcpb.elewa.odoo-mcp-server" python -m src.core.local_onboarding
```

Run as the same OS user as Claude; the fallback writes to the same local
credential file. If `uv` is not on PATH, use its executable path shown in the
extension's server settings. Restart the extension after terminal setup, then
ask Claude to run `odoo_ping`.

On Windows PowerShell, the command is the same after `uv tool install`. Normal profile metadata is entered as terminal text; the API key is read with hidden `getpass` input, authenticated, and only then written to the existing owner-only legacy credentials file. This file adapter is transitional while OS credential-store support is implemented.

The MCP `odoo_setup_credentials` tool itself never accepts the API key. Local
stdio uses the loopback browser adapter. Hosted adapters must provide their own
principal-bound provider; they must not reuse a local account's browser flow.

### Select the Odoo transport

- Odoo 18 and below: `xmlrpc`
- Odoo 19 and above: `json2`

Transport belongs to each saved connection. In the connection form or terminal
setup, enter **13** for the older database and **19** for the newer database.
Both flows validate and save the selected transport. If the version or saved
transport is unspecified, Odoo 19 / `json2` is the default. Inherited
`ODOO_TRANSPORT` values no longer override database connections.

For Odoo 13, enter the account's local Odoo password in the form; API keys were
introduced in Odoo 14. For Odoo Online 13, a local password may need to be set
on that database user separately from the Odoo Online account login.
See [Odoo 13 authentication](https://www.odoo.com/documentation/13.0/developer/api/odoo.html).

Existing entries that already contain a transport keep it. For an older entry
without a saved transport, run connection setup again with the correct Odoo
version to validate and save it. This is necessary for pre-19 connections that
previously depended on the extension's global selector.

Generated client templates retain the configurable write/self-update policy
references. Clients without `user_config` interpolation should replace those
references with their chosen values (`false` unless explicitly enabled).

`json2` requires an Odoo API key and has its own supported-method boundaries. Select the transport as part of the profile; do not rely on a global transport setting when multiple profiles are in use.

### Verify safely

1. Confirm the selected profile and transport with `odoo_runtime_info`.
2. Run `odoo_ping` against a non-production database where possible.
3. Exercise read-only tools before considering write access.
4. Enable writes only through the explicit, staged approval policy; a client UI or sandbox is not an approval boundary on its own.

## Plugin repositories

You may fork this repository to review or distribute plugin metadata, but the repository remains source and packaging material. It is not a network service, OAuth issuer, credential vault, or remote connector. Pair it with a separately deployed and qualified OpenCrane remote connector for Cowork, or use the local stdio path.

## Updates and troubleshooting

### Match the SDK to the installed server

Version 2.0.0 in this checkout intentionally uses MCP SDK 2 (`mcp>=2.0,<3`)
and imports `MCPServer`. Older servers that import `mcp.server.fastmcp` require
SDK 1. Installing SDK 2 into those older environments produces
`ModuleNotFoundError: No module named 'mcp.server.fastmcp'` before Odoo
authentication can start. Changing the API key cannot fix that import failure.

To repair an existing isolated **SDK 1 server** environment without upgrading
the server, use the environment's Python executable. SDK 1.30.0 has been
verified with the older local Elewa installation; this is not a claim that
every SDK 1 release supports its APIs.

macOS, for the existing isolated Elewa runtime:

```bash
uv pip install --python "$HOME/.local/share/elewa-odoo-mcp/runtime/bin/python" 'mcp==1.30.0'
uv pip check --python "$HOME/.local/share/elewa-odoo-mcp/runtime/bin/python"
"$HOME/.local/share/elewa-odoo-mcp/runtime/bin/python" -I "$HOME/.local/share/elewa-odoo-mcp/verify_elewa.py"
```

Windows PowerShell, from the directory containing an older server's `.venv`:

```powershell
uv pip install --python .\.venv\Scripts\python.exe 'mcp==1.30.0'
uv pip check --python .\.venv\Scripts\python.exe
.\.venv\Scripts\python.exe -c "from mcp.server.fastmcp import FastMCP; from src.mcp.odoo.server import mcp"
```

These commands repair dependencies only. An import check does not prove a
successful MCP handshake or that the client loaded the connection. Verify
initialization, tool discovery, and a read-only diagnostic call before changing
client configuration. Keep existing client allowlists and write policy gates.
For this checkout's SDK 2 release, install the built package so its own dependency
constraint is applied; do not apply the SDK 1 repair commands to version 2.0.0.

The external Elewa verifier at `~/.local/share/elewa-odoo-mcp/verify_elewa.py`
is not a repository installer. Its diagnostics were repaired separately with a
backup: dependency/import validation, Odoo authentication, per-model read checks,
MCP initialization, discovery, and read-only ping each have a separate stage.
`src/mcp/odoo/utils/startup_diagnostics.py` contains the tested formatter used by
that repair. It unwraps nested failures and extracts missing-module names from
captured subprocess stderr, while withholding arbitrary exception messages,
traceback source lines, and response bodies. Verification output goes to stderr;
server stdout remains reserved for MCP protocol messages.

- Prefer versioned release assets over the rolling `latest-main` prerelease for regular users.
- After a local update, restart the local MCP client so it loads the new process.
- If `odoo_ping` fails, verify the selected profile's URL, database, login email, API-key status, and transport version rule.
- If remote Cowork setup is unavailable, ask the OpenCrane administrator to qualify the connector; do not substitute a shared API key or a repository import.

## Security reminder

- Never put raw Odoo API keys in conversations, prompts, plugin manifests, or shared configuration files.
- Keep profiles, OAuth identity, and credential release bound to one user at a time.
- A missing adapter, entitlement, profile, or live qualification is an unavailable state, not permission to bypass the boundary.

### Release version fields

For a product release, update `version` together in `manifest.json`,
`pyproject.toml`, `.claude-plugin/plugin.json`, and the project's `uv.lock` entry.
Keep `manifest_version` at `0.4`: it identifies the MCPB schema, not the product
release. The packaging workflow uses the qualified MCPB CLI 2.1.2.

Before tagging a release, run:

```bash
mcpb validate manifest.json
mcpb pack
uv lock --check
```

A `v2.0.4` tag triggers the versioned GitHub release workflow after review/merge.

# MCP 2.0 Rewrite and Onboarding Execution Plan

## Objective

Rewrite the server onto the official Model Context Protocol Python SDK 2.x and
the MCP `2026-07-28` protocol while preserving the existing Odoo safety model,
Odoo-version transport boundary, and local-first operation.

The primary hosted journey is Claude Cowork through an OpenCrane-owned remote
MCP endpoint. Local stdio remains a supported secondary journey. Both use the
same principal, connection-profile, credential-provider, and approval contracts.

## Non-negotiable decisions

- Use the official `modelcontextprotocol/python-sdk` 2.x `MCPServer`, not the
  unrelated third-party FastMCP framework.
- Odoo 18 and below uses XML-RPC. Odoo 19 and above uses JSON-2.
- Never pass an Odoo API key through a tool argument, MCP form elicitation,
  conversation, model context, result, `_meta`, MRTR `requestState`, log, trace,
  or exception.
- Keep downstream Odoo credentials separate from the OAuth token that
  authenticates a Cowork/OpenCrane user to the MCP endpoint.
- Bind every profile, credential version, and approval to a verified principal.
  Never fall back from personal to group, organization, or platform credentials.
- Keep writes disabled by default and require server-enforced, exact-payload
  approval. Client permission modes are additional safeguards, not authority.
- Keep applications as thin composition roots. Reusable identity, custody,
  connection, safety, and approval behavior belongs in focused modules.
- Ship frequent, validated commits on one feature branch and one pull request.

## Current baseline and known gaps

- The repository exposes 33 tools, two resources, and three prompts.
- The server starts only over stdio and has no remote HTTP/OAuth composition.
- `mcp>=1.0.0` is unbounded and can now resolve the breaking SDK 2.x release.
- `AppContext` holds one mutable Odoo client, database choice, and session-write
  flag even though MCP `2026-07-28` has no protocol session authority.
- Approval records and validated payloads are process-global in-memory maps.
- Credentials are raw API keys in a home-directory JSON file keyed by database
  name. Process-wide `ODOO_API_KEY` and `ODOO_TRANSPORT` overrides cannot model
  per-user, multi-profile custody.
- The current form-elicitation work has the right intent but still sends the API
  key through the MCP client. It must be replaced by out-of-band onboarding.
- `OdooClient._model_id_cache` is shared across principals and databases.
- Cowork documentation incorrectly presents a plugin repository as the hosting
  mechanism. Cowork connectors require a reachable remote MCP endpoint.
- Package, manifest, and plugin versions and licenses are inconsistent.
- The release workflow packages artifacts without first enforcing install,
  test, lint, generated-config, or protocol smoke gates.
- The untracked `.mcpb` is a generated stale artifact and must not be committed.

## Product topology

### Primary: Claude Cowork through OpenCrane

```text
Claude Cowork
  -> per-user OAuth to OpenCrane
  -> OpenCrane remote MCP endpoint
  -> entitlement and exact Odoo profile selection
  -> OpenCrane/Obot credential custody
  -> Odoo XML-RPC or JSON-2
```

OpenCrane owns public HTTPS, OAuth/OIDC, tenant and user mapping, entitlement,
profile persistence, Obot custody, credential rotation and revocation, exact run
admission, audit receipts, and account-management UI.

This repository owns the MCP tool contract, transport enforcement, Odoo client
and safety behavior, portable principal/profile/credential ports, local adapters,
legacy credential migration, and a remote composition seam that accepts only a
verified principal plus a brokered credential capability.

### Secondary: local stdio

```text
Claude Desktop or another local host
  -> stdio process
  -> local installation principal
  -> loopback onboarding page or getpass CLI
  -> OS credential store
  -> Odoo XML-RPC or JSON-2
```

Local mode must not require OpenCrane or any hosted relay.

## Onboarding lifecycle

### Organization setup

1. Deploy the remote MCP endpoint behind public HTTPS.
2. Configure OpenCrane/OIDC as the MCP authorization server and audience.
3. Register the endpoint as a Claude custom connector.
4. Publish the Odoo integration in the OpenCrane MCP catalogue.
5. Assign organization, group, and user visibility independently from
   credential ownership.
6. Verify OAuth, custody, DNS/TLS, readiness, and a read-only Odoo call.

### User setup

1. The user connects Claude to OpenCrane through OAuth. This creates only the
   trusted Cowork-to-OpenCrane principal mapping.
2. If no Odoo profile is ready, the first Odoo call returns an out-of-band,
   short-lived, single-use onboarding continuation.
3. The browser submits Odoo URL, database, username, API key, profile label, and
   optional company selection directly to the custody plane.
4. The backend normalizes the URL, authenticates, detects Odoo major version,
   enforces the matching transport, resolves accessible companies, and stores
   the secret in custody.
5. Only non-secret profile metadata and an opaque custody reference are stored
   in product state.
6. The interrupted MCP call resumes and reports the exact profile, database,
   Odoo identity, company, version, transport, and read/write policy in use.

### Day-two account management

- List profiles and connection health.
- Set a durable per-principal default profile.
- Add another database without colliding on database name.
- Rotate credentials by creating and validating a new immutable version before
  making it active.
- Revoke a profile or credential immediately and fail admitted work closed.
- Show bounded audit metadata without ever returning custody material.

## Target contracts

```python
@dataclass(frozen=True)
class Principal:
    subject: str
    issuer: str | None
    kind: Literal["local", "remote"]


@dataclass(frozen=True)
class OdooProfile:
    id: str
    principal_id: str
    label: str
    canonical_url: str
    database: str
    username: str
    company_id: int | None
    odoo_major: int
    transport: Literal["xmlrpc", "json2"]
    credential_version: int
    state: Literal["pending", "ready", "revoked", "failed"]
```

Required ports:

- `PrincipalProvider`: derives identity from trusted local process context or a
  validated remote OAuth request, never a tool parameter.
- `ProfileRepository`: performs exact principal-plus-profile lookup and stores
  durable per-principal defaults.
- `CredentialProvider`: resolves a short-lived credential lease or brokered
  invocation capability without serializing it.
- `OdooClientFactory`: creates or pools clients by principal, profile, and
  credential version.
- `ApprovalRepository`: atomically reserves and consumes exact, expiring,
  single-use approvals.
- `OnboardingProvider`: produces safe out-of-band onboarding continuations and
  reports non-secret completion state.

## Write authority

```text
preview
  -> live schema and safety validation
  -> approval record bound to principal + profile + credential version + payload hash
  -> MCP 2 Resolve/Elicit exact-action confirmation
  -> atomic reservation
  -> revalidate identity, entitlement, revocation, payload, and SafetyGuard
  -> Odoo call
  -> success, failed, or unknown audit receipt
```

- Remove `session_writes` and the session enable/disable tools.
- Make all mutations, including internal notes and activities, use the same
  approval authority.
- Mark a timeout after possible downstream submission as `unknown`; never retry
  it automatically.
- Freeze the exact credential version at validation/admission and revalidate it
  immediately before external I/O.

## Execution phases and commit strategy

### Phase 0: Stabilize and capture the contract

- [x] Commit this execution plan as the first branch checkpoint.
- [x] Add a temporary v1 constraint `mcp>=1.28,<2` so the current application
  cannot accidentally install SDK 2 before the migration slice is ready.
- [x] Capture tool, resource, prompt, schema, safety, and transport behavior in
  contract tests.
- [x] Establish a reproducible development environment and current test result.
- [x] Resolve the in-flight form-elicitation change by preserving its tests but
  replacing the secret-input boundary in the onboarding phase.

Exit gate: the final v1-compatible baseline installs and its contract tests pass.

### Phase 1: Introduce portable identity and profile authorities

- [x] Add `Principal`, `OdooProfile`, and immutable credential-version types.
- [x] Add principal, profile, credential, onboarding, approval, and client-factory
  protocols with unavailable/fail-closed adapters.
- [ ] Move transport selection from process environment to validated profile.
- [x] Remove `ODOO_API_KEY` as a runtime override.
- [x] Make caches per client/profile/credential version.
- [x] Add local JSON metadata repository without moving secrets yet.

Exit gate: two principals with identical profile labels and database names cannot
read, select, cache, or execute with each other's state.

### Phase 2: Implement secure local onboarding and legacy migration

- [ ] Add a loopback-only onboarding endpoint with strict Host/Origin checks,
  authenticated single-use state, CSRF protection, short expiry, and no secret
  in URLs or logs.
- [x] Add a `getpass` CLI fallback for hosts without URL elicitation.
- [ ] Add an OS credential-store provider and an explicitly selected owner-only
  file fallback for constrained plugin environments.
- [ ] Add an idempotent legacy `credentials.json` migrator that authenticates
  before activation, leaves input untouched on failure, and never silently
  deletes legacy credentials.

Exit gate: API keys do not appear in MCP schemas, traffic fixtures, output,
logging, traces, errors, onboarding state, or profile metadata.

### Phase 3: Port the server shell to MCP SDK 2

- [x] Pin MCP SDK 2.x and add `uv.lock`.
- [x] Rename `FastMCP` imports and types to `MCPServer`.
- [x] Fix static resource registration incompatible with SDK 2.
- [ ] Keep process lifespan for shared infrastructure only; resolve principal,
  profile, credentials, and Odoo client per request.
- [x] Remove direct `ctx.elicit()` flows; legacy local database selection now
  requires an explicit durable default.
- [ ] Add MCP 2 `Resolve`/MRTR flows for non-secret selection and exact
  confirmation.
- [x] Preserve stdio and add in-process SDK 2 contract tests in default and
  legacy compatibility modes.

Exit gate: SDK 2 lists the complete server surface and executes representative
read-only calls over stdio without session authority.

### Phase 4: Rebuild profile selection and write governance

- [x] Remove protocol-session write authority; legacy enable/disable names are
  non-authorizing compatibility aliases.
- [ ] Replace database switching with durable default-profile selection and an
  explicit optional `profile_id` on profile-bound operations.
- [x] Replace in-memory approval maps with atomic local persistence.
- [ ] Bind approval to principal, profile, credential version, canonical payload,
  expiry, and nonce.
- [ ] Add MRTR accept/decline flows and fail closed when client capability is
  absent.
- [ ] Route notes, activities, creates, updates, and approved calls through the
  same execution authority.

Exit gate: replay, expiry, payload mutation, principal/profile mismatch,
credential rotation, declined consent, and ambiguous downstream failure tests pass.

### Phase 5: Add hosted composition seams

- [ ] Add Streamable HTTP composition separately from stdio.
- [ ] Require a verified remote principal before any profile lookup.
- [ ] Define the OpenCrane profile and custody adapter contract; do not duplicate
  OpenCrane OAuth, tenant IAM, or Obot custody inside this repository.
- [ ] Add onboarding status and safe connection-management tools.
- [ ] Keep the hosted adapter fail closed until the live OpenCrane OAuth and
  custody qualification passes.

OpenCrane follow-up requirements:

- Keep `McpServerInstall` as the per-user install/entitlement record.
- Add multiple `McpConnectionProfile` records per install and a durable
  `defaultProfileId`.
- Bind each profile to immutable, revocable custody versions rather than one
  untyped `credentialRef` on the install.
- Freeze the selected profile and credential version at run admission and
  revalidate them at external I/O.

Exit gate: two OAuth principals with the same Odoo metadata remain isolated and
the server stores/returns only opaque custody references.

### Phase 6: Structured output and surface cleanup

- [ ] Replace JSON-string domain results and broad unions with named Pydantic
  result models.
- [ ] Mark profile-bound resources private/user-scoped or convert them to tools.
- [ ] Preserve existing names unless a compatibility alias or documented
  deprecation is necessary.
- [ ] Remove session terminology and stale FastMCP examples.

Exit gate: all 33 tools, two resources, and three prompts are preserved or have
explicit, tested migration notes.

### Phase 7: Cowork/OpenCrane and local packaging

- [x] Rewrite setup documentation around the primary remote Cowork/OpenCrane
  connector and secondary local stdio journey.
- [x] Generate separate local-stdio artifacts and a remote connector descriptor.
- [x] Remove credential environment variables from distributed client configs.
- [x] Align package, manifest, plugin, runtime, and release versions and licenses.
- [x] Exclude bytecode, caches, local state, tests, credentials, and stale source
  from release bundles.
- [x] Regenerate client configs and the MCPB from authoritative inputs; keep the
  binary MCPB as a CI/release artifact rather than source-controlled output.

Exit gate: the documented Cowork connector, OpenCrane integration, Claude
Desktop extension, and manual local configurations match generated artifacts.

### Phase 8: CI, qualification, and release

- [x] Separate test/lint/package gates from publishing and restrict write
  permissions to release jobs.
- [x] Build and clean-install wheel and sdist artifacts.
- [x] Run Ruff, unit tests, generated-output checks, bundle inspection, secret
  scans, SDK protocol smoke tests, and checksum verification.
- [ ] Qualify Odoo 18 XML-RPC and Odoo 19 JSON-2.
- [ ] Qualify Cowork OAuth, first-use onboarding, multiple profiles, read-only
  use, write step-up, exact approval, rotation, and revocation through OpenCrane.
- [ ] Document rollback to the final v1 release.

Exit gate: all automated gates are green and the live Cowork/OpenCrane and local
stdio journeys pass before the major release is published.

## Validation matrix

| Boundary | Required proofs |
| --- | --- |
| Identity | Invalid issuer, audience, expiry, or subject fails before profile lookup; local and remote principals cannot collide. |
| Profiles | Same database name across URLs and principals is isolated; defaults are durable and principal-bound. |
| Credentials | Secrets never serialize; rotation and revocation are immediate; legacy migration is idempotent and non-destructive on failure. |
| Odoo transport | Odoo <=18 cannot use JSON-2; Odoo >=19 cannot use XML-RPC; one client never mixes transports. |
| MCP protocol | SDK 2 default `2026-07-28` and legacy clients list and call the expected surface; no direct back-channel elicitation remains. |
| Writes | Policy, MRTR consent, payload hash, principal, profile, credential version, TTL, and single-use reservation are all required. |
| Packaging | Clean artifacts install on supported Python versions and contain no caches, credentials, local paths, or stale generated files. |
| Hosted journey | Cowork OAuth, OpenCrane entitlement, browser onboarding, custody, resumed read, approval, audit, rotation, and revocation pass live qualification. |

## Execution log

- [x] Created branch `feat/mcp-v2-rewrite` from `main` while preserving existing
  worktree changes.
- [x] Replaced the stale platform-breadth plan with this MCP 2.0 and onboarding
  execution plan.
- [x] Committed the plan checkpoint as `00b9bf8`.
- [x] Added the temporary v1 dependency fence and lockfile as `4401988`.
- [x] Froze the current public names and write annotations as `3f5d868`.
- [x] Added principal/profile/credential contracts as `407cd37`.
- [x] Added the profile-bound Odoo client factory as `c7417ad`.
- [x] Added owner-only non-secret profile persistence as `aa946bb`.
- [x] Established the current baseline: 93 tests pass on Python 3.14.3. Focused
  Ruff checks pass for all new files; the repository has 76 pre-existing Ruff
  findings that remain outside these focused slices.
- [x] Fixed PR merge-ref packaging and separated read-only package jobs from
  write-authorized release publishing as `3f82708`.
- [x] Added secret-free onboarding continuations as `4c0a5b7`.
- [x] Isolated JSON-2 credentials and model caches per client as `65ed96d`.
- [x] Cut the server and domain modules to official MCP SDK 2 as `614c832`.
- [x] Added modern `2026-07-28` and legacy protocol client tests as `97b0de7`.
- [x] Added atomic principal/profile/version/payload-bound SQLite approvals as
  `db79301` and wired them into write execution as `eccc7df`.
- [x] Added the secure local `getpass` onboarding command as `7a75e90`.
- [x] Rewrote Cowork/OpenCrane and local onboarding documentation as `4624a3e`.
- [x] Aligned the v2 package, manifest, plugin, local snippets, and remote
  connector example as `ed6b58a`.
- [x] Added test, lint, generation, wheel/sdist, and artifact inspection gates
  as `873fa9e`.
- [x] Removed protocol-session write authority as `2123393` while preserving
  non-authorizing compatibility tool names.
- [x] Removed the final direct elicitation path in favor of durable explicit
  database selection as `a8fcd8f`.
- [x] Current local result: 116 tests pass after removing the superseded legacy
  in-memory approval implementation and tests; Python error lint, generated-output
  drift, JSON/YAML syntax, wheel/sdist build, artifact-content inspection, and
  diff checks pass.
- [x] Regenerated and validated the MCPB with the official CLI; the manifest
  schema passes and inspection confirms no tests, caches, virtualenvs,
  credentials, local paths, internal agent instructions, or stale approval code.
- [x] Live PR package run `31376295899` passed the locked install, test, lint,
  generation, clean-install, artifact inspection, MCPB validation, archive, and
  upload gates; checksum verification is added to the same required job.

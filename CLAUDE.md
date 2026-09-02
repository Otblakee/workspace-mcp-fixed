# CLAUDE.md

Project guidance for Claude Code working in this repo. See `README.md` for
project overview, setup, transports, and tool tiers. See `FOLLOWUPS.md` for
parked follow-ups not addressed in the current PR.

This is OTB's fork of `taylorwilsdon/google_workspace_mcp` (via
`akilja24/workspace-mcp-fixed`). Hosted on Render. Six services live: drive,
gmail, calendar, docs, sheets, contacts. Single-user (`oliver@otbgroup.co.uk`)
for now, designed for multi-user expansion.

**Hard rule:** never enable `apps_script` in the `TOOLS` env var until audit
logging has been live and reviewed for 30 days.

## Audit logging

Every MCP tool call is logged to a Google Sheet via `core/audit.py`.

**Sheet:** OTB_LOG_MCPAuditLog_2026-05-04_v1
**Sheet ID:** `1bfVQMbU3PgEkjN58fD01dIE2WBTJ9rsdM-3R_yUaTkE`
**Tabs:** One per calendar month (`YYYY-MM`), auto-created on first write.

**Required Render env vars:**
- `AUDIT_SHEET_ID` — Sheet ID of OTB_LOG_MCPAuditLog
- `AUDIT_FLUSH_INTERVAL_S` — default 30
- `AUDIT_BATCH_SIZE` — default 50
- `DEFAULT_USER` — fallback when OAuth identity isn't resolvable; default "oli"

**Auth model:** two modes, selected by whether `AUDIT_SA_JSON_FILE` /
`AUDIT_SA_JSON_B64` is set (see `core/audit.py` docstring).

- *Service-account writer* (multi-user mode, added on
  `claude/multi-account-workspace-groups-2dnyhi`): one dedicated service
  account, shared with the Sheet as its only Editor, appends every row.
  Users need no access to the Sheet, so nobody can read, edit, delete or
  forge another user's rows, and rows attributed to `DEFAULT_USER` are
  written instead of dropped to stdout. Set this before adding a second
  user. Loader shared with the group policy in `core/service_account.py`.
- *Per-user writer* (original single-user mode, still the fallback): rows
  are written with the calling user's own OAuth credentials, so every user
  needs Sheets scope and Editor on the Sheet, and every Editor can read and
  alter every row. Acceptable for one user only. A Render Postgres immutable
  mirror remains the Phase-3 answer for evidence-grade audit.

**Architecture:** decorator + monkey-patch on FastMCP `tool` decorator at server init.
Async queue, buffered flush every 30s. Each row's `user` is captured from
`authenticated_user_email` on the FastMCP request context at submit time;
falls back to `DEFAULT_USER` only when no context is live (local stdio dev,
edge cases). Credentials are resolved per-flush by user email — OAuth 2.1
mode reads from the in-process session store, OAuth 2.0 from the legacy
credential cache. A fresh Sheets client is built per flush so token refresh
flows through google-auth's standard lifecycle. Sensitive params (body,
content, values, notes, subject, etc.) are redacted to
`<redacted:type:length>`. Audit failures never break tool calls.

## Tool changes in this PR (fix/drive-base64-locale-draft-delete-docfmt)

### `create_drive_file` — new `base64_content` parameter
Optional standard-base64 (NOT urlsafe) string for binary uploads (PNG, PDF, …).
Mutually exclusive with `content` and `fileUrl`; if more than one source is
supplied the call rejects up-front. Supply the actual `mime_type` for the
binary; the bytes are wrapped in `MediaIoBaseUpload` and uploaded with
`supportsAllDrives=True` so Shared Drive parents work correctly. Mirrors the
base64 attachment pattern already in `gmail_tools.draft_gmail_message`.

### `create_spreadsheet` — new `locale` parameter
Defaults to `"en_GB"` so OTB-created sheets get GBP / DD-MM-YYYY by default.
Set on `properties.locale` of the create body alongside `properties.title`.
Override with any IETF BCP 47 tag (e.g. `en_US`, `fr_FR`).

### `delete_gmail_draft` — new tool
`delete_gmail_draft(draft_id: str)` calls `users().drafts().delete(userId='me',
id=draft_id)`. Uses `GMAIL_COMPOSE_SCOPE` (same scope as
`draft_gmail_message`). Registered in `core/tool_tiers.yaml` under
`gmail.extended`.

**Render env-var update**: `TOOLS` must contain SERVICE names only (it feeds
`--tools` via the Dockerfile CMD, and argparse restricts that flag to service
names — a tool name like `delete_gmail_draft` in `TOOLS` makes the container
exit 2 in a boot loop). Keep `TOOLS` as e.g.
`gmail drive calendar docs sheets contacts` and set `TOOL_TIER=extended`
(feeds `--tool-tier` via the Dockerfile CMD) so extended-tier tools such as
`delete_gmail_draft` are loaded.

### `create_doc` — new `content_format` parameter (`'plain'` | `'markdown'`)
Default `'plain'` is unchanged: `documents.create({title})` followed by an
optional `batchUpdate insertText` with the raw content (literal characters).

When `content_format='markdown'` and the content is non-empty, `create_doc`
routes through the **Drive API**: uploads the bytes as `text/markdown` with
target mimeType `application/vnd.google-apps.document` and lets Google's
server-side converter render headings, lists, bold/italic, etc. as native Doc
styles. Same upload pattern as `gdrive.drive_tools.import_to_google_doc`. No
client-side markdown parser, no new dependency.

`create_doc` keeps its original `@require_google_service('docs', 'docs_write')`
decorator so the default plain path requires only Docs scope — clients with
Docs-only credentials still work for plain creation. The Drive service is
acquired lazily via a small `_create_doc_drive_service` helper (decorated
with `@require_google_service('drive', 'drive_file')`) that's only called
inside the markdown branch when content is non-empty. The public MCP
signature seen by clients is unchanged.

Markdown subset that should work end-to-end via Google's converter:
`#`/`##`/`###` headings, `-`/`*` bulleted lists, `1.` numbered lists,
`**bold**`, `*italic*`/`_italic_`, blank-line paragraph breaks. Edge content
(empty, whitespace-only, very large) routes through the plain path or empty
fast-path; see `tests/test_mcp_fixes.py`. Document any
Google-converter limitations encountered during live testing in
`FOLLOWUPS.md`.

## Drive `supportsAllDrives` audit (Issue 2)

Audit performed on commit `dcadcb1`, before/after summary below. List endpoints
must additionally carry `includeItemsFromAllDrives=True`. Read media paths via
`files().get_media()` should also carry `supportsAllDrives=True`;
`files().export_media()` does not accept the flag in Drive v3.

| File | Line | Function | Method | sAD before | iIfAD before | sAD after | iIfAD after |
| --- | --- | --- | --- | --- | --- | --- | --- |
| gappsscript/apps_script_tools.py | 45 | `list_script_projects` | `.list(**request_params)` | ❌ | ❌ | ✅ | ✅ |
| gappsscript/apps_script_tools.py | 743 | `_delete_script_project_impl` | `.delete(fileId=…)` | ❌ | n/a | ✅ | n/a |
| gdrive/drive_tools.py | 112 | `search_drive_files` | `.list(**list_params)` | ✅ (helper) | ✅ (helper) | unchanged | unchanged |
| gdrive/drive_tools.py | 174 | `get_drive_file_content` | `.export_media(…)` | n/a | n/a | n/a | n/a |
| gdrive/drive_tools.py | 176 | `get_drive_file_content` | `.get_media(fileId=…)` | ❌ | n/a | ✅ | n/a |
| gdrive/drive_tools.py | 330 | `get_drive_file_download_url` | `.export_media(…)` | n/a | n/a | n/a | n/a |
| gdrive/drive_tools.py | 332 | `get_drive_file_download_url` | `.get_media(fileId=…)` | ❌ | n/a | ✅ | n/a |
| gdrive/drive_tools.py | 460 | `list_drive_items` | `.list(**list_params)` | ✅ (helper) | ✅ (helper) | unchanged | unchanged |
| gdrive/drive_tools.py | 496 | `_create_drive_folder_impl` | `.create(...)` | ✅ | n/a | unchanged | n/a |
| gdrive/drive_tools.py | 660,696,757,793 | `create_drive_file` (4 branches) | `.create(...)` | ✅ | n/a | unchanged | n/a |
| gdrive/drive_tools.py | 807 | `create_drive_file` (base64 branch — new) | `.create(...)` | ✅ | n/a | new ✅ | n/a |
| gdrive/drive_tools.py | 1305 | `import_to_google_doc` | `.create(...)` | ✅ | n/a | unchanged | n/a |
| gdrive/drive_tools.py | 1367 | `get_drive_file_permissions` | `.get(...)` | ✅ | n/a | unchanged | n/a |
| gdrive/drive_tools.py | 1480 | `check_drive_file_public_access` | `.list(**list_params)` | ✅ | ✅ | unchanged | unchanged |
| gdrive/drive_tools.py | 1502 | `check_drive_file_public_access` | `.get(...)` | ✅ | n/a | unchanged | n/a |
| gdrive/drive_tools.py | 1652 | `update_drive_file` | `.update(**query_params)` | ✅ (params) | n/a | unchanged | n/a |
| gdrive/drive_tools.py | 1746 | `get_drive_shareable_link` | `.get(...)` | ✅ | n/a | unchanged | n/a |
| gdrive/drive_tools.py | 2191 | `copy_drive_file` | `.copy(...)` | ✅ | n/a | unchanged | n/a |
| gdrive/drive_tools.py | 2366 | `set_drive_file_permissions` | `.update(...)` | ✅ | n/a | unchanged | n/a |
| gdrive/drive_helpers.py | 253 | `resolve_folder_id` | `.get(...)` | ✅ | n/a | unchanged | n/a |
| gdocs/docs_tools.py | 79,133,247,253,303,381,798,1291,1314,1353 | various | mixed | ✅ | ✅ where applicable | unchanged | unchanged |
| gsheets/sheets_tools.py | 63 | `list_spreadsheets` | `.list(...)` | ✅ | ✅ | unchanged | unchanged |
| gcalendar/calendar_tools.py | 705 | calendar attachment lookup | `.get(...)` | ✅ | n/a | unchanged | n/a |

`export_media` rows are marked n/a because Drive v3's discovery does not
accept `supportsAllDrives` on that endpoint. All other Drive API call sites
in the repo now carry the appropriate flags.

## Render redeploy checklist

After this PR merges:
1. Trigger a Render redeploy.
2. Confirm `delete_gmail_draft` is loaded: keep `TOOLS` to service names only (e.g. `gmail drive calendar docs sheets contacts` — never tool names, which crash-loop the container with argparse exit 2) and set `TOOL_TIER=extended` so the `gmail.extended` tier (which includes `delete_gmail_draft`) is picked up.
3. No new env vars required for these fixes.
4. No new pip dependencies — both Drive and Sheets clients were already pinned in `pyproject.toml`.

## Multi-user security hardening (claude/multi-user-mcp-support-E5ujm)

Five fixes that close cross-user leak surfaces before adding teammates to
the deployment. All have unit coverage in `tests/test_multi_user_security.py`.

### 1. OAuth 2.0 fallback closed in `auth/service_decorator.py`

`_detect_oauth_version` previously returned `False` (meaning "use OAuth
2.0") when `MCP_ENABLE_OAUTH21=true` but the request had no
`authenticated_user_email` and no FastMCP access token. The OAuth 2.0 path
then read `user_google_email` straight out of the caller's kwargs, so any
client that default-filled that field could impersonate any other user
whose creds happened to be cached on the server. This is the most
plausible mechanism for upstream issue #162 (LibreChat cross-user data
access, "can't reproduce" by maintainer).

The function now raises `GoogleAuthenticationError` instead of falling
back. Side effect: any deployment running with `MCP_ENABLE_OAUTH21=true`
that wasn't actually completing the OAuth 2.1 flow will now hard-fail
instead of silently impersonating. Operationally that's the right trade.

### 2. Per-user audit attribution in `core/audit.py`

`_flush` previously built one Sheets client from the *first* user in the
batch whose creds resolved, then wrote every row in the batch via that
client. Effects: Sheets revision history attributed all rows to whoever
came first, that user's quota was burned for everyone, and they had
indirect read access to other users' redacted `params_summary`.

Replaced `_build_sheets_for_batch` with `_build_sheets_for_user`. `_flush`
now groups the batch by `user`, builds a Sheets client per user, and
writes only that user's rows with that user's credentials. Cost: O(distinct
users in batch) Sheets calls per flush. Fine at team scale; revisit if
the team grows past ~20 active users.

### 3. Deep redaction in `core/audit.py`

`_redact` was a one-level walk: `SENSITIVE` keys at the top of `kwargs`
were redacted, but nested fields like `message.body`, `parts[*].content`,
or `data["raw"]` slipped through verbatim. Now `_redact_value` walks
dicts/lists recursively up to `_REDACT_MAX_DEPTH = 6`, applies `SENSITIVE`
matching at every level, and truncates long strings anywhere in the tree.
Non-JSON-serializable leaves render as `<TypeName>` via `json.dumps(default=…)`.

### 4. `_resolve_user_email` warn-on-fallback in `core/audit.py`

The bare `except Exception` previously swallowed every failure and
attributed audit rows to `DEFAULT_USER` ("oli") in silence. Now logs a
WARN when the FastMCP context is present but `authenticated_user_email`
is empty (likely middleware ordering bug) and when `get_state` raises.
The legitimate "no context at all" case (stdio dev, background task)
stays quiet.

### 5. Domain policy in `auth/auth_info_middleware.py`

New `_claims_pass_domain_policy(claims, email)` helper, evaluated at both
auth gates (FastMCP-validated access token and `Authorization: Bearer`
header). Two layers, both opt-in via env:

- `email_verified=False` is always rejected when the claim is present.
  Absence is allowed (Google access tokens commonly omit it).
- `OAUTH_ALLOWED_EMAIL_DOMAINS` (comma-separated) restricts accepted
  identities. Prefers Google's IdP-attested `hd` claim; falls back to the
  email domain literal when `hd` isn't in the claims. Unset → no
  restriction (preserves single-user dev workflow).

**New Render env var to set before adding any second user:**

- `OAUTH_ALLOWED_EMAIL_DOMAINS=otbgroup.co.uk`

Without this, the middleware will accept any verified Google identity
that completes the OAuth flow against your client. The IdP-side fix
(setting the OAuth client to Internal in your Workspace org, if the GCP
project is in-org) is still recommended as the outer ring; this is
defence in depth.

### Not in scope of this PR (parked for follow-up)

- Per-user tool ACL middleware + user registry.
- Programmatic revoke endpoint (Google `oauth2.revoke` + session-store
  invalidation).
- Postgres mirror for the audit log (the per-user attribution above is
  the minimum bar; immutable storage + service-account writer is the
  full Phase-3 story).
- Verifying issue #162 is fully closed by fix #1 — needs a two-account
  reproduction harness against the live Render service.

## Large file support (feature/large-file-support)

The base64 path used by `create_drive_file` and the in-memory bytes
buffer used by `get_drive_file_download_url` cap out around 50–60 MB on
the 512 MB Render instance. Three new Drive tools open a memory-safe
path for arbitrarily large files. Coverage in
`tests/test_large_file_support.py`.

### Tool: `create_drive_upload_session`

Pre-creates an empty placeholder file in Drive to reserve a real
`file_id`, then opens a Google Drive resumable upload session against
that file. Returns the upload URI (sensitive — see audit changes
below), the placeholder `file_id`, and the URI's expiry (Google's docs
say one week).

The caller PUTs file bytes directly to the upload URI. **Bytes never
travel through this server or through MCP tool arguments.** That's the
whole point — the 512 MB Render instance can broker uploads of any
size because it never sees the bytes.

If session-init fails (network error, Google rejection), the
placeholder file is rolled back so we don't litter Drive with empties.

### Tool: `confirm_drive_upload`

Polls Google's resumable session endpoint with `Content-Range: bytes
*/*` per the Drive resumable-upload spec. Three terminal outcomes:

- `complete` (HTTP 200/201) — upload succeeded; tool returns the
  final file metadata fetched via `files.get`.
- `incomplete` (HTTP 308) — partial upload; tool parses the `Range`
  header and returns `bytes_received` so the caller can resume.
- `failed` (HTTP 404/410 or other) — session expired or was rejected;
  tool returns a clean status string with the HTTP code, not a raw
  exception.

### Tool: `download_drive_file`

Streams Drive → local disk in 4 MB chunks via `MediaIoBaseDownload`
writing to a real file handle (not a `BytesIO`). Only one chunk lives
in memory at a time, regardless of file size. The downloaded file is
registered with `core.attachment_storage` so it's served via
`/attachments/{file_id}` (HTTP transport) or returned as an absolute
path (stdio). Files expire from storage after 1 hour. **No
credentials are returned to the caller at any point.**

Native Google docs (Docs / Sheets / Slides) are exported to PDF / CSV
/ PDF respectively — use the older `get_drive_file_download_url` if
you need different export formats.

Chunk size is configurable via `WORKSPACE_DOWNLOAD_CHUNK_BYTES`
(default 4 194 304 = 4 MiB). Lower it on very tight memory; raise it
to reduce HTTP round trips on big files over fast links.

### `core/attachment_storage.py` — two new methods

- `reserve_path(filename)` — returns `(file_id, absolute_path)` inside
  the storage dir without writing anything. Lets streaming downloads
  open the path with `O_CREAT|O_TRUNC|0600` and pass the file handle
  to `MediaIoBaseDownload`.
- `register_existing_file(file_id, file_path, filename, mime_type,
  size)` — registers an already-on-disk file with the metadata table
  so it's served and aged out by the existing `/attachments/{file_id}`
  route + `cleanup_expired` sweep. Pairs with `reserve_path`.

The original `save_attachment(base64_data, ...)` path is unchanged so
existing callers (Gmail attachments, the Chat download tool, the
non-streaming Drive download tool) keep working.

### Audit fixes alongside the new tools

- **`SENSITIVE` adds `upload_uri`.** Resumable upload session URIs are
  short-lived bearer-equivalent tokens — anyone with one in the audit
  window could PUT bytes to the upload. Treat them like credentials.
  (`base64_content`, `fileUrl`, and `attachments` were already in
  SENSITIVE; we keep them.)
- **`_resource_id` skips `None` and `""` values.** `str(None)` was
  emitting the literal string `"None"` into the audit sheet for tools
  that take an optional file_id and got called without one. Both the
  kwargs path and the result-dict path now treat None/empty the same
  as a missing key.
- **Audit error rows now name the original exception class.**
  `handle_http_errors` re-raises Google `HttpError` (and other
  framework errors) as `Exception(message) from cause`. Audit row used
  to capture `type(e).__name__`, which collapsed every Drive failure
  to `"Exception:"`. New helper `_origin_error_type` walks one level
  through `__cause__` when the outer is a bare `Exception`, so audit
  rows now read e.g. `HttpError: ...` instead of `Exception: ...`.
  Subclasses of `Exception` (like `TransientNetworkError`) are kept
  as-is, not unwrapped.

### New env var (optional)

- `WORKSPACE_DOWNLOAD_CHUNK_BYTES` — chunk size in bytes for the
  streaming download path. Default 4194304 (4 MiB). Unset on Render
  → default applies; no action required.

### Operational notes for Render

- Each `download_drive_file` call writes to `WORKSPACE_ATTACHMENT_DIR`
  (default `~/.workspace-mcp/attachments/`) and the file lives there
  for an hour. If the Render instance restarts before the user fetches
  the file, the URL 404s — that's the same behaviour as the existing
  Gmail / Chat attachment flows.
- Resumable upload session URIs are valid for 7 days regardless of
  this server's lifecycle. A redeploy mid-upload doesn't invalidate
  the upload — just the audit-row correlation, which is acceptable.

## Destructive-tool trim (claude/inspiring-pascal-d9twsj)

Gates destructive Google Workspace operations at the MCP source so no
connected client (Claude chat, Cowork, Code, the OTB AI Cockpit) can call
them. The MCP is the one shared control point we own; trimming it protects
every surface at once. This is a companion to the Cockpit's own Drive
allowlist work.

**Important nuance (do not lose it):** gating tools does NOT reduce the OAuth
token's scope. Full Drive scope is intentionally kept (full open spec). A
leaked full-scope token can still delete directly via Google, bypassing this
denylist. Token security (encryption at rest, short-lived access, rotating
refresh, fast revoke, no token in logs) and Google-side controls (Vault
retention, Drive sharing restrictions) remain real controls. The gating is
not, on its own, the whole story.

### Mechanism — hard denylist at the registration chokepoint

`core/tool_policy.py` holds `BLOCKED_TOOLS`, the single source of truth.
Enforcement is in `core/server.py` `_audited_tool`: a blocked tool's
`@server.tool()` decorator becomes a no-op, so the tool is never registered,
never appears in `list_tools`, and can never be called. Fail-closed and
independent of `--tools`, `--tool-tier`, `--read-only`, `tool_tiers.yaml`, or
any env var. The blocked names are also removed from `core/tool_tiers.yaml`
(defense in depth); `tests/test_tool_policy.py` asserts the two stay in sync.

Note: YAML pruning alone is NOT sufficient. The `elif args.tools is not None:`
branch in `main.py` calls `set_enabled_tools(None)`, which disables per-tool
filtering, so without the code denylist every decorated tool in the loaded
services would register. The denylist is the control; the YAML prune is tidy.

### Blocked tools (removed from every surface)

- Drive ownership / access loss: `transfer_drive_ownership`,
  `remove_drive_permission`
- Drive over-share / exfiltration: `share_drive_file`,
  `batch_share_drive_file`, `set_drive_file_permissions`,
  `update_drive_permission` (a public link is a permanent leak, worse than a
  recoverable trash; remove from `BLOCKED_TOOLS` and gate to internal-only if
  the AI must share)
- Calendar: `delete_event`
- Contacts: `delete_contact`, `batch_delete_contacts`, `delete_contact_group`
- Gmail: `delete_gmail_draft`, `delete_gmail_filter`,
  `batch_modify_gmail_message_labels` (bulk trash via the TRASH label)

Deliberately NOT blocked (judgement calls, harden later if needed): the
single-message `modify_gmail_message_labels` (normal archive/label path, can
still apply TRASH to one message), `manage_gmail_label` (can delete labels),
`share_calendar`, `send_gmail_message`, `create_gmail_filter`,
`delete_conditional_formatting`. The content-overwrite tools
(`modify_doc_text`, `find_and_replace_doc`, `batch_update_doc`,
`modify_sheet_values`, `modify_event`, `update_contact`) are kept by design;
Drive/Docs/Sheets version history is the recovery backstop, which is strong
for Docs/Sheets and weaker for Calendar/Contacts.

### Soft-delete replaces trash/delete for Drive

`update_drive_file` no longer accepts a `trashed` parameter (the trash path is
removed). Two new tools replace it:

- `soft_delete_drive_file(file_id, reason=None)` — moves the file into a
  private holding folder (`DRIVE_HOLDING_FOLDER_ID`) and records the original
  parents in `appProperties` (`mcp_orig_parents`, `mcp_deleted_at`,
  `mcp_deleted_by`, `mcp_reason`). Never trashes, never hard-deletes. Fails
  closed if `DRIVE_HOLDING_FOLDER_ID` is unset. Flags files the caller does
  not own (Drive may restrict the move).
- `restore_drive_file(file_id, target_folder_id=None)` — moves the file back
  to its recorded original parents (or `target_folder_id`) and clears the
  soft-delete markers.

Caveat: soft-delete is organizational, not a security boundary. The file
stays fully live and editable; the kept content-overwrite tools can still
blank it in place. Soft-delete only replaces delete/trash.

### New Render env var

- `DRIVE_HOLDING_FOLDER_ID` — Drive folder ID of a private holding folder you
  own and empty manually. Required for `soft_delete_drive_file` /
  `restore_drive_file`; those tools fail closed if it is unset.

### Workspace-side controls (companion, not in this repo)

There is no Workspace toggle that disables delete for a full-scope token. The
levers that actually help: Google Vault retention on Drive (the only control
that survives a token leak; needs Business Plus / Enterprise / Vault add-on);
the 25-day admin restore window for emptied trash; Drive sharing settings to
cap exfiltration; API controls / app access control + an Internal OAuth
client to limit who can use the token; Context-Aware Access to pin source IP
(needs static egress + Enterprise tier).

## Drive architecture + migration tools (claude/otb-drive-mcp-gaps-t5hb6x)

Implements the gap analysis in OTB_IT_DriveMcpToolGaps_2026-08-12_v1, which
in turn implements OTB_IT_TargetDriveArchitecture_2026-07-31_v1.xlsx. Fifteen
new tools across three new modules, plus shared plumbing.

**New files**
- `gdrive/drive_batch.py` — retry/backoff, pagination, the groups-only
  permission guardrail, manifest parsing, JSONL report writing. No tools; all
  unit-testable without touching FastMCP.
- `gdrive/shared_drive_tools.py` — P1 architecture-build tools.
- `gdrive/drive_migration_tools.py` — P2 migration engine + P3 batch helpers.
- `gadmin/admin_group_tools.py` — the opt-in Admin SDK group-write service.

Tests: `tests/gdrive/test_drive_batch.py`,
`tests/gdrive/test_shared_drive_tools.py`,
`tests/gdrive/test_drive_migration_tools.py`,
`tests/test_admin_group_write.py`.

### P1 — architecture build

| Tool | API | Notes |
| --- | --- | --- |
| `create_shared_drive` | `drives.create` | `requestId` (UUID) makes our own retries idempotent. OU placement stays an Admin console step — no reliable public API. |
| `update_shared_drive` | `drives.update` | Rename + the four restriction flags (plus `sharingFoldersRequiresOrganizerPermission`). Re-reads `drives.get` after the update and flags a rename that didn't round-trip. |
| `list_shared_drives` | `drives.list` | Optional `use_domain_admin_access`. Says so explicitly when `max_results` capped the result. |
| `set_drive_permission` | `permissions.create` / `.update` | Groups-only guardrail. Idempotent. |
| `revoke_drive_permission` | `permissions.delete` | Refuses self-lockout; refuses to remove the last organizer of a shared drive. |
| `create_shortcut` | `files.create` (shortcut mime) | Idempotent per (target, parent). Refuses to chain shortcuts. |

**Naming decision — `revoke_drive_permission`, not `remove_drive_permission`.**
`remove_drive_permission` is in `BLOCKED_TOOLS`, and enforcement is by
`fn.__name__` at the registration chokepoint: a new function under that name
would be silently unregistered. Removing it from the denylist would re-expose
the unguarded legacy implementation that still lives in `gdrive/drive_tools.py`.
Renaming the new tool was the lower-risk option. The legacy sharing tools
(`share_drive_file`, `batch_share_drive_file`, `set_drive_file_permissions`,
`update_drive_permission`, `remove_drive_permission`, `transfer_drive_ownership`)
all stay blocked — the guarded tools are additive, not a relaxation.

**Groups-only guardrail** (`shared_drive_tools.assert_principal_is_group`,
called before `drive_batch.resolve_principal` builds the body). Enforcement is
a **positive Admin Directory lookup**, not the declared permission `type`.

The original design declared `type=group` and assumed Drive would reject a
personal address. Live testing disproved that: Drive silently coerces the type
and creates an individual grant. Never restore that assumption — see the
2026-08-12 findings in `FOLLOWUPS.md`.

So the address is resolved against `admin.directory.groups.get` first:

- Resolves as a group → grant proceeds.
- 404/400, or a 403 that `users.get` resolves as a person → refused outright,
  **no override**. The message names `allow_individual=True` as the explicit,
  audited way to grant to an individual on purpose. (The Directory answers
  `groups.get` with 403, not 404, for a personal address — hence the
  `users.get` disambiguation.)
- Directory genuinely unreachable or unresolved → refused, unless
  `allow_unverified_group=True` is passed, which logs loudly and annotates the
  result.

`allow_individual=True` skips the check and switches the type to `user`.
`anyone` / `domain` principals are refused in-tool and have no escape hatch —
no tool on this server can create a public link.

Operational consequence: group grants need a reachable Admin Directory service.
The OTB deployment has the `gadmin` read tools enabled, so the required scopes
(`admin.directory.group.readonly`, `.group.member.readonly`,
`.user.readonly`) are already granted. A drive-only deployment must enable
`gadmin` or pass `allow_unverified_group=True`.

### P2/P3 — migration engine

| Tool | Notes |
| --- | --- |
| `walk_drive` | Two passes: BFS by parent, then (shared-drive roots) an independent `corpora=drive` sweep. Sweep-only items are added to the manifest tagged `discovered_by: "sweep"` and called out in the summary — that's the fix for the lossy crawl that missed six folders and a whole drive. Rows sorted by path so two walks of a static drive are byte-identical. |
| `get_drive_file_metadata` | `files.get` with md5/sha1/sha256, `properties`, `appProperties`, `parents`, `driveId`. Says explicitly when the file is native Google and therefore checksumless. |
| `create_folder_tree` | Accepts `paths=[...]` or the xlsx tab-02 manifest shape (`drive`, `folder_path`, `action`). Existing path = reuse. Returns path → ID for registry write-back. |
| `batch_copy_from_manifest` | Idempotency key is the `mcp_source_file_id` **appProperty**, queried via `appProperties has { key=… and value=… }`. User-visible provenance goes in `properties` (`sourceFileId`, `sourceDrive`, `migrationBatch`). Rows run `batch_size` at a time; a failing row is recorded and the run continues. |
| `reconcile_folders` | Path-keyed diff emitting `missing_in_dest`, `extra_in_dest`, `mime_mismatch`, `size_mismatch`, `checksum_mismatch`, `checksum_unavailable`, `unverifiable_native`. The last two are non-blocking; everything else blocks the go/no-go. |
| `rebuild_hub` | Reads the registry's `hub_section` column, ensures a section folder per distinct value, diffs shortcuts by `targetId`. Orphan removal is opt-in (`remove_orphans=True`) and **soft-deletes** — see below. |

**Orphan removal never hard-deletes.** A shortcut is a Drive file, so
`rebuild_hub` moves orphans into `DRIVE_HOLDING_FOLDER_ID` with the same
`mcp_softdeleted` / `mcp_orig_parents` markers `soft_delete_drive_file` writes,
which means `restore_drive_file` reverses it. It fails closed if
`DRIVE_HOLDING_FOLDER_ID` is unset. `tests/gdrive/test_drive_migration_tools.py`
asserts the module contains no `.delete(` call at all.

`_get_holding_folder_id` moved from `gdrive/drive_tools.py` to
`gdrive.drive_helpers.get_holding_folder_id` so both soft-delete paths share
one definition; `drive_tools._get_holding_folder_id` is now a thin alias.

**Reports, not inline dumps.** `walk_drive`, `batch_copy_from_manifest` and
`reconcile_folders` write JSONL into the attachment store (0600, 1-hour
expiry) and return a summary plus the access line. A 40k-row inventory does
not belong in an MCP tool result.

### Admin SDK group writes — the one carve-out

`gadmin` stays read-only. Group writes live in a **separate service**,
`gadmin_write` (module `gadmin/admin_group_tools.py`), with its own scope list
`ADMIN_WRITE_SCOPES` and its own `tool_tiers.yaml` section. It is in
`OPT_IN_TOOLS`, so a wiped `TOOLS` env var never enables it.

The write surface is deliberately three tools: `create_group`,
`add_group_member`, `remove_group_member`. No user writes, no OU writes, no
role assignment, no group deletion — those stay on GAM CLI / the Admin
Console, and a parametrised source scan in `tests/test_admin_group_write.py`
asserts none of them are reachable.

`tests/test_admin_readonly.py` changed in exactly one place: the forbidden
scope-literal list no longer includes `admin.directory.group`, and a new test
pins that exception — the scope must be in `ADMIN_WRITE_SCOPES`, must not be in
`ADMIN_SCOPES` or either `gadmin` map, and must not be granted under
`--read-only`. Every other forbidden write scope (user, orgunit,
rolemanagement, device.mobile) is still banned outright.

### Scope wiring

- New scope group `drive_full` → `https://www.googleapis.com/auth/drive`.
  Required because `drive.file` cannot reach `drives.*` or permissions on
  items this app did not create. `DRIVE_SCOPES` already contained
  `DRIVE_SCOPE`, so the consent prompt is unchanged for the `drive` service.
- New scope group `admin_directory_group_write` →
  `https://www.googleapis.com/auth/admin.directory.group`. **This is a consent
  screen change** — add it before enabling `gadmin_write` or every call 403s.

### New env vars (both optional)

- `DRIVE_PERMISSION_ALLOWED_DOMAINS` — comma-separated domain allowlist for
  permission principals. Unset → no restriction. Set to `otbgroup.co.uk` to
  refuse external grants at the tool boundary.
- `DRIVE_HOLDING_FOLDER_ID` — already required for soft-delete; now also
  required for `rebuild_hub(remove_orphans=True)`.

### Render redeploy checklist

1. `TOOLS` stays service names only (`gmail drive calendar docs sheets
   contacts`). `TOOL_TIER=extended` already loads all twelve new Drive tools —
   they are declared at the extended tier precisely so no tier change is
   needed.
2. To enable group writes: add `gadmin_write` to `TOOLS`, and first add
   `https://www.googleapis.com/auth/admin.directory.group` to the OAuth
   consent screen.
3. Optional: set `DRIVE_PERMISSION_ALLOWED_DOMAINS=otbgroup.co.uk`.
4. No new pip dependencies.

### Rollback

The change is additive. To roll back a single tool, remove its name from
`core/tool_tiers.yaml` (it stops being registered under tier filtering) or add
it to `BLOCKED_TOOLS` in `core/tool_policy.py` (fail-closed, independent of
every other switch). To roll back the group-write service entirely, drop
`gadmin_write` from `TOOLS` — no code change, and the scope stops being
requested. To roll back the whole branch, revert the merge commit; the only
edits to pre-existing behaviour are the `_get_holding_folder_id` move (pure
refactor, same semantics) and the `tests/test_admin_readonly.py` carve-out.

### Benchmark note (TBRDC)

Measured against the mocked service doubles, not live Google — API latency
dominates in reality. Structural throughput characteristics:

- `walk_drive`: 1 `files.list` per folder (pageSize 1000, fully drained) plus
  1 sweep pass per 1000 items. A 5k-item drive with 400 folders is ~405
  requests. Expect roughly 1.5–3k items/min against live Drive, network-bound.
- `batch_copy_from_manifest`: 3 requests per row (provenance check, source
  `files.get`, `files.copy`), `batch_size` rows concurrently (default 10).
  Live throughput is capped by Drive's per-user write quota well before this
  server's; lower `batch_size` if 403 `userRateLimitExceeded` shows up in the
  result log.
- `reconcile_folders`: two full walks, so roughly 2× `walk_drive` cost.

Record real numbers in `FOLLOWUPS.md` after the first live pilot.

### Live verification still outstanding

The suite is unit-scope with mocked Google services. Before the architecture
build runs for real, execute the scratch-shared-drive checks listed in
`FOLLOWUPS.md` under "Live scratch-drive verification".

## Group-based tool access policy (claude/multi-account-workspace-groups-2dnyhi)

Google Workspace group membership decides which MCP tools each signed-in user
can see and call. Two sources of truth, deliberately separated:

- **Who** is in a group: the Google Admin console. Joiners, leavers and role
  changes are a group edit there, with Google's audit trail.
- **What** a group may do: `core/group_policy.yaml`, version-controlled and
  changed by PR.

This narrows what the assistant may *do* on a user's behalf. It never widens
what a user can *see*: every tool still runs with the caller's own OAuth token,
so Google's permissions remain the outer boundary.

**Files**

- `core/access_policy.py` — policy model, selector expansion, membership
  sources, TTL cache, decision engine. No FastMCP dependency; fully unit-tested.
- `core/group_policy.yaml` — the shipped OTB policy (three groups: admins,
  managers, staff).
- `auth/access_policy_middleware.py` — `AccessPolicyMiddleware`: filters
  `tools/list`, refuses disallowed `tools/call` with an `AuthorizationError`
  and writes a `status=denied` row to the audit sheet.
- `get_my_access` tool (`core/server.py`) — always callable; reports the
  caller's email, policy groups, decision source and allowed tool list.
- Tests: `tests/test_access_policy.py`, `tests/test_access_policy_middleware.py`,
  `tests/test_policy_group_guard.py`, `tests/test_auth_middleware_hooks.py`.

**Policy grammar.** `allow` / `deny` lists per group take `"*"`, `"<service>.*"`,
`"<service>.<tier>"` (cumulative like `--tool-tier`) or a bare tool name. A
tool name that does not exist in `core/tool_tiers.yaml`, or that is in
`BLOCKED_TOOLS`, makes the policy fail to load (typos must not silently grant
nothing). A user's allowed set is the union over matched groups of
`(allow − deny)`, plus `default`, plus `get_my_access`. A `deny` subtracts only
from its own group; the hard global stop for a tool is still `BLOCKED_TOOLS`.

**Membership lookup.** `members.hasMember` on the Directory API, once per
policy group per user, cached for `MCP_GROUP_POLICY_CACHE_TTL_S` (300 s).
`hasMember` reports direct *and nested* membership within the domain (per the
Directory API discovery document), so groups can contain groups. Cloud
Identity's transitive-membership API was rejected: it needs Enterprise or
Cloud Identity Premium. The lookup identity is a dedicated service account:
either holding a Workspace admin role with the Groups > Read privilege (assign
under Admin console → Account → Admin roles → role → *Assign service accounts*;
no domain-wide delegation), or, if that is unavailable, domain-wide delegation
impersonating `MCP_GROUP_POLICY_SUBJECT` with exactly the scope
`admin.directory.group.member.readonly`. A user's own token is never used to
decide that user's permissions.

**Failure behaviour (fail-closed).** Lookup error with no cached answer, or a
cached answer older than `MCP_GROUP_POLICY_STALE_TTL_S` (3600 s), means the
user gets `get_my_access` only. A policy file that does not parse means *every*
user gets `get_my_access` only, with the parse error in the log and in the
denial message. `MCP_GROUP_POLICY_BREAKGLASS_EMAILS` names accounts that skip
the lookup and get the full registered set (minus `BLOCKED_TOOLS`), logged at
WARNING on every decision: the owner's escape hatch if the Directory API is
down. Keep it to one address.

**Capabilities (parameter-level permissions).** A tool name cannot express
"send to an outside address" or "write into a folder someone else owns", so
each group may also carry `capabilities`, validated against
`KNOWN_CAPABILITIES`:

| Capability | Gates |
| --- | --- |
| `url_fetch` | `create_drive_file(fileUrl=http…)`, `import_to_google_doc(file_url=…)`: the server fetching an arbitrary URL on the caller's behalf (a beacon / exfil channel under prompt injection). |
| `external_share` | `set_drive_permission` to an individual or an outside address; `share_calendar` to an outside address or as `owner`; creating, copying, importing, exporting or moving anything into a destination controlled outside the organisation (`gdrive.drive_helpers.assert_internal_destination`): a My Drive folder owned by an outside address, or a shared drive that is not internal. A user can be a member of a shared drive another organisation owns and Drive exposes no owning-customer field to a member, so a shared drive counts as internal only when it is named in `DRIVE_INTERNAL_SHARED_DRIVE_IDS` or every visible organizer is on an internal domain; an unreadable organizer list is external. Leaving a folder (`remove_parents`) is never guarded. |
| `external_recipients` | `send_gmail_message` To/Cc/Bcc outside the organisation; `create_event` / `modify_event` attendees outside the organisation. |

"Outside the organisation" means not in `OAUTH_ALLOWED_EMAIL_DOMAINS`; when
that variable is unset nothing can be classified as external and the guards
are inert. OTB is one Workspace customer with several sign-in domains, and
in a multi-domain customer the `hd` claim and the email domain carry the
*user's own* domain, so the variable must list every domain that hosts a
staff sign-in account: today `otbgroup.co.uk,jit-logistics.com` (enumerated
from the live Directory; see the render.yaml comment). A single-domain value
locks JIT staff out with an unexplained auth failure. Re-enumerate at every
domain cutover (Vale and BIR staff move off `otbgroup.co.uk` when
`valeautomotive.co.uk` / `bir-d.co.uk` go live). In `off` mode every capability is granted, so today's single-user
behaviour is unchanged. Under `enforce` a denied capability raises
`CapabilityDenied` (a `UserInputError`) inside the tool, which the audit
wrapper records as an error row. The shipped policy grants all three to
`mcp-admins`, `external_recipients` to `mcp-managers`, none to `mcp-staff`.

Two tool-level rules apply regardless of mode: `create_gmail_filter` refuses
actions that `forward` mail or add `TRASH` / `SPAM` (persistent exfiltration
or inbox suppression: configure those in Gmail settings), and
`modify_sheet_values` refuses formula-looking cells without
`allow_formulas=True` (see the security review section).

**Per-user rate caps (enforce mode only).** `AccessPolicyMiddleware` counts
calls per (user, tool) in a sliding window for the tools whose repetition is
the damage: `soft_delete_drive_file` 20, `modify_gmail_message_labels` 60,
`send_gmail_message` 30, `update_drive_file` 60, `share_calendar` 5,
`set_drive_permission` 20, `create_gmail_filter` 5, `create_event` 60, all per
10 minutes. Over the cap the call is refused with an `AuthorizationError`
and audited as `denied`. Break-glass accounts are exempt. Override with
`MCP_TOOL_RATE_LIMITS` (JSON `{"tool": [count, seconds]}`, `0` disables a
tool's cap). Process-local: a redeploy resets the counters.

**Enforcement points.** `AuthInfoMiddleware` now also runs on `tools/list`, so
the policy middleware (added directly after it; FastMCP runs middleware in
registration order) can filter the listing per user. A refused call is audited
with `status=denied`, `error=policy: …` and the user's groups in
`params_summary`, because the audited tool wrapper never runs for a refused
call. stdio transport skips the policy (no OAuth identity there), matching
FastMCP's own `AuthMiddleware`.

**`gadmin_write` guard.** `create_group`, `add_group_member` and
`remove_group_member` refuse any group named in the policy file, before any
Directory call. Otherwise a user allowed `add_group_member` could add
themselves to `mcp-admins`. Policy-group membership is Admin-console-only.
Make the policy groups admin-managed and closed in Groups settings (nobody can
join, nobody but admins can add members) so the same escalation is impossible
through Google Groups itself.

**Env vars (all optional; defaults keep today's behaviour)**

| Var | Default | Meaning |
| --- | --- | --- |
| `MCP_GROUP_POLICY_MODE` | `off` | `enforce` switches the policy on. |
| `MCP_GROUP_POLICY_FILE` | `core/group_policy.yaml` | Policy path override. |
| `MCP_GROUP_POLICY_SA_JSON_FILE` | unset | Path to the service-account key (Render Secret File). Preferred. |
| `MCP_GROUP_POLICY_SA_JSON_B64` | unset | Base64 of the key JSON; used if the file var is unset. |
| `MCP_GROUP_POLICY_SUBJECT` | unset | Admin to impersonate via DWD. Leave unset when the SA holds the Groups Reader role itself. |
| `MCP_GROUP_POLICY_BREAKGLASS_EMAILS` | unset | Comma-separated full-access accounts. |
| `MCP_GROUP_POLICY_CACHE_TTL_S` | `300` | Fresh-cache window per user. |
| `MCP_GROUP_POLICY_STALE_TTL_S` | `3600` | How long a stale answer may be served during a Directory outage. |
| `MCP_GROUP_POLICY_STATIC_MEMBERS` | unset | JSON `{group: [emails]}`; dev/test only, ignored when a service account is set. |
| `MCP_TOOL_RATE_LIMITS` | see above | JSON override of the per-user call caps applied under `enforce`. |
| `DRIVE_INTERNAL_SHARED_DRIVE_IDS` | unset | Comma-separated shared-drive IDs always treated as internal by the destination guard, skipping the organizer lookup. |

**Also in this branch**

- `OAUTH_ALLOWED_EMAIL_DOMAINS` rejection is now explicit: a verified token
  from a foreign domain gets an `AuthorizationError` on `tools/call` instead
  of a confusing "no authenticated user" failure inside the tool.
- `MCP_ALLOWED_CLIENT_REDIRECT_URIS` (comma-separated patterns) is passed to
  FastMCP's `GoogleProvider` as `allowed_client_redirect_uris`. Unset keeps
  FastMCP's default, which accepts *any* redirect URI at dynamic client
  registration; a WARNING is logged at startup until it is set.
- `MCP_OAUTH_REFRESH_TOKEN_TTL_S` caps how long an MCP client stays signed
  in without re-consent (FastMCP default one year; blueprint 30 days), and
  a single-domain `OAUTH_ALLOWED_EMAIL_DOMAINS` is passed to Google as the
  `hd` sign-in hint. See the security review section.
- `render.yaml` now carries `OAUTH_ALLOWED_EMAIL_DOMAINS=otbgroup.co.uk`,
  `MCP_GROUP_POLICY_MODE=off`, `gadmin` in `TOOLS` (matching the live
  deployment) and `sync: false` placeholders for the new secrets,
  `WORKSPACE_EXTERNAL_URL` and `DRIVE_HOLDING_FOLDER_ID`.

**Rollback.** `MCP_GROUP_POLICY_MODE=off` (or unset) makes the middleware a
pass-through; nothing else changes. The `gadmin_write` guard stays active in
either mode by design.

## Security review fixes (claude/multi-account-workspace-groups-2dnyhi)

Findings from the multi-user security review that were clear-cut enough to
fix in the same branch. Each has unit coverage (`tests/test_deploy_config.py`,
`tests/test_hardening_round2.py`, `tests/test_hardening_round3.py`,
`tests/test_audit_service_account.py`, `tests/test_policy_group_guard.py`).

- **OAuth proxy state never reached the persistent disk.** `render.yaml`
  selects the `disk` backend, but the `DiskStore` import needs the
  `py-key-value-aio[disk]` extra, which was never declared. The import failed
  on every boot and FastMCP fell back to its own store under `~/.fastmcp`
  (ephemeral), so every redeploy wiped client registrations and upstream
  tokens and forced every user to re-authenticate. Fixed in `pyproject.toml`;
  `FASTMCP_HOME=/data/fastmcp` pins even the fallback store to the disk.
- **Debug log file.** `mcp_server_debug.log` was an unbounded DEBUG file
  inside the container holding query strings, identities and API error
  bodies. It now rotates (10 MiB × 3) and `WORKSPACE_MCP_FILE_LOGGING=false`
  (set in `render.yaml`) disables it where stdout is already retained.
- **Audit `error` column leaked query strings.** Google `HttpError` text
  embeds the request URL, whose query carries the Gmail/Drive search
  expression. URL query strings are stripped before the row is queued.
- **Gmail `attachments[].path` over streamable-http** could name any file
  under the server's home, including other users' relayed downloads. Refused
  for remote clients (base64 `content` still works), matching the existing
  gate on `import_to_google_doc` / `create_drive_file`.
- **Audit sheet readable and editable by every staff user.** See the audit
  section above: the service-account writer mode removes the need for staff
  to hold Editor on the Sheet.
- **Shared soft-delete holding folder.** `restore_drive_file` now refuses a
  file soft-deleted by another account; the holding folder otherwise let any
  user who can soft-delete pull another user's file out of it into a folder
  of their choosing.
- **Orphaned attachment relay files.** Metadata is in-memory, so files
  written before a restart were never swept. `cleanup_expired` now also
  unlinks untracked files older than the expiry.
- **Explicit domain rejection.** A verified token outside
  `OAUTH_ALLOWED_EMAIL_DOMAINS` is refused with an `AuthorizationError` on
  `tools/list`, `tools/call` and `prompts/get`, never falls through to the
  weaker identity fallbacks, and clears any identity left in session state by
  an earlier request.
- **Committed `google_workspace_mcp.dxt` removed.** The upstream desktop
  extension bundle checked into this fork contained the upstream author's
  `.mcpregistry_github_token` and `.mcpregistry_registry_token` files, their
  mypy/pytest/ruff caches and private review notes, and was copied into the
  public Docker image by `COPY . .`. Removed from the tree; `*.dxt` and
  `.mcpregistry_*` are now ignored by git and Docker. The files remain in
  git history (a public fork of a public repo); the upstream author should be
  told so they can rotate those tokens if they have not already.
- **Zip inflation cap on Office text extraction.** A small `.docx`/`.xlsx`
  whose XML member inflates to gigabytes would be read whole into memory.
  Members over 32 MiB or with an inflation ratio above 200:1 are refused.
- **Formula injection into Sheets.** `modify_sheet_values` defaults to
  `USER_ENTERED`, so text that starts with `=` (or `+FUNCTION(`) copied from
  an email became a live formula (`IMPORTDATA` / `IMPORTXML` exfiltrate the
  sheet). Such cells are now refused unless `allow_formulas=True` or
  `value_input_option='RAW'`.
- **Query strings out of the logs.** `handle_http_errors` scrubs URL query
  strings from Google error text before logging or returning it; the
  uvicorn access log drops query strings (OAuth callback codes and state);
  the startup banner no longer prints part of the client secret or the
  client ID; the OAuth 2.0 authorization URL is no longer logged; the
  `AUDIT_FALLBACK` / `AUDIT_DROP` stdout rows carry identity, tool and status
  but not `params_summary` or `error`.
- **`RENDER_EXTERNAL_URL` fallback.** A service re-created from the
  blueprint without `WORKSPACE_EXTERNAL_URL` used `http://localhost` as its
  OAuth issuer and attachment base; Render's injected `RENDER_EXTERNAL_URL`
  is now the fallback.
- **Dynamic client registration allowlist.** `MCP_ALLOWED_CLIENT_REDIRECT_URIS`
  (see the access-policy section). Until it is set, any party can register
  an MCP client against this server and phish a consent click.
- **Dependency advisories.** `mcp` 1.26.0 → 1.29.1: CVE-2026-52869
  (GHSA-jpw9-pfvf-9f58, CVSS 7.1) let anyone holding another user's session
  id inject JSON-RPC into that session because the SSE and Streamable HTTP
  transports never checked the authenticated principal; patched in 1.27.2.
  `python-multipart` 0.0.29 → 0.0.32 (seven denial-of-service and
  parameter-smuggling advisories on the unauthenticated OAuth form
  endpoints). `starlette` 0.52.1 is inside CVE-2026-54283's range (form
  limits ignored for URL-encoded bodies, fixed in 1.3.1) but `fastapi`
  0.128.3 caps starlette `<1.0`; fastapi is imported only for
  `HTMLResponse` / `JSONResponse` / `FileResponse` and the OAuth 2.0
  callback app, all of which starlette provides, so the way out is the
  small refactor parked in `FOLLOWUPS.md`. `tests/test_hardening_round5.py`
  pins the floors. `.github/dependabot.yml` shipped with an empty
  `package-ecosystem` (Dependabot rejects the file, so nothing ever ran);
  it now covers `uv`, `github-actions` and `docker` weekly.
- **OAuth store survives a key rotation.** Both `FernetEncryptionWrapper`
  constructions in `core/server.py` (disk and Valkey backends) pass
  `raise_on_decryption_error=False`. A record encrypted under a previous
  JWT signing key or client secret now reads as a miss (the client
  re-registers, the user re-consents) instead of raising on every OAuth
  request until `/data` is wiped by hand. Rotating the secret is the
  incident-response action, so it must not brick the service.
- **Google sign-in domain hint and refresh-token lifetime.**
  `_provider_hardening_kwargs` in `core/server.py`: when
  `OAUTH_ALLOWED_EMAIL_DOMAINS` names exactly one domain, Google's
  authorize request carries `hd=<domain>` so the account chooser prefers
  the Workspace account and a personal account's refresh token never lands
  in the store. It is a hint; the middleware domain policy stays the
  control. With OTB's two sign-in domains the hint is not sent (it only
  fires for exactly one domain), so on this deployment it is inactive
  until the domain list ever shrinks to one. `MCP_OAUTH_REFRESH_TOKEN_TTL_S` (seconds, positive integer) maps
  to FastMCP's `fallback_refresh_token_expiry_seconds`: how long a client
  stays signed in without re-consent. FastMCP's default is one year;
  `render.yaml` sets 2592000 (30 days).
- **`MCP_SINGLE_USER_MODE` set by hand.** `main.py` rejected only the
  `--single-user` flag under OAuth 2.1; the env var reached `get_credentials`
  directly and would hand any cached credential to every caller. Both
  `main._single_user_requested` and `google_auth._single_user_mode_active`
  now refuse it under OAuth 2.1.
- **Test isolation.** `tests/conftest.py` clears the deployment variables
  (`MCP_GROUP_POLICY_*`, `AUDIT_SA_JSON_*`, `MCP_TOOL_RATE_LIMITS`,
  `MCP_ALLOWED_CLIENT_REDIRECT_URIS`, `MCP_OAUTH_REFRESH_TOKEN_TTL_S`,
  `MCP_SINGLE_USER_MODE`) for every test and resets the policy engine, and
  `AccessPolicyEngine.from_env(environ=…)` no longer falls through to
  `os.environ` for the policy path. Before this, a shell with Render's
  `AUDIT_SA_JSON_B64` exported made four audit tests perform a live token
  exchange against Google. The `remove_group_member` alias and nesting
  guard now has its own tests.

Findings deliberately **not** fixed in code, with the recommended control:

- **`admin.directory.user.security` scope.** It is in `ADMIN_SCOPES`, so
  every user is asked for it, and it also authorises `tokens.delete`; the
  only consumer is the read tool `list_oauth_tokens_for_user`. Blocking that
  tool (`BLOCKED_TOOLS`) and dropping the scope narrows every staff token
  and the owner's stored refresh token. Left as a decision because it
  removes a tool.
- **Public repository and GHCR image.** The fork is public, so the group
  policy, audit Sheet ID, holding-folder design and blueprint are readable
  by anyone, and `docker-publish.yml` pushes the image to a public GHCR
  package. Render builds from source (`dockerfilePath`), so the workflow is
  not needed. A fork cannot be switched to private in place, so the history
  has been pushed to the private `Otblakee/otb-workspace-mcp` (see
  "Repository move" below); archive the fork once Render points at the new
  repo.
- **CI workflows.** `ruff.yml` runs with `contents: write` and auto-commits
  to same-repo PR branches (fork PRs get a read-only token, so the exposure
  is collaborators only); `publish-mcp-registry.yml` would try to publish
  the fork on any `v*` tag; actions are pinned by tag. Recommended: a
  read-only ruff check, delete the publish workflow, pin actions by SHA.
- **`gc.collect()` after every tool call** (`auth/service_decorator.py`) and
  per audit flush is a stop-the-world sweep on the single Render worker.
  Upstream added it to stop a googleapiclient memory leak; measure under a
  few concurrent users before replacing it with a periodic sweep.

- **`/attachments/{file_id}` is an unauthenticated capability URL.** Anyone
  holding the UUID can fetch the file for an hour, which turns a Drive file a
  user may read into a shareable link outside Google's controls. Mitigation
  today: unguessable UUID, 1-hour expiry, `Cache-Control: no-store`. Proper
  fix: retire the relay for Drive downloads in favour of the stateless
  pattern `get_gmail_attachment_content` already uses (parked in
  `FOLLOWUPS.md`).
- **Every registered MCP client gets the full scope set by default**
  (`valid_scopes` doubles as the DCR default scope). Narrowing it would
  break clients that do not request scopes explicitly; the per-tool scope
  check and the group policy are the effective controls.
- **JWT signing key derived from the OAuth client secret** when
  `FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY` is unset. Set it (it is already
  in `render.yaml` as a secret placeholder); rotating the client secret then
  no longer invalidates every session.
- **Revocation.** There is no revoke endpoint, but FastMCP validates the
  upstream Google token against `tokeninfo` on every request, so suspending
  the Google account or revoking the app's access in the Admin console takes
  effect on the user's next call. That is the leaver procedure.
- **Attribution fallback `DEFAULT_USER=oli`.** With the service-account
  writer those rows now reach the Sheet; treat any row carrying
  `DEFAULT_USER` as an attribution failure to investigate, not as the owner's
  activity.

### Multi-user rollout checklist (do in this order)

1. Merge this branch; let Render redeploy. Confirm in the logs that the
   OAuth proxy reports `Using DiskStore` (not a fallback warning) and that
   `mcp_server_debug.log` is no longer written.
2. In the GCP project: set the OAuth consent screen **User type to
   Internal** (removes the 100-test-user cap and the 7-day refresh-token
   expiry of External+Testing). Set `FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY`
   on Render to a long random secret.
3. Confirm `OAUTH_ALLOWED_EMAIL_DOMAINS=otbgroup.co.uk,jit-logistics.com` is
   set (now in `render.yaml`). Never a single domain: `jit-logistics.com`
   hosts a real sign-in account and would be locked out. Re-check with
   `list_users` at every domain cutover. Leave `TOOL_TIER` unset: the live deployment runs every
   tier of `gmail drive calendar docs sheets contacts gadmin` (visible from
   the connected tool list, which includes complete-tier and gadmin tools),
   and the group policy narrows the surface per user. Earlier notes in this
   file saying `TOOL_TIER=extended` is set on Render are out of date.
4. Read the redirect URIs registered by the real clients from the Render
   logs, then set `MCP_ALLOWED_CLIENT_REDIRECT_URIS` to exactly those
   patterns.
5. Create a service account in the GCP project, download its key, share the
   audit Sheet with it as **Editor** (remove Editor from everyone else, keep
   Viewer for the owner only), set `AUDIT_SA_JSON_B64`, redeploy, confirm new
   rows arrive with the correct `user` column.
6. In the Admin console: create `mcp-admins@`, `mcp-managers@` and
   `mcp-staff@otbgroup.co.uk` as admin-managed, closed groups (nobody can
   join; only admins add members). Put the owner in `mcp-admins`. Assign the
   service account a custom admin role with **Groups → Read** only.
7. Set `MCP_GROUP_POLICY_SA_JSON_B64` (same or a second key),
   `MCP_GROUP_POLICY_BREAKGLASS_EMAILS=oliver@otbgroup.co.uk`, then
   `MCP_GROUP_POLICY_MODE=enforce`; redeploy.
8. Verify with `get_my_access` as the owner (full set), as a staff test
   account (staff list), and as an account in no group (`get_my_access`
   only); make one denied call and confirm the `status=denied` audit row.
9. Only then add real staff to `mcp-staff@`. Add managers to
   `mcp-managers@` deliberately: that group can send email.

## Repository move to `Otblakee/otb-workspace-mcp` (claude/multi-account-workspace-groups-2dnyhi)

Implements §7.2 of the July 2026 plan on branch
`claude/security-audit-multiworkspace-d2nx3u`
(`SECURITY_AUDIT_AND_ROLLOUT_PLAN.md`): re-home, do not rewrite. The full
history and every branch of the public fork were pushed to the private repo
(a pure move; `main` there was already an ancestor, so it fast-forwarded),
then this branch carries the cleanup that the plan asked for as a reviewable
diff on top:

- Upstream distribution machinery removed: `publish-mcp-registry.yml`
  (would publish this repo to PyPI and the MCP Registry on a `v*` tag),
  `smithery.yaml`, `glama.json`, `manifest.json`, `server.json`,
  `README_NEW.md`. The `.dxt` bundle went earlier in this branch.
  `docker-publish.yml` and `helm-chart/` are left for a separate decision.
- Package renamed to `otb-workspace-mcp` (`pyproject.toml`, `uv.lock`,
  the `[project.scripts]` entry, repository URLs). `get_package_version`
  tries the new name first and keeps the two older names as fallbacks so
  `/health` never reports `dev` on an older environment. The Dockerfile runs
  `uv run main.py`, so the script rename changes nothing at runtime.

Still by hand, in this order:

1. Transfer the repo to the OTB GitHub organisation if wanted (Settings →
   Danger Zone → Transfer). GitHub redirects the old URL. Install the Claude
   GitHub App on the organisation so sessions can keep working on it.
2. Point the Render service at the new repo (Settings → Build & Deploy;
   Render's GitHub app must be granted access to it), redeploy, check
   `/health` reports the version.
3. In your local clone: `git remote rename origin upstream` and
   `git remote set-url --push upstream DISABLED`, then add the new repo as
   `origin`. Upstream changes are reviewed and cherry-picked, never merged
   wholesale: `BLOCKED_TOOLS` is a denylist, so a routine merge can register
   new destructive tools silently.
4. Archive `Otblakee/workspace-mcp-fixed` once the new deploy is proven.

The July plan's Phase 0 to Phase 3 items are delivered by this branch except:
the legacy OAuth 2.0 plaintext credential store (only used when
`MCP_ENABLE_OAUTH21` is off), a revocation endpoint, idle/absolute TTL on the
in-memory session store, the attachment relay, the Postgres audit mirror, and
the Workspace-side work (new GCP project with an Internal consent screen and
one OAuth client, revoking the six stray clients and the nine broad
third-party grants, App access control by org unit). Those remain in
`FOLLOWUPS.md` and the plan.

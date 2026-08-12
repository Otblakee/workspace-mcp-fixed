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

**Auth model:** the audit logger writes via the same OAuth credentials
that the calling user already provided to the MCP. No separate service
account. This means audit writes inherit the user's Sheets scope and
Editor permission on the audit Sheet. Trade-off: at single-user phase,
the user technically has Edit access on their own audit entries.
Acceptable risk during Phase 1; revisit before Team rollout (consider
Workload Identity Federation or a Render Postgres immutable mirror).

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

**Groups-only guardrail** (`drive_batch.resolve_principal`). The declared
permission `type` is what makes it fail closed: default `type=group` means
Drive itself rejects a personal address, so an individual grant cannot happen
by accident. `allow_individual=True` switches the type to `user` explicitly.
`anyone` / `domain` principals are refused in-tool and have no escape hatch —
no tool on this server can create a public link.

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

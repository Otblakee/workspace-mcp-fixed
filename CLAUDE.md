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

**Render env-var update**: ensure `TOOLS` (or whichever tier env var is
active) covers `gmail` extended tier, or add `delete_gmail_draft` explicitly.
Listed `TOOLS` should include it after `draft_gmail_message`.

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
2. Confirm `TOOLS` (or the tool-tier env var in use) covers `delete_gmail_draft` — either by enabling the `gmail` extended tier or by listing the tool name explicitly.
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

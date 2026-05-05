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

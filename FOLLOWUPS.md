# FOLLOWUPS

Items deliberately parked out of the
`fix/drive-base64-locale-draft-delete-docfmt` branch. Each is independently
shippable; pick them up as separate PRs.

## `modify_doc_text` — markdown support

Apply the same `content_format='markdown'` parameter to `modify_doc_text` (and
any other Doc-mutating tool that accepts a content blob). The Drive
converter does not support partial-document conversion the same way it
supports whole-Doc creation, so the implementation strategy is different from
`create_doc`: likely either (a) replace the doc body wholesale via Drive
update with a new markdown blob, or (b) generate `batchUpdate` requests
client-side from a parsed AST. Upstream
`taylorwilsdon/google_workspace_mcp#604` has a prototype for the matching
pattern on `update_drive_file` worth referencing.

Parked because Issue 5 explicitly scoped this out, and because (b) requires
either a markdown parser dependency or a non-trivial client-side lift that
deserves its own design discussion.

## Google markdown-converter limitations to verify on live runs

Once Issue 5 ships, run a live test of `create_doc(content_format='markdown',
…)` against each item and record any that don't render as expected:

- `# H1` / `## H2` / `### H3` headings
- `- a` and `* a` bulleted lists (including nested `  - b`)
- `1. a` numbered lists (including nested)
- `**bold**`, `*italic*`, `_italic_`
- Blank-line paragraph breaks
- Inline code `` `code` ``
- Fenced code blocks
- Tables (likely converter limitation — verify)
- Images via `![alt](url)` (likely won't fetch — verify)
- Links `[text](url)`

Per the project brief: **document, don't fix**. If the converter drops
something, note it here and update `CLAUDE.md`'s Markdown subset section so
future callers know what they get.

## `_apply_tab_formatting` portability (audit logging branch)

The audit-logging branch (`claude/add-audit-logging-lMWzV`, PR #1) added
`AuditLogger._apply_tab_formatting` for new monthly tabs. If/when a similar
"new sheet, please match the manual formatting" pattern is needed elsewhere
(e.g. weekly rotational tabs, per-user tabs), extract the rule construction
from `core/audit.py` into a small helper rather than copying. Not urgent;
flagged so future tab-creation code doesn't reinvent it.

## SERVICE_MAP gaps in `core/audit.py`

The audit module's `SERVICE_MAP` covers only drive/gmail/calendar/docs/sheets/
contacts. Tools from `gchat`, `gforms`, `gslides`, `gtasks`, `gsearch`,
`gappsscript` log `service="unknown"`. Add the missing keys when those services
are enabled. Tracked in the audit-logging PR (#1) review thread; included
here for completeness because the audit module ships in a separate PR.

## STDIO audit flusher start

`core/server.py` in the audit branch starts the audit flusher lazily on the
first tool invocation, which works under both HTTP and stdio. If we ever move
to a transport without a per-tool entry point, the lazy-start trick won't
fire — switch to a FastMCP `lifespan` hook at that point.

## Migrate remaining attachment tools to the stateless pattern

`get_gmail_attachment_content` now delivers statelessly (inline base64 under
`ATTACHMENT_INLINE_MAX_BYTES`, Drive transfer folder above it) and no longer
mints `/attachments/{file_id}` URLs. The other three tools that mint relay
URLs should follow the same pattern:

- `get_chat_attachment_content` (gchat/chat_tools.py)
- `get_drive_file_download_url` (gdrive/drive_tools.py)
- `download_drive_file` (gdrive/drive_tools.py)

Until then they share the relay's fragilities: instance-local metadata,
1-hour expiry, dead URLs after restart/redeploy.

## Persist AttachmentStorage metadata if the relay stays

`AttachmentStorage._metadata` is an in-process dict, so every stored
attachment 404s after a restart even when the file is still on disk. If the
relay isn't fully retired by the migration above, persist the metadata as a
JSON sidecar (e.g. `<file_id>.meta.json` next to each file, or one index
file) so a restarted instance can keep serving unexpired files.

## Ops: WORKSPACE_ATTACHMENT_DIR not set on the live Render service

The live Render service logs show attachments being written to
`/home/app/.workspace-mcp/attachments` — the in-code default — which means
the `WORKSPACE_ATTACHMENT_DIR=/data/attachments` value from `render.yaml` is
not actually set in the Render dashboard. Set it (or sync the dashboard env
group with `render.yaml`) so relay files land on the persistent disk rather
than the ephemeral container filesystem.

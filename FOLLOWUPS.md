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

## Live scratch-drive verification (Drive architecture + migration tools)

`tests/gdrive/test_shared_drive_tools.py`,
`tests/gdrive/test_drive_migration_tools.py` and
`tests/test_admin_group_write.py` are unit-scope with mocked Google services.
The following must be run once against a **scratch shared drive** before the
real architecture build, and the results recorded here:

1. `create_shared_drive` → confirm the drive appears in `list_shared_drives`
   and in the Admin console.
2. `update_shared_drive` rename → confirm the round-trip check passes (the
   tool re-reads `drives.get`; a silent failure would surface as the "did not
   round-trip" warning).
3. `set_drive_permission` with a real group → confirm the role via
   `get_drive_file_permissions`. Then repeat the same call and confirm it
   reports "No change". Then call it with a personal address and **no**
   `allow_individual` and confirm Google rejects it (this is the guardrail's
   real enforcement point, and it is the one behaviour the mocks cannot prove).
4. `revoke_drive_permission` against a second organizer; then attempt to
   revoke the last organizer and confirm the refusal fires.
5. `create_shortcut` twice with the same (target, parent) → confirm exactly
   one shortcut exists.
6. `walk_drive` twice against a static scratch drive → confirm identical row
   counts and identical manifest bytes. Add a folder that the parent walk
   cannot reach (e.g. a folder whose only parent link is broken) and confirm
   it is reported as sweep-only.
7. `batch_copy_from_manifest` 50-file pilot → confirm zero unstamped copies,
   then re-run the identical manifest and confirm zero copies.
8. `reconcile_folders` on the pilot → confirm 0 blocking discrepancies before
   the full run is authorised.
9. `rebuild_hub` with `dry_run=True` first; then `remove_orphans=True` and
   confirm the orphan lands in `DRIVE_HOLDING_FOLDER_ID` (not trash) and that
   `restore_drive_file` puts it back.

Record measured items/min for `walk_drive` and rows/min for
`batch_copy_from_manifest` here once available; the numbers in `CLAUDE.md` are
structural estimates only.

## Parked out of the Drive architecture branch

- **Permission expiry on shared drives.** `set_drive_permission` accepts
  `expiration_time` and validates its RFC 3339 shape, but Google only honours
  expiries on reader/commenter/writer *file* permissions — shared-drive
  memberships cannot expire. The tool passes the value through and lets Drive
  reject it rather than maintaining a second copy of Google's matrix. If the
  live run shows a confusing error, add an explicit pre-check.
- **`walk_drive` on My Drive folders.** The drive-wide sweep needs a
  `driveId` corpus, so a root that is a folder *inside* a drive gets the
  parent walk only and a warning saying so. A `corpora=user` sweep filtered to
  descendants would close this; it was not needed for the shared-drive
  migration.
- **Multi-parent items.** The inventory records an item once and warns on the
  second visit. Correct for shared drives (single parent), lossy for legacy My
  Drive content with multiple parents. Revisit only if a My Drive migration
  lands.
- **`create_folder_tree` concurrency.** Path segments are created
  sequentially so a shared prefix is never created twice. Fine at ~60 folders
  per template; if a template ever runs to thousands, batch the independent
  leaves.
- **Registry write-back.** `create_folder_tree` returns path → ID but does not
  write those IDs back to the Folder Registry sheet. That is a deliberate
  seam: the caller decides which registry column and row each ID belongs in.
  A `write_folder_registry` tool could close the loop.
- **OU placement of new shared drives** stays an Admin console step — there
  is no reliable public API for moving shared drives between OUs. Verify the
  API state before building anything here.

## Restore parallelism in `batch_copy_from_manifest`

Copy rows are issued sequentially. The reason is transport safety, not
preference: every request is built from the single `service` object injected by
`@require_google_service`, and `execute_with_backoff` dispatches each request to
a worker thread via `asyncio.to_thread`. googleapiclient's underlying httplib2
`Http` object is not thread-safe, so running rows concurrently shared one
connection across threads and could interleave or corrupt HTTP activity on a
real run — invisible in the mocked tests, ugly in a live migration.

`batch_size` is now the progress-logging interval rather than a concurrency
knob.

To restore real parallelism safely, give each concurrent worker its own
transport. The cleanest route with only public repo APIs is to call a
`@require_google_service("drive", "drive_full")` helper N times (as
`_hub_registry_service` does for Sheets) to obtain N independent service
objects, then pin one per worker slot. `build()` is local when the discovery
document is bundled — as it is for drive v3 — so the setup cost is small.
Worth doing only if a live pilot shows sequential throughput is the actual
bottleneck; Drive's per-user write quota is expected to bind first.

The same caveat applies to the pre-existing concurrent `asyncio.gather` calls in
`gchat/chat_tools.py` and `gappsscript/apps_script_tools.py`. Those fan out over
two or a handful of requests rather than thousands of rows, so the exposure is
much smaller, but the hazard is identical and they were not touched here.

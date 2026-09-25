# Changelog

All notable changes to OTB's fork of the Google Workspace MCP are recorded
here. Versions follow [Semantic Versioning](https://semver.org/). Earlier
releases are recorded in the git history and in `CLAUDE.md`.

## [1.14.3] - 2026-09-25

### Fixed

- Container start-up now works with a Render persistent disk. Render
  mounts the disk owned by root while the server runs as the non-root
  `app` user, so pointing `WORKSPACE_MCP_CREDENTIALS_DIR` at `/data`
  used to fail the start-up permission check. New `entrypoint.sh` starts
  as root, hands `/data` to `app` (only when the mount exists and is not
  already owned by `app`), then drops privileges with `gosu` before
  running the server. Without a disk the behaviour is unchanged: the
  server still runs as `app`.
- `gosu` is installed in the image; the `USER app` instruction is removed
  from the Dockerfile because the entrypoint now does the privilege drop.

### Deploy notes

- Attach the disk in the Render dashboard first (mount path `/data`),
  then set `WORKSPACE_MCP_CREDENTIALS_DIR=/data/credentials`,
  `WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND=disk`,
  `WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY=/data/oauth-proxy` and
  `WORKSPACE_ATTACHMENT_DIR=/data/attachments`, as `render.yaml` already
  describes. After that redeploys no longer log every connected client out.
- Services with a disk cannot use zero-downtime deploys or run more than
  one instance. Acceptable for this single-instance MCP.

## [1.14.2] - 2026-09-25

### Added

- `list_drive_themes`: read-only list of Google's stock shared drive themes
  (`about.get`, `fields=driveThemes`), one line per theme with `id`,
  `colorRgb` and `backgroundImageLink`, sorted by `id`. Audited like every
  other tool.
- `accent_hex` on `set_shared_drive_theme`: with `image_file_id`, sets the
  drive's exact accent colour (`colorRgb`) in the same `drives.update` as the
  image. The report names the nearest stock theme and its RGB distance for
  information only; no stock theme is applied. Refused with `theme_id`,
  because Google does not accept `colorRgb` alongside `themeId`.
- `accent` per category in `set_shared_drive_themes_from_registry`:
  `entity_images` values may be `{"image": <file_id>, "accent": <hex>}`, and a
  new `accent_hex` argument sets the default for categories without one.
  Plain file-ID values still work. A bad hex stops the run before any drive
  changes.

### Unchanged

- Omitting `accent_hex` leaves the drive colour alone, as before.

## [1.14.1] - 2026-09-25

### Fixed

- Shared drive banner tools no longer read `themeId` back from Drive. It is
  write-only in the Drive API, so every drive showed "(custom/none)" and every
  successful `set_shared_drive_theme(theme_id=...)` warned "verify the banner".
  Found on the first live read after the 1.14.0 deploy.
- `get_shared_drive_theme` and the before/after report now name the stock
  theme by matching the banner image against `about.get(driveThemes)`
  (query strings ignored); a custom image shows as "none (custom image)".
- A theme change is verified by the drive now showing that theme's image or
  colour.
- `list_shared_drives` shows `colorRgb` and `backgroundImageLink` only.
- `create_shared_drive` echoes the requested `theme_id` instead of reading
  `themeId` from the response (which always showed "(default)").

## [1.14.0] - 2026-09-25

### Added

- `set_shared_drive_theme`: set a shared drive's banner from a Google stock
  theme or a JPG/PNG already in Drive, with an optional crop (defaults to the
  largest centred 80:9 area). Checks Manager rights, falls back to
  domain-admin access, and returns the before and after theme, colour and
  image link. Supports `dry_run`.
- `get_shared_drive_theme`: read a drive's `themeId`, `colorRgb` and
  `backgroundImageLink`.
- `set_shared_drive_themes_from_registry`: apply an entity → image mapping
  (`OTB`, `JIT`, `VALE`, `BIR`, `Restricted`, `Hub`, `ExternalShare`) to every
  drive in the Folder Registry. Every image is checked before any drive
  changes; one failing drive does not stop the run. Supports `dry_run`.

### Changed

- `list_shared_drives` now shows `themeId`, `colorRgb` and
  `backgroundImageLink` for each drive.
- `gdrive.shared_drive_tools._get_shared_drive` accepts optional
  `use_domain_admin_access` and `fields`. Existing callers send the same
  request as before.

### Deploy notes

- No new env vars, scopes or dependencies. The three tools are at the
  `extended` tier, so `TOOL_TIER=extended` on Render loads them. The bulk
  tool also needs the `sheets` service enabled (already on at OTB).

# Changelog

All notable changes to OTB's fork of the Google Workspace MCP are recorded
here. Versions follow [Semantic Versioning](https://semver.org/). Earlier
releases are recorded in the git history and in `CLAUDE.md`.

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

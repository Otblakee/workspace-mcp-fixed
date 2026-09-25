# Changelog

All notable changes to OTB's fork of the Google Workspace MCP are recorded
here. Versions follow [Semantic Versioning](https://semver.org/). Earlier
releases are recorded in the git history and in `CLAUDE.md`.

## [1.16.0] - 2026-09-25

### Changed

- FastMCP moved from the 3.x line (3.3.1) to 4.0.10; the pin is now
  `fastmcp>=4.0.0,<5.0.0`. FastMCP 4 is built on the MCP Python SDK 2.x, so
  the lockfile also moves `mcp` 1.26.0 to 2.2.0 (plus the new `mcp-types`
  package), Starlette 0.52.1 to 1.7.0, FastAPI 0.128.3 to 0.141.1, and adds
  `httpx2`, `httpcore2` and `truststore`, which FastMCP uses for its own
  HTTP client. No other pin in `pyproject.toml` changed: the resolver moved
  those packages within the ranges already declared. The repo's own Google
  API calls still use `httpx`.
- Every FastMCP import the repo uses (`FastMCP`, `GoogleProvider`,
  `AccessToken`, `derive_jwt_key`, `Middleware`, `MiddlewareContext`,
  `get_context`, `get_access_token`, `get_http_headers`) exists in 4.0.10
  with the same signature, and the patched `server.tool` decorator, the
  `local_provider._components` tool table, `remove_tool`, `Context.set_state`
  and `get_state`, `custom_route` and `run(transport="streamable-http")`
  behave as before. The API differences that were checked and did not need
  code changes are listed in `CLAUDE.md` under "FastMCP 4 upgrade".

### Fixed

- The server could not boot on FastMCP 4. `SecureFastMCP.http_app` in
  `core/server.py` registered the audit flusher's start and shutdown drain
  with Starlette's `add_event_handler`, and Starlette 1.x removed that
  method (`AttributeError: 'StarletteWithLifespan' object has no attribute
  'add_event_handler'` on `server.run`). On Starlette 0.x the two handlers
  were silently ignored, because FastMCP always installs its own lifespan
  and Starlette only runs `on_startup` handlers from its default one, so
  the documented start-on-boot and drain-on-shutdown never actually ran and
  only the lazy per-tool start did. The app now wraps the router's lifespan
  context: the audit flusher starts before FastMCP's session manager comes
  up and drains after it has shut down. `tests/test_http_app_lifespan.py`
  drives the lifespan with Starlette's test client and pins this, and a
  source scan keeps the removed event API out of the entrypoints.

### Operator notes

- No new environment variable, no OAuth consent screen change, and no
  change to the Dockerfile, `render.yaml`, the Helm chart or the `/health`
  route. A normal Render redeploy is all that is needed.
- Connected clients keep working through the redeploy. This was verified
  against the disk-backed OAuth proxy state: dynamic client registrations,
  access tokens and refresh tokens written by the 3.x proxy are read and
  accepted by the 4.x proxy on the same `/data/oauth-proxy` directory (and
  the reverse, should a rollback be needed). The storage collections and
  token models are unchanged.
- Client-visible differences, all from FastMCP's own code:
  `/.well-known/openid-configuration` is now served alongside the two
  existing well-known routes; the authorization server metadata advertises
  `token_endpoint_auth_methods_supported: ["none", "private_key_jwt"]`
  (it said `client_secret_post` and `client_secret_basic` before, but the
  proxy has always stored registered clients as public `none` clients, so
  the advertisement now matches what is enforced) and
  `authorization_response_iss_parameter_supported: true`; registration
  responses carry `application_type: native`; an `/mcp` request with no
  token gets a 401 with an empty body and `WWW-Authenticate: Bearer
  scope="..." resource_metadata="..."` (a bad or expired token still gets
  `error="invalid_token"` with the same description as before, which is
  what clients key their re-authentication on); the initialize result no
  longer lists an empty `experimental` capability or the
  `io.modelcontextprotocol/ui` extension, neither of which this server
  used; and `serverInfo.version` now reads 4.0.10 (it has always been the
  FastMCP version, not this package's).
- FastMCP 4 clients negotiate the sessionless 2026-07-28 protocol by
  default (no `Mcp-Session-Id`, a fresh `ctx.session_id` per request). The
  auth middleware sets the request state on every call, so tool
  attribution and the audit log are unaffected. The `mcp_session_binding`
  fallback only ever helped clients that keep a session header, which the
  current connectors still do; a sessionless client has to present its
  bearer token on every request, which the OAuth 2.1 gate requires anyway.

## [1.15.1] - 2026-09-25

### Fixed

- The OAuth 2.1 proxy now really persists its state on the Render disk.
  `WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND=disk` needs the `disk` extra of
  `py-key-value-aio`; the image did not have it, so the server logged
  "Disk storage requested but dependencies not available" and fell back to
  in-memory storage. The Google credentials were on `/data`, but the tokens
  the connected clients hold were not, so every deploy still logged them
  out. The extra is now a base dependency and
  `tests/test_deploy_config.py::TestOAuthProxyDiskStorage` pins it.

## [1.15.0] - 2026-09-25

### Added

- Centrally managed Gmail signatures: opt-in service `gsignatures` with five
  tools. `preview_email_signature` and `get_email_signatures` read;
  `set_email_signature` and `apply_email_signatures` write, dry run by
  default, and a live write needs `dry_run=False` together with
  `confirm=True`; `audit_email_signatures` compares every send-as address
  in a scope with the ledger and never writes a signature. Scopes are exactly
  one of OU, domain or group (or `all_users` for audits); a scope larger
  than `max_users` is refused with the count.
- `python -m gsignatures.audit_cli` for the weekly Render cron (run as
  `uv run python -m gsignatures.audit_cli --all` in the container): exit 0
  when every managed address matches the ledger, 2 on any drift, 1 on a
  fatal error. Audits only; never re-applies.
- The service uses a Google service account with domain-wide delegation
  (`gsignatures/sa_auth.py`), never the user's OAuth token. Delegation
  scopes are exactly `gmail.settings.basic`,
  `admin.directory.user.readonly` and
  `admin.directory.group.member.readonly`; `gmail.settings.sharing` is
  deliberately not granted. Every tool refuses callers not on
  `SIGNATURE_ADMIN_EMAILS` (default `oliver@otbgroup.co.uk`) before any
  Google call.
- New env vars, all read by the feature only: `SIGNATURE_SERVICE_ACCOUNT_FILE`
  (or `SIGNATURE_SERVICE_ACCOUNT_JSON`), `SIGNATURE_DIRECTORY_ADMIN`,
  `SIGNATURE_ADMIN_EMAILS`, `SIGNATURE_LEDGER_SHEET_ID`. None is printed at
  start-up.
- Ledger Sheet (`OTB_LOG_SignatureLedger_2026-09-25_v1`): one row per live
  apply with the hash Gmail returned after the write and the previous
  signature HTML; audit runs write an `Audit_<date>` tab.
- Wiring: `gsignatures` in `OPT_IN_TOOLS`, `tool_imports`, `tool_icons` and
  the `--tools` choices in `main.py`; a `gsignatures` section in
  `core/tool_tiers.yaml` (all five at the core tier); empty entries in
  `TOOL_SCOPES_MAP` and `TOOL_READONLY_SCOPES_MAP`; `gsignatures` in the
  audit logger's module map.

### Deploy notes

- Follow `gsignatures/RUNBOOK.md` in order: service account (no IAM roles),
  domain-wide delegation with the three scopes, the ledger Sheet shared with
  the service account, the Render environment group `signatures` (secret
  file plus four variables) attached to the web service and the cron, then
  add `gsignatures` to `TOOLS`. The service never loads unless `TOOLS` names
  it. No OAuth consent screen change, no new pip dependency.
- Pilot on the owner first (`preview`, dry run, live on the primary and one
  alias), then roll out by OU with dry runs and kept result tables.

### Review pass before release

Four independent reviews (security, conformance to the owner decisions,
test gaps, operator readiness) ran on the build, every medium or high
finding was checked by three separate verifiers, and the confirmed ones were
fixed before this version was cut. The version was not released in between,
so the fixes are part of 1.15.0 rather than a patch release.


#### Added

- `restore_email_signature`: the sixth `gsignatures` tool. Puts back the
  `previous_signature_html` the ledger recorded for one send-as address
  (latest row, or a named `run_id`), under the same dry-run and confirm
  rule, and records the restore as a ledger row with versions `restored`.
  Replaces the runbook's "paste it back in the editor" rollback step, which
  the owner could not perform.
- Audit status `stale_directory`: the ledger's `rendered_hash` no longer
  matches a fresh render (a job title or mobile changed in the Directory
  since the apply). Counts as drift for the cron, as it already counted as
  `would_apply` for `apply_email_signatures`.
- The engine refuses a rendered signature over Gmail's 10,000-character
  limit (`engine.MAX_SIGNATURE_CHARS`) as an `error` row for that address.

#### Fixed

- `--read-only` now removes `set_email_signature`, `apply_email_signatures`
  and `restore_email_signature` at registration (they carry a
  `_workspace_write_tool` marker that `core.tool_registry` honours, since
  they hold no OAuth scope), and each refuses a live run in its body when
  the server is read-only.
- A live run proves the ledger writable before the first Gmail patch by
  re-writing the identical header row, so a Sheet shared as Viewer is
  refused up front instead of after the write. Once a ledger append fails
  mid-run, no further address is patched in that run (each becomes an
  `error` row reading `not attempted`), in that user and every later one.
- A dry run whose ledger could not be read says so on every `would_apply`
  row and in a header line, instead of claiming `no ledger row for this
  address`. A Sheet with no `Ledger` tab yet reads as an empty ledger for
  the read tools and the cron, so a fresh deployment audits as
  `never_applied` rather than failing with a 400.
- `python -m gsignatures.audit_cli`: a command line argparse rejects exits 1
  (fatal), not argparse's 2, which is the drift code; `--help` stays 0. The
  CLI reads the ledger through the same `prepare_ledger` as the audit tool.
- An unknown or unreadable group scope is refused naming the group; a
  missing or unreadable service-account key aborts a scope run instead of
  producing one identical error row per user. `include_aliases=False`
  reports excluded aliases as `skipped` even when the engine would have
  reported an error for them. The switch checks run before any client is
  built, so the caller sees the confirm or scope refusal even when no ledger
  or key is configured.
- Ledger rows without a user or send-as are logged by position, timestamp
  and run_id only, never with `previous_signature_html`. Audit rows for the
  signature tools now carry the mailbox (`user_email`) as `resource_id`.
- Runbook: pilot expectations corrected (step 6.2 with no `Ledger` tab,
  step 6.8 after two addresses), the Companies House cross-reference, the
  `render.yaml` reconciliation, the Directory admin's required privileges,
  delegation propagation (up to 24 hours), Render failure notifications
  (opt-in, not automatic email), and the secret file readability fallback.
  `uv run python -m gsignatures.audit_cli --all` is the cron command
  everywhere.

#### Hardened after the review pass

- The caller gate also checks `authenticated_via` and accepts only the
  server's OAuth 2.1 paths (`fastmcp_oauth`, `mcp_session_binding`). The
  raw bearer-token path does not check a token's audience and the stdio
  paths carry no login, so an allowlisted address arriving that way is
  refused for these tools; nothing else on the server changes.

### Rollback

- Drop `gsignatures` from `TOOLS` and redeploy to remove the tools. Delete
  the domain-wide delegation entry to kill the capability outright. The
  ledger's `previous_signature_html` column restores any signature by hand;
  reverting the template versions in git and re-running restores them in
  bulk.

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

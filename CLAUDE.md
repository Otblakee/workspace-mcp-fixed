# CLAUDE.md

Project guidance for Claude Code working in this repo.
See `README.md` for project overview, setup, transports, and tool tiers.

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

**Hard rule:** never enable `apps_script` in `TOOLS` env var until audit log has been live
and reviewed for 30 days.

# CLAUDE.md

Project guidance for Claude Code working in this repo.
See `README.md` for project overview, setup, transports, and tool tiers.

## Audit logging

Every MCP tool call is logged to a Google Sheet via `core/audit.py`.

**Sheet:** OTB_LOG_MCPAuditLog_2026-05-04_v1
**Sheet ID:** `1bfVQMbU3PgEkjN58fD01dIE2WBTJ9rsdM-3R_yUaTkE`
**Tabs:** One per calendar month (`YYYY-MM`), auto-created on first write.

**Required Render env vars:**
- `AUDIT_SHEET_ID` — Sheet ID above
- `AUDIT_SA_JSON_B64` — base64-encoded service account JSON (key for `otb-mcp-audit` SA)
- `AUDIT_FLUSH_INTERVAL_S` — default 30
- `AUDIT_BATCH_SIZE` — default 50
- `DEFAULT_USER` — default "oli"

**Architecture:** decorator + monkey-patch on FastMCP `tool` decorator at server init.
Async queue, buffered flush every 30s. Sensitive params (body, content, values, notes,
subject, etc.) are redacted to `<redacted:type:length>`. Audit failures never break tool calls.

**Hard rule:** never enable `apps_script` in `TOOLS` env var until audit log has been live
and reviewed for 30 days.

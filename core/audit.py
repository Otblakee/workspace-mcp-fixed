"""OTB Workspace MCP — audit logger.

Async, buffered, fail-soft. Logger errors never break tool calls.

Auth model: writes use the SAME OAuth credentials each calling user already
provided to the MCP. No service-account key. The user must hold Sheets write
scope (every Workspace user enabled here already does, since `sheets` is one
of the live services) and Editor on the audit Sheet. Trade-offs documented
in CLAUDE.md."""

import asyncio
import functools
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from google.auth.transport.requests import Request as _GoogleAuthRequest
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

AUDIT_SHEET_ID = os.environ.get("AUDIT_SHEET_ID", "")
FLUSH_S = int(os.environ.get("AUDIT_FLUSH_INTERVAL_S", "30"))
BATCH = int(os.environ.get("AUDIT_BATCH_SIZE", "50"))
DEFAULT_USER = os.environ.get("DEFAULT_USER", "oli")
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

ENABLED = bool(AUDIT_SHEET_ID)

HEADERS = ["timestamp_utc", "user", "service", "tool", "params_summary",
           "resource_id", "status", "error", "latency_ms"]

SERVICE_MAP = {"drive": "drive", "gmail": "gmail", "calendar": "calendar",
    "event": "calendar", "doc": "docs", "comment": "docs", "spreadsheet": "sheets",
    "sheet": "sheets", "contact": "contacts", "label": "gmail", "filter": "gmail",
    "permission": "drive"}

SENSITIVE = {"body", "text_content", "message_body", "html_body", "values",
             "data", "content", "notes", "description", "subject", "query",
             "base64_content", "fileUrl", "attachments"}


def _service(tool: str) -> str:
    n = tool.lower()
    for k, v in SERVICE_MAP.items():
        if k in n:
            return v
    return "unknown"


def _redact(kwargs: dict) -> str:
    out = {}
    for k, v in (kwargs or {}).items():
        if k in SENSITIVE:
            try:
                length = len(v) if hasattr(v, "__len__") else "?"
            except Exception:
                length = "?"
            out[k] = f"<redacted:{type(v).__name__}:{length}>"
        elif isinstance(v, str) and len(v) > 200:
            out[k] = v[:200] + "…"
        else:
            try:
                json.dumps(v); out[k] = v
            except Exception:
                out[k] = f"<{type(v).__name__}>"
    return json.dumps(out, ensure_ascii=False)[:1000]


def _resource_id(result: Any, kwargs: dict) -> str:
    for k in ("file_id", "document_id", "spreadsheet_id", "message_id",
              "event_id", "thread_id", "calendar_id", "resource_name"):
        if k in (kwargs or {}):
            return str(kwargs[k])[:200]
    if isinstance(result, dict):
        for k in ("id", "fileId", "documentId", "spreadsheetId", "messageId", "resourceName"):
            if k in result:
                return str(result[k])[:200]
    return ""


async def _resolve_user_email() -> str:
    """Read the authenticated user's email off FastMCP context state.

    The auth middleware sets `authenticated_user_email` on the request context
    for each tool call. Falls back to DEFAULT_USER when no context is live
    (local stdio dev, background task before any request, etc.)."""
    try:
        from fastmcp.server.dependencies import get_context
        ctx = get_context()
        if ctx is None:
            return DEFAULT_USER
        email = await ctx.get_state("authenticated_user_email")
        return email or DEFAULT_USER
    except Exception:
        return DEFAULT_USER


def _resolve_credentials(user_email: str):
    """Look up refreshable Google OAuth credentials for ``user_email``.

    Branches on the active OAuth mode: 2.1 reads the in-process session store
    (no context dependency, lock-protected — safe from a background flusher);
    2.0 falls back to the legacy file/store helper. Returns None if no
    credentials are available for the user; the caller is expected to skip
    that user and try another in the batch."""
    try:
        from auth.oauth_config import is_oauth21_enabled
        if is_oauth21_enabled():
            from auth.oauth21_session_store import get_oauth21_session_store
            return get_oauth21_session_store().get_credentials(user_email)
        from auth.google_auth import get_credentials as _get_oauth20_credentials
        return _get_oauth20_credentials(
            user_google_email=user_email,
            required_scopes=[SHEETS_SCOPE],
        )
    except Exception as e:
        log.debug("Audit credential lookup failed for %s: %s", user_email, e)
        return None


class AuditLogger:
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self._task = None
        self._current_tab = None

    async def start(self):
        if not ENABLED:
            log.warning("Audit DISABLED — missing AUDIT_SHEET_ID")
            return
        self._task = asyncio.create_task(self._loop())
        log.info("Audit started → sheet %s", AUDIT_SHEET_ID)

    def submit(self, entry: dict):
        if not ENABLED:
            return
        try:
            self.q.put_nowait(entry)
        except asyncio.QueueFull:
            log.error("AUDIT_DROP %s", json.dumps(entry))

    async def _loop(self):
        while True:
            await asyncio.sleep(FLUSH_S)
            batch = []
            while not self.q.empty() and len(batch) < BATCH:
                batch.append(self.q.get_nowait())
            if not batch:
                continue
            try:
                await self._flush(batch)
            except Exception as e:
                log.error("Audit flush failed (%s) — falling back to stdout", e)
                for entry in batch:
                    log.error("AUDIT_FALLBACK %s", json.dumps(entry))

    async def _flush(self, batch):
        sheets = await asyncio.to_thread(self._build_sheets_for_batch, batch)
        if sheets is None:
            raise RuntimeError("no usable user OAuth credentials in batch")
        tab = datetime.now(timezone.utc).strftime("%Y-%m")
        if tab != self._current_tab:
            await asyncio.to_thread(self._ensure_tab, sheets, tab)
            self._current_tab = tab
        rows = [[e.get(h, "") for h in HEADERS] for e in batch]
        await asyncio.to_thread(
            sheets.spreadsheets().values().append(
                spreadsheetId=AUDIT_SHEET_ID,
                range=f"'{tab}'!A:I",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows}).execute)

    def _build_sheets_for_batch(self, batch):
        """Fresh Sheets client per flush, built from the first user in the
        batch whose OAuth credentials resolve and refresh cleanly. Building
        per-flush rather than caching means token-refresh work flows through
        the standard google-auth lifecycle naturally."""
        seen = set()
        for entry in batch:
            email = entry.get("user")
            if not email or email in seen:
                continue
            seen.add(email)
            creds = _resolve_credentials(email)
            if creds is None:
                continue
            if creds.expired and getattr(creds, "refresh_token", None):
                try:
                    creds.refresh(_GoogleAuthRequest())
                except Exception as e:
                    log.warning("Audit creds refresh failed for %s: %s", email, e)
                    continue
            return build("sheets", "v4", credentials=creds, cache_discovery=False)
        return None

    def _ensure_tab(self, sheets, tab):
        meta = sheets.spreadsheets().get(spreadsheetId=AUDIT_SHEET_ID).execute()
        if tab not in {s["properties"]["title"] for s in meta.get("sheets", [])}:
            add_resp = sheets.spreadsheets().batchUpdate(
                spreadsheetId=AUDIT_SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": tab}}}]}).execute()
            sheets.spreadsheets().values().update(
                spreadsheetId=AUDIT_SHEET_ID,
                range=f"'{tab}'!A1:I1",
                valueInputOption="RAW",
                body={"values": [HEADERS]}).execute()
            try:
                new_sheet_id = add_resp["replies"][0]["addSheet"]["properties"]["sheetId"]
                self._apply_tab_formatting(sheets, new_sheet_id)
            except Exception as e:
                log.warning("Audit tab '%s' created without conditional formatting: %s", tab, e)

    def _apply_tab_formatting(self, sheets, sheet_id):
        grid_range = {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 1000,
            "startColumnIndex": 0, "endColumnIndex": 9,
        }
        write_ops_re = ('=REGEXMATCH($D2,"^(create|modify|send|delete|update|share|'
                       'set|add|remove|transfer|batch|draft|import|copy|format|'
                       'insert|manage|reply|resolve)_")')
        rules = [
            ('=$G2="error"',
             {"red": 0.988, "green": 0.894, "blue": 0.894},
             {"red": 0.612, "green": 0.0, "blue": 0.024}),
            (write_ops_re,
             {"red": 1.0, "green": 0.949, "blue": 0.8},
             {"red": 0.498, "green": 0.376, "blue": 0.0}),
        ]
        requests = [{
            "addConditionalFormatRule": {
                "index": i,
                "rule": {
                    "ranges": [grid_range],
                    "booleanRule": {
                        "condition": {"type": "CUSTOM_FORMULA",
                                      "values": [{"userEnteredValue": formula}]},
                        "format": {"backgroundColor": bg,
                                   "textFormat": {"foregroundColor": fg}},
                    },
                },
            },
        } for i, (formula, bg, fg) in enumerate(rules)]
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=AUDIT_SHEET_ID,
            body={"requests": requests}).execute()


_inst: Optional[AuditLogger] = None


def logger() -> AuditLogger:
    global _inst
    if _inst is None:
        _inst = AuditLogger()
    return _inst


def audit_log(user_resolver: Callable[[], str] | None = None):
    """Wrap a tool function. Logs success/error to the audit Sheet via
    async queue.

    The ``user`` field on each row is captured from the FastMCP request
    context (``authenticated_user_email``). ``user_resolver`` is preserved as
    an additional fallback layer between the context lookup and DEFAULT_USER
    — useful for tests and any future tool that wants to override the
    attribution."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrap(*args, **kwargs):
            t0 = time.perf_counter()
            result, err, status = None, "", "success"
            try:
                result = await fn(*args, **kwargs)
                return result
            except Exception as e:
                status = "error"
                err = f"{type(e).__name__}: {str(e)[:300]}"
                raise
            finally:
                try:
                    user = await _resolve_user_email()
                    if user == DEFAULT_USER and user_resolver is not None:
                        try:
                            user = user_resolver() or DEFAULT_USER
                        except Exception:
                            pass
                    logger().submit({
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "user": user,
                        "service": _service(fn.__name__),
                        "tool": fn.__name__,
                        "params_summary": _redact(kwargs),
                        "resource_id": _resource_id(result, kwargs),
                        "status": status,
                        "error": err,
                        "latency_ms": int((time.perf_counter() - t0) * 1000),
                    })
                except Exception as ae:
                    log.error("Audit submit failed (non-fatal): %s", ae)
        return wrap
    return deco

"""OTB Workspace MCP — audit logger.

Async, buffered, fail-soft. Logger errors never break tool calls.

Auth model: writes use the SAME OAuth credentials each calling user already
provided to the MCP. No service-account key. The user must hold Sheets write
scope (every Workspace user enabled here already does, since `sheets` is one
of the live services) and Editor on the audit Sheet. Trade-offs documented
in CLAUDE.md."""

import asyncio
import functools
import gc
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
             "base64_content", "fileUrl", "attachments",
             # Resumable upload session URIs are short-lived bearer-equivalent
             # tokens — anyone with one can PUT bytes to the in-flight upload.
             "upload_uri"}


def _service(tool: str) -> str:
    n = tool.lower()
    for k, v in SERVICE_MAP.items():
        if k in n:
            return v
    return "unknown"


# Deep redaction: SENSITIVE keys are matched at every nesting level so
# nested payloads like attachments=[{"content": ...}] or
# message={"raw": ...} don't slip through the way the old shallow walk
# allowed. Bounded depth keeps adversarial inputs from blowing the stack.
_REDACT_MAX_DEPTH = 6


def _safe_len(v) -> Any:
    try:
        return len(v) if hasattr(v, "__len__") else "?"
    except Exception:
        return "?"


def _redact_value(v: Any, depth: int = 0) -> Any:
    if depth >= _REDACT_MAX_DEPTH:
        return "<truncated:depth>"
    if isinstance(v, dict):
        out = {}
        for k, val in v.items():
            if k in SENSITIVE:
                out[k] = f"<redacted:{type(val).__name__}:{_safe_len(val)}>"
            else:
                out[k] = _redact_value(val, depth + 1)
        return out
    if isinstance(v, (list, tuple)):
        return [_redact_value(item, depth + 1) for item in v]
    if isinstance(v, str) and len(v) > 200:
        return v[:200] + "…"
    return v


def _redact(kwargs: dict) -> str:
    redacted = _redact_value(kwargs or {})
    try:
        return json.dumps(
            redacted,
            ensure_ascii=False,
            default=lambda o: f"<{type(o).__name__}>",
        )[:1000]
    except Exception:
        return str(redacted)[:1000]


def _resource_id(result: Any, kwargs: dict) -> str:
    # Treat None values the same as missing keys. ``str(None)`` would log
    # the literal string "None" which is useless noise in the audit sheet
    # and confusing in queries that expect either a real ID or empty.
    kw = kwargs or {}
    for k in ("file_id", "document_id", "spreadsheet_id", "message_id",
              "event_id", "thread_id", "calendar_id", "resource_name"):
        v = kw.get(k)
        if v is not None and v != "":
            return str(v)[:200]
    if isinstance(result, dict):
        for k in ("id", "fileId", "documentId", "spreadsheetId", "messageId", "resourceName"):
            v = result.get(k)
            if v is not None and v != "":
                return str(v)[:200]
    return ""


def _origin_error_type(e: BaseException) -> str:
    """Return the most informative exception class name for ``e``.

    ``handle_http_errors`` re-raises Google ``HttpError`` (and other
    framework errors) as bare ``Exception(message) from cause``. The audit
    row used to capture ``type(e).__name__`` directly, which collapsed every
    such failure to the unhelpful string ``"Exception"``. When the outer is
    a plain ``Exception`` and a ``__cause__`` is attached, we prefer the
    cause's class name so the audit row reads e.g. ``HttpError: ...``
    instead of ``Exception: ...``.
    """
    if type(e) is Exception and e.__cause__ is not None:
        return type(e.__cause__).__name__
    return type(e).__name__


async def _resolve_user_email() -> str:
    """Read the authenticated user's email off FastMCP context state.

    The auth middleware sets `authenticated_user_email` on the request context
    for each tool call. Falls back to DEFAULT_USER when no context is live
    (local stdio dev, background task before any request, etc.).

    Empty-but-present context state and resolver exceptions are warn-logged
    rather than swallowed silently — by the time this runs (in a tool call's
    finally block) the auth middleware should have populated state, so an
    empty value usually indicates a middleware bug worth seeing."""
    try:
        from fastmcp.server.dependencies import get_context
        ctx = get_context()
        if ctx is None:
            return DEFAULT_USER
        email = await ctx.get_state("authenticated_user_email")
        if not email:
            log.warning(
                "Audit: context present but authenticated_user_email empty; "
                "attributing to DEFAULT_USER. Likely a middleware ordering bug."
            )
            return DEFAULT_USER
        return email
    except Exception as e:
        log.warning(
            "Audit: failed to resolve user email (%s); attributing to DEFAULT_USER", e
        )
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
    # Run periodic memory-hygiene sweeps every Nth flush iteration.
    # At FLUSH_S=30, N=10 ≈ once every 5 minutes.
    _SWEEP_EVERY_N_TICKS = 10

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self._task = None
        self._current_tab = None
        self._tick = 0

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
            self._tick += 1
            if self._tick % self._SWEEP_EVERY_N_TICKS == 0:
                _run_periodic_memory_sweeps()
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
        # Group entries by attributed user. Each user's rows are written with
        # that user's own OAuth credentials so Sheets revision history matches
        # the actor recorded in the row, and so one teammate's audit data is
        # never written by another teammate's token. Trade-off: O(distinct
        # users in batch) Sheets API calls instead of one — fine at team
        # scale. Entries we can't write (no resolvable creds) fall through to
        # the stdout fallback the caller already has.
        by_user: dict[str, list] = {}
        for entry in batch:
            by_user.setdefault(entry.get("user") or DEFAULT_USER, []).append(entry)

        tab = datetime.now(timezone.utc).strftime("%Y-%m")
        tab_ensured_this_flush = tab == self._current_tab
        unwritten: list = []

        for email, entries in by_user.items():
            sheets = await asyncio.to_thread(self._build_sheets_for_user, email)
            if sheets is None:
                unwritten.extend(entries)
                continue
            try:
                if not tab_ensured_this_flush:
                    await asyncio.to_thread(self._ensure_tab, sheets, tab)
                    self._current_tab = tab
                    tab_ensured_this_flush = True
                rows = [[e.get(h, "") for h in HEADERS] for e in entries]
                await asyncio.to_thread(
                    sheets.spreadsheets().values().append(
                        spreadsheetId=AUDIT_SHEET_ID,
                        range=f"'{tab}'!A:I",
                        valueInputOption="RAW",
                        insertDataOption="INSERT_ROWS",
                        body={"values": rows}).execute)
            except Exception as e:
                log.warning("Audit append failed for %s (%d rows): %s", email, len(entries), e)
                unwritten.extend(entries)
            finally:
                # Drop the per-user sheets client; googleapiclient Resource
                # trees retain circular refs that defy refcount cleanup.
                await asyncio.to_thread(sheets.close)
                gc.collect()

        if unwritten:
            raise RuntimeError(
                f"audit flush: {len(unwritten)} of {len(batch)} rows had no usable user creds"
            )

    def _build_sheets_for_user(self, email: str):
        """Fresh Sheets client built from the named user's OAuth credentials.
        Returns None if the user has no resolvable credentials or refresh
        fails. Building per-flush rather than caching means token-refresh
        work flows through the standard google-auth lifecycle naturally."""
        if not email:
            return None
        creds = _resolve_credentials(email)
        if creds is None:
            return None
        if creds.expired and getattr(creds, "refresh_token", None):
            try:
                creds.refresh(_GoogleAuthRequest())
            except Exception as e:
                log.warning("Audit creds refresh failed for %s: %s", email, e)
                return None
        return build("sheets", "v4", credentials=creds, cache_discovery=False)

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


def _run_periodic_memory_sweeps() -> None:
    """Periodic memory hygiene driven from the audit flush loop.

    Collected here because the audit logger is the only always-on async task
    in the server. Each sweep is fail-soft so a broken one can't take down
    the loop.
    """
    try:
        from core.attachment_storage import get_attachment_storage
        removed = get_attachment_storage().cleanup_expired()
        if removed:
            log.info("Attachment cleanup removed %d expired files", removed)
    except Exception as e:
        log.debug("Attachment cleanup sweep failed: %s", e)
    try:
        from auth.oauth21_session_store import get_oauth21_session_store
        store = get_oauth21_session_store()
        # Cleanup expired OAuth states (already locked-and-locked-down by store).
        with store._lock:  # safe: same RLock used internally everywhere
            store._cleanup_expired_oauth_states_locked()
        # Sweep orphaned MCP→user mappings whose backing session was deleted
        # elsewhere; bounded work.
        store.cleanup_orphaned_mappings()
    except Exception as e:
        log.debug("OAuth session sweep failed: %s", e)


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
                err = f"{_origin_error_type(e)}: {str(e)[:300]}"
                raise
            finally:
                try:
                    # Extract resource_id eagerly so we can drop the full
                    # response body before yielding to the queue submit.
                    # Sheets calls with includeValuesInResponse=True can echo
                    # back many KB of data that we'd otherwise pin in the
                    # `wrap` frame across the queue put.
                    resource_id = _resource_id(result, kwargs)
                    result = None
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
                        "resource_id": resource_id,
                        "status": status,
                        "error": err,
                        "latency_ms": int((time.perf_counter() - t0) * 1000),
                    })
                except Exception as ae:
                    log.error("Audit submit failed (non-fatal): %s", ae)
        return wrap
    return deco

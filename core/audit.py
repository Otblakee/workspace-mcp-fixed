"""OTB Workspace MCP — audit logger.
Async, buffered, fail-soft. Logger errors never break tool calls."""

import asyncio, base64, functools, json, logging, os, time
from datetime import datetime, timezone
from typing import Any, Callable

from google.oauth2 import service_account
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

AUDIT_SHEET_ID = os.environ.get("AUDIT_SHEET_ID", "")
AUDIT_SA_JSON_B64 = os.environ.get("AUDIT_SA_JSON_B64", "")
FLUSH_S = int(os.environ.get("AUDIT_FLUSH_INTERVAL_S", "30"))
BATCH = int(os.environ.get("AUDIT_BATCH_SIZE", "50"))
ENABLED = bool(AUDIT_SHEET_ID and AUDIT_SA_JSON_B64)

HEADERS = ["timestamp_utc","user","service","tool","params_summary",
           "resource_id","status","error","latency_ms"]

SERVICE_MAP = {"drive":"drive","gmail":"gmail","calendar":"calendar",
    "event":"calendar","doc":"docs","comment":"docs","spreadsheet":"sheets",
    "sheet":"sheets","contact":"contacts","label":"gmail","filter":"gmail",
    "permission":"drive"}

SENSITIVE = {"body","text_content","message_body","html_body","values",
             "data","content","notes","description","subject","query"}

def _service(tool: str) -> str:
    n = tool.lower()
    for k, v in SERVICE_MAP.items():
        if k in n: return v
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
    for k in ("file_id","document_id","spreadsheet_id","message_id",
              "event_id","thread_id","calendar_id","resource_name"):
        if k in (kwargs or {}): return str(kwargs[k])[:200]
    if isinstance(result, dict):
        for k in ("id","fileId","documentId","spreadsheetId","messageId","resourceName"):
            if k in result: return str(result[k])[:200]
    return ""


class AuditLogger:
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self._task = None
        self._sheets = None
        self._current_tab = None

    def _client(self):
        sa = json.loads(base64.b64decode(AUDIT_SA_JSON_B64).decode())
        creds = service_account.Credentials.from_service_account_info(
            sa, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        return build("sheets", "v4", credentials=creds, cache_discovery=False)

    async def start(self):
        if not ENABLED:
            log.warning("Audit DISABLED — missing AUDIT_SHEET_ID or AUDIT_SA_JSON_B64")
            return
        self._sheets = self._client()
        self._task = asyncio.create_task(self._loop())
        log.info("Audit started → sheet %s", AUDIT_SHEET_ID)

    def submit(self, entry: dict):
        if not ENABLED: return
        try: self.q.put_nowait(entry)
        except asyncio.QueueFull:
            log.error("AUDIT_DROP %s", json.dumps(entry))

    async def _loop(self):
        while True:
            await asyncio.sleep(FLUSH_S)
            batch = []
            while not self.q.empty() and len(batch) < BATCH:
                batch.append(self.q.get_nowait())
            if not batch: continue
            try:
                await self._flush(batch)
            except Exception as e:
                log.error("Audit flush failed (%s) — falling back to stdout", e)
                for entry in batch:
                    log.error("AUDIT_FALLBACK %s", json.dumps(entry))

    async def _flush(self, batch):
        tab = datetime.now(timezone.utc).strftime("%Y-%m")
        if tab != self._current_tab:
            await asyncio.to_thread(self._ensure_tab, tab)
            self._current_tab = tab
        rows = [[e.get(h, "") for h in HEADERS] for e in batch]
        await asyncio.to_thread(
            self._sheets.spreadsheets().values().append(
                spreadsheetId=AUDIT_SHEET_ID,
                range=f"'{tab}'!A:I",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows}).execute)

    def _ensure_tab(self, tab):
        meta = self._sheets.spreadsheets().get(spreadsheetId=AUDIT_SHEET_ID).execute()
        if tab not in {s["properties"]["title"] for s in meta.get("sheets", [])}:
            add_resp = self._sheets.spreadsheets().batchUpdate(
                spreadsheetId=AUDIT_SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": tab}}}]}).execute()
            self._sheets.spreadsheets().values().update(
                spreadsheetId=AUDIT_SHEET_ID,
                range=f"'{tab}'!A1:I1",
                valueInputOption="RAW",
                body={"values": [HEADERS]}).execute()
            try:
                new_sheet_id = add_resp["replies"][0]["addSheet"]["properties"]["sheetId"]
                self._apply_tab_formatting(new_sheet_id)
            except Exception as e:
                log.warning("Audit tab '%s' created without conditional formatting: %s", tab, e)

    def _apply_tab_formatting(self, sheet_id):
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
        self._sheets.spreadsheets().batchUpdate(
            spreadsheetId=AUDIT_SHEET_ID,
            body={"requests": requests}).execute()


_inst: AuditLogger | None = None
def logger() -> AuditLogger:
    global _inst
    if _inst is None: _inst = AuditLogger()
    return _inst


def audit_log(user_resolver: Callable[[], str] | None = None):
    """Wrap a tool function. Logs success/error to the audit Sheet via async queue."""
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
                    logger().submit({
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "user": (user_resolver() if user_resolver else os.environ.get("DEFAULT_USER","oli")),
                        "service": _service(fn.__name__),
                        "tool": fn.__name__,
                        "params_summary": _redact(kwargs),
                        "resource_id": _resource_id(result, kwargs),
                        "status": status,
                        "error": err,
                        "latency_ms": int((time.perf_counter()-t0)*1000),
                    })
                except Exception as ae:
                    log.error("Audit submit failed (non-fatal): %s", ae)
        return wrap
    return deco

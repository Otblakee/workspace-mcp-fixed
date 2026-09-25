"""
The signature ledger: a Google Sheet recording every signature apply.

Why a ledger. Gmail sanitises the HTML signature on save, so the stored
signature is never byte-identical to the template output. Drift cannot be
judged by comparing the template with what Gmail returns. Instead, right
after each apply the tool reads the signature back, hashes it, and records
that hash together with the template and statutory versions applied. Later
audits compare Gmail's current hash with the recorded read-back hash and the
pinned versions with the recorded ones. The engine's ``drift_status`` does
the comparison; this module only stores and retrieves the rows.

Sheet layout:

* ``Ledger`` tab, append-only, one row per (apply, send-as address), columns
  in ``LEDGER_HEADER`` order. ``applied_at`` is an ISO-8601 UTC timestamp so
  "latest" is a plain string comparison.
* One audit tab per audit run (name chosen by the caller), rewritten in full
  each time, columns in ``AUDIT_HEADER`` order.

Sheets API facts this module relies on (Sheets v4):

* ``spreadsheets.get`` with ``fields=sheets.properties.title`` lists tabs.
* ``spreadsheets.batchUpdate`` with an ``addSheet`` request creates a tab.
* ``values.append`` with ``valueInputOption=RAW`` stores strings verbatim (no
  formula or number parsing, so a hash that looks numeric stays a string) and
  ``insertDataOption=INSERT_ROWS`` adds rows after the last row of the table.
* ``values.get`` omits ``values`` entirely for an empty range and returns
  short rows when trailing cells are empty, so both are tolerated on read.
* Tab names in A1 notation are single-quoted, with a literal quote doubled.

All calls go through ``gdrive.drive_batch.execute_with_backoff``: the Sheets
API raises ``HttpError`` in the same shape as Drive. ``values.append`` is the
one non-idempotent call (a replay after an ambiguous failure would duplicate
the row), so it is declared as such and only retried on definite rejections.

The Sheet is written as the service account itself (share it with the
service account's address as an Editor); no user is impersonated for it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from gdrive.drive_batch import execute_with_backoff

from gsignatures.sa_auth import ENV_LEDGER_SHEET_ID

logger = logging.getLogger(__name__)

LEDGER_TAB = "Ledger"

LEDGER_HEADER: List[str] = [
    "applied_at",
    "actor",
    "user_email",
    "send_as_email",
    "entity",
    "template_version",
    "statutory_version",
    "rendered_hash",
    "readback_hash",
    "previous_hash",
    "previous_signature_html",
    "run_id",
]

AUDIT_HEADER: List[str] = [
    "audited_at",
    "user_email",
    "send_as_email",
    "entity",
    "expected_template_version",
    "expected_statutory_version",
    "ledger_template_version",
    "ledger_statutory_version",
    "status",
    "reason",
    "current_hash",
]

# A ledger row without these cannot be attributed to an apply. Refused
# before anything is sent.
_LEDGER_REQUIRED = ("applied_at", "user_email", "send_as_email")

# Column letter of the last ledger column (A:L for twelve columns).
_LEDGER_LAST_COLUMN = chr(ord("A") + len(LEDGER_HEADER) - 1)


class LedgerError(Exception):
    """The ledger is not configured, or the Sheet does not look like a ledger."""


# --- Configuration -----------------------------------------------------------


def ledger_sheet_id() -> str:
    """The ledger Sheet ID from ``SIGNATURE_LEDGER_SHEET_ID``."""
    value = os.environ.get(ENV_LEDGER_SHEET_ID, "").strip()
    if not value:
        raise LedgerError(
            f"{ENV_LEDGER_SHEET_ID} is not set. Create a Google Sheet for the "
            "signature ledger, share it as Editor with the service account's "
            "address (gsignatures.sa_auth.service_account_email()), and set "
            f"{ENV_LEDGER_SHEET_ID} to its ID. The Ledger tab is created on "
            "first write."
        )
    return value


# --- Small helpers -------------------------------------------------------------


def _a1(tab: str, cells: Optional[str] = None) -> str:
    """A1 range for ``tab`` (whole tab when ``cells`` is None)."""
    quoted = "'" + tab.replace("'", "''") + "'"
    return f"{quoted}!{cells}" if cells else quoted


def _cell(value: Any) -> str:
    """Sheets cell text: ``None`` becomes empty, everything else is ``str``."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _rows_to_values(
    rows: List[Dict[str, Any]], header: List[str], *, required: Tuple[str, ...] = ()
) -> List[List[str]]:
    """Order dict rows by ``header``, refusing unknown or missing keys."""
    allowed = set(header)
    values: List[List[str]] = []
    problems: List[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            problems.append(f"row {index}: expected a dict, got {type(row).__name__}")
            continue
        unknown = sorted(set(row) - allowed)
        if unknown:
            problems.append(f"row {index}: unknown keys {', '.join(unknown)}")
        missing = [k for k in required if not _cell(row.get(k)).strip()]
        if missing:
            problems.append(f"row {index}: missing {', '.join(missing)}")
        values.append([_cell(row.get(key)) for key in header])
    if problems:
        raise LedgerError("Refusing to write ledger rows: " + "; ".join(problems))
    return values


def _check_sheet_id(sheet_id: str) -> str:
    cleaned = (sheet_id or "").strip()
    if not cleaned:
        raise LedgerError("A spreadsheet ID is required.")
    return cleaned


def _check_tab(tab: str) -> str:
    cleaned = (tab or "").strip()
    if not cleaned:
        raise LedgerError("A tab name is required.")
    return cleaned


def _check_header(header: List[str]) -> List[str]:
    if not header or any(not str(h).strip() for h in header):
        raise LedgerError("A header with no blank column names is required.")
    return [str(h) for h in header]


async def _tab_titles(sheets, sheet_id: str) -> List[str]:
    meta = await execute_with_backoff(
        lambda: sheets.spreadsheets().get(
            spreadsheetId=sheet_id, fields="sheets.properties.title"
        ),
        label="sheets-get",
    )
    return [
        str((sheet.get("properties") or {}).get("title") or "")
        for sheet in (meta.get("sheets") or [])
    ]


async def _read_values(sheets, sheet_id: str, a1_range: str) -> List[List[Any]]:
    response = await execute_with_backoff(
        lambda: (
            sheets.spreadsheets().values().get(spreadsheetId=sheet_id, range=a1_range)
        ),
        label="sheets-values-get",
    )
    return [list(row) for row in (response.get("values") or [])]


async def _write_values(sheets, sheet_id: str, a1_range: str, values) -> None:
    await execute_with_backoff(
        lambda: (
            sheets.spreadsheets()
            .values()
            .update(
                spreadsheetId=sheet_id,
                range=a1_range,
                valueInputOption="RAW",
                body={"values": values},
            )
        ),
        label="sheets-values-update",
    )


# --- Tabs -------------------------------------------------------------------


async def ensure_tab(sheets, sheet_id: str, tab: str, header: List[str]) -> None:
    """Make sure ``tab`` exists with ``header`` in row 1.

    Missing tab: create it and write the header. Existing tab with an empty
    first row: write the header. Existing tab whose first row already is
    ``header``: nothing. Existing tab with a different first row: refuse,
    because appending to it would misalign every column. Data below the
    header is never touched.
    """
    sheet_id = _check_sheet_id(sheet_id)
    tab = _check_tab(tab)
    header = _check_header(header)

    titles = await _tab_titles(sheets, sheet_id)
    header_range = _a1(tab, "1:1")

    if tab not in titles:
        await execute_with_backoff(
            lambda: sheets.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
            ),
            label="sheets-add-tab",
            idempotent=False,
        )
        await _write_values(sheets, sheet_id, header_range, [header])
        logger.info("created ledger tab %r with %d columns", tab, len(header))
        return

    existing = await _read_values(sheets, sheet_id, header_range)
    first_row = [_cell(v).strip() for v in (existing[0] if existing else [])]
    if not any(first_row):
        await _write_values(sheets, sheet_id, header_range, [header])
        return
    if first_row[: len(header)] != header or any(first_row[len(header) :]):
        raise LedgerError(
            f"Tab {tab!r} exists but its header row does not match the expected "
            f"columns. Expected {header}; found {first_row}. Fix or rename the "
            "tab by hand; this tool will not overwrite it."
        )


# --- Ledger writes and reads -----------------------------------------------


async def append_ledger_rows(sheets, sheet_id: str, rows: List[Dict[str, Any]]) -> int:
    """Append ``rows`` to the Ledger tab in ``LEDGER_HEADER`` order.

    Returns the number of rows written (0 for an empty list, with no API
    call). Every row is validated before the single ``values.append`` is
    sent, so a bad row means nothing is written.
    """
    sheet_id = _check_sheet_id(sheet_id)
    if not rows:
        return 0
    values = _rows_to_values(rows, LEDGER_HEADER, required=_LEDGER_REQUIRED)
    target = _a1(LEDGER_TAB, f"A:{_LEDGER_LAST_COLUMN}")
    await execute_with_backoff(
        lambda: (
            sheets.spreadsheets()
            .values()
            .append(
                spreadsheetId=sheet_id,
                range=target,
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            )
        ),
        label="ledger-append",
        idempotent=False,
    )
    return len(values)


async def ledger_tab_exists(sheets, sheet_id: str) -> bool:
    """Whether the Sheet has a ``Ledger`` tab at all.

    A Sheet that was shared but never written to has no such tab, and
    ``values.get`` on a missing tab is a 400 from the API. Callers that read
    the ledger for information treat "no tab" as an empty ledger.
    """
    sheet_id = _check_sheet_id(sheet_id)
    return LEDGER_TAB in await _tab_titles(sheets, sheet_id)


async def assert_tab_writable(
    sheets, sheet_id: str, tab: str, header: List[str]
) -> None:
    """Prove the tab can be written by re-writing its header row unchanged.

    ``ensure_tab`` only writes when the tab or its header is missing, so on a
    Sheet shared as Viewer the steady state (tab present, header right) would
    pass every read and only fail at the first append, after the Gmail write.
    Writing the identical header back changes nothing in the Sheet and fails
    with the API's 403 when the share is read-only. Call it only on a path
    that is about to write.
    """
    sheet_id = _check_sheet_id(sheet_id)
    tab = _check_tab(tab)
    header = _check_header(header)
    await _write_values(sheets, sheet_id, _a1(tab, "1:1"), [header])


def ledger_key(record: Dict[str, Any]) -> Tuple[str, str]:
    """The ``(user_email, send_as_email)`` key of a ledger record, lower-cased."""
    return (
        _cell(record.get("user_email")).strip().lower(),
        _cell(record.get("send_as_email")).strip().lower(),
    )


async def read_ledger_rows(sheets, sheet_id: str) -> List[Dict[str, str]]:
    """Every attributable ledger row as a dict, in sheet order.

    Short rows are padded with empty strings and blank rows are ignored. A
    row without a user or send-as address is skipped with a warning that
    names only its row number, ``applied_at`` and ``run_id``: never the
    previous signature HTML, which carries a person's details. An empty
    sheet, or a header with no data, gives ``[]``. A first row that is not
    the ledger header is refused rather than misread.
    """
    sheet_id = _check_sheet_id(sheet_id)
    rows = await _read_values(
        sheets, sheet_id, _a1(LEDGER_TAB, f"A:{_LEDGER_LAST_COLUMN}")
    )
    if not rows:
        return []

    header = [_cell(v).strip() for v in rows[0]]
    if header[: len(LEDGER_HEADER)] != LEDGER_HEADER:
        raise LedgerError(
            f"The {LEDGER_TAB!r} tab does not start with the ledger header. "
            f"Expected {LEDGER_HEADER}; found {header}."
        )

    records: List[Dict[str, str]] = []
    width = len(LEDGER_HEADER)
    for index, raw in enumerate(rows[1:], start=2):
        cells = [_cell(v) for v in raw[:width]]
        if not any(c.strip() for c in cells):
            continue
        cells += [""] * (width - len(cells))
        record = dict(zip(LEDGER_HEADER, cells))
        user_key, send_as_key = ledger_key(record)
        if not user_key or not send_as_key:
            logger.warning(
                "ledger row %d skipped: no user/send-as (applied_at=%r run_id=%r)",
                index,
                record["applied_at"],
                record["run_id"],
            )
            continue
        records.append(record)
    return records


async def read_ledger_latest(
    sheets, sheet_id: str
) -> Dict[Tuple[str, str], Dict[str, str]]:
    """Latest ledger row per ``(user_email, send_as_email)``, both lower-cased.

    "Latest" is the greatest ``applied_at`` string (ISO-8601 UTC compares
    lexically); on a tie the later row in the sheet wins. Rows come from
    ``read_ledger_rows`` and share its tolerance and refusals.
    """
    latest: Dict[Tuple[str, str], Dict[str, str]] = {}
    for record in await read_ledger_rows(sheets, sheet_id):
        key = ledger_key(record)
        current = latest.get(key)
        if current is None or record["applied_at"] >= current["applied_at"]:
            latest[key] = record
    return latest


# --- Audit reports -------------------------------------------------------------


async def write_audit_report(
    sheets, sheet_id: str, tab_name: str, rows: List[Dict[str, Any]]
) -> None:
    """Replace ``tab_name`` with the audit header plus ``rows``.

    Rows are validated (``AUDIT_HEADER`` keys only) before the tab is
    touched, then the tab is ensured, cleared and rewritten in one
    ``values.update``. Missing keys become empty cells; status and reason
    come from the engine's ``drift_status``.
    """
    sheet_id = _check_sheet_id(sheet_id)
    tab_name = _check_tab(tab_name)
    values = _rows_to_values(rows, AUDIT_HEADER)

    await ensure_tab(sheets, sheet_id, tab_name, AUDIT_HEADER)
    await execute_with_backoff(
        lambda: (
            sheets.spreadsheets()
            .values()
            .clear(spreadsheetId=sheet_id, range=_a1(tab_name), body={})
        ),
        label="audit-clear",
    )
    await _write_values(sheets, sheet_id, _a1(tab_name, "A1"), [AUDIT_HEADER] + values)

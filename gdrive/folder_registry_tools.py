"""
Folder Registry hygiene: find and archive the noise rows that clog it.

The Folder Registry (Sheet tab ``FolderRegistry``) is the curated list of
folders OTB has decided exist. It is read whole, on every call, by
``rebuild_hub`` and the shared-drive banner tools. In November 2026 a git
checkout synced into a JIT working folder put hundreds of ``.git`` rows into
it through the Apps Script scanner, and every lookup slowed down.

Two tools, one rule set:

* ``audit_folder_registry`` is read-only. It reports row counts per drive, a
  depth histogram, the rows the noise rules match, the parents with more
  children than a registry should carry, and duplicated folder IDs. The hit
  list goes to a JSONL report in the attachment store.
* ``prune_folder_registry`` archives matched rows to a ``RegistryArchive``
  tab and only then deletes them from the registry tab, in one batch. Dry
  run by default; a live run needs ``dry_run=False`` and ``confirm=True``.
  Nothing is pruned that the archive does not already hold, and a row whose
  content changed between the read and the delete aborts the whole delete.

The noise rules live in ``gdrive.drive_batch`` (``EXCLUDED_PATH_SEGMENTS``,
``excluded_segment``) and are the same ones ``walk_drive`` applies, so the
scanner, the inventory and the registry agree on what noise is.

Rows at depth 0 (drive roots) and depth 1 (top-level folders) are never
matched, whatever they are called. Rows carrying an ``exclude`` flag or an
``action`` of skip/none/ignore are a deliberate opt-out and are never pruned.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from auth.service_decorator import require_google_service
from core.server import server
from core.utils import UserInputError, handle_http_errors
from gdrive.drive_batch import (
    DEFAULT_REGISTRY_MAX_DEPTH,
    excluded_segment,
    execute_with_backoff,
    path_segments,
    summarise_counts,
    write_jsonl_report,
)
from gdrive.drive_migration_tools import (
    _registry_column,
    registry_header_keys,
    registry_rows_from_values,
)

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_RANGE = "FolderRegistry"
DEFAULT_ARCHIVE_TAB = "RegistryArchive"

# A parent with more registered children than this is almost certainly a
# synced working folder or an unmodelled tree, not architecture.
DEFAULT_CHILDREN_BUDGET = 200

# A single prune run above this many rows is refused. Raise it on purpose;
# the point is that a rule misfire cannot empty the registry in one call.
DEFAULT_MAX_PRUNE_ROWS = 5_000

# Rows at or below this depth are never noise. 0 = drive root, 1 = top-level.
PROTECTED_MAX_DEPTH = 1

ARCHIVE_EXTRA_COLUMNS = ["archived_at", "archived_by", "archive_reason", "source_row"]

LIVE_CONFIRM_MESSAGE = (
    "prune_folder_registry is a dry run by default. A live prune needs BOTH "
    "dry_run=False AND confirm=True. Run it as a dry run first and check the "
    "report, then pass confirm=True."
)

_TRUTHY = frozenset({"true", "yes", "y", "1", "x"})
_SKIP_ACTIONS = frozenset({"skip", "none", "ignore", "exclude"})

_RANGE_RE = re.compile(
    r"^\s*(?:'(?P<quoted>(?:[^']|'')+)'|(?P<bare>[^!]+?))\s*(?:!(?P<cells>.+))?\s*$"
)
_START_ROW_RE = re.compile(r"^[A-Za-z]*(?P<row>\d+)")


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without FastMCP or Google)
# ---------------------------------------------------------------------------


def split_registry_range(sheet_range: str) -> Tuple[str, int]:
    """Split an A1 range into ``(tab_title, start_row)``.

    ``"FolderRegistry"`` → ``("FolderRegistry", 1)``;
    ``"'Folder Registry'!A1:Z"`` → ``("Folder Registry", 1)``;
    ``"Tab!A3:Z"`` → ``("Tab", 3)``; ``"Tab!A:Z"`` → ``("Tab", 1)``.
    ``start_row`` is the 1-based sheet row holding the header.
    """
    match = _RANGE_RE.match(sheet_range or "")
    if not match:
        raise UserInputError(f"Cannot parse registry_range {sheet_range!r}.")
    tab = match.group("quoted")
    if tab is not None:
        tab = tab.replace("''", "'")
    else:
        tab = (match.group("bare") or "").strip()
    if not tab:
        raise UserInputError(f"registry_range {sheet_range!r} names no tab.")
    cells = (match.group("cells") or "").strip()
    start_row = 1
    if cells:
        first = cells.split(":", 1)[0]
        row_match = _START_ROW_RE.match(first)
        if row_match and row_match.group("row"):
            start_row = int(row_match.group("row"))
    return tab, start_row


def _a1_tab(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def row_is_opted_out(row: Dict[str, str]) -> bool:
    """True when the row carries the deliberate exclude marker."""
    if _truthy(row.get("exclude")) or _truthy(row.get("excluded")):
        return True
    return str(row.get("action") or "").strip().lower() in _SKIP_ACTIONS


def registry_depth(row: Dict[str, str], path: str) -> int:
    """Depth of a registry row: the ``depth`` column when numeric, else from
    the path with the leading drive-name segment not counted."""
    raw = str(row.get("depth") or "").strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    segments = path_segments(path)
    drive = str(row.get("drive") or "").strip()
    if segments and drive and segments[0] == drive:
        return len(segments) - 1
    return len(segments)


def _path_without_drive(row: Dict[str, str], path: str) -> str:
    segments = path_segments(path)
    drive = str(row.get("drive") or "").strip()
    if segments and drive and segments[0] == drive:
        segments = segments[1:]
    return "/".join(segments)


@dataclass
class RegistryHit:
    sheet_row: int
    drive: str
    path: str
    folder_id: str
    depth: int
    reason: str
    segment: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "sheet_row": self.sheet_row,
            "drive": self.drive,
            "path": self.path,
            "folder_id": self.folder_id,
            "depth": self.depth,
            "reason": self.reason,
            "segment": self.segment,
        }


@dataclass
class RegistryAudit:
    total_rows: int
    blank_rows: int
    rows_per_drive: Dict[str, int]
    depth_histogram: Dict[int, int]
    noise_hits: List[RegistryHit]
    depth_hits: List[RegistryHit]
    opted_out_rows: int
    missing_path_rows: int
    wide_parents: List[Tuple[str, int]]
    duplicate_folder_ids: Dict[str, List[int]]
    path_column: str
    id_column: Optional[str]
    warnings: List[str] = field(default_factory=list)

    @property
    def hits(self) -> List[RegistryHit]:
        return self.noise_hits + self.depth_hits


def classify_registry_rows(
    rows: Sequence[Dict[str, str]],
    *,
    start_row: int = 1,
    max_depth: int = DEFAULT_REGISTRY_MAX_DEPTH,
    extra_segments: Optional[Sequence[str]] = None,
    children_budget: int = DEFAULT_CHILDREN_BUDGET,
) -> RegistryAudit:
    """Apply the noise rules to registry rows and gather the statistics.

    ``start_row`` is the sheet row of the header, so data row ``i`` (0-based)
    sits at sheet row ``start_row + 1 + i``.
    """
    path_column = _registry_column(rows, ("path", "folder_path"))
    if not path_column:
        raise UserInputError(
            "Registry has no path column (looked for 'path' and 'folder_path'); "
            "the noise rules work on the folder path."
        )
    id_column = _registry_column(rows, ("folder_id", "id", "drive_folder_id"))

    rows_per_drive: Counter = Counter()
    depth_histogram: Counter = Counter()
    children_per_parent: Counter = Counter()
    ids_to_rows: Dict[str, List[int]] = {}
    noise_hits: List[RegistryHit] = []
    depth_hits: List[RegistryHit] = []
    opted_out = 0
    missing_path = 0
    blank = 0

    for index, row in enumerate(rows):
        sheet_row = start_row + 1 + index
        if not any(str(v).strip() for v in row.values()):
            blank += 1
            continue
        path = str(row.get(path_column) or "").strip().strip("/")
        drive = str(row.get("drive") or "").strip() or "(no drive)"
        folder_id = str(row.get(id_column) or "").strip() if id_column else ""
        rows_per_drive[drive] += 1
        if folder_id:
            ids_to_rows.setdefault(folder_id, []).append(sheet_row)
        if not path:
            missing_path += 1
            continue

        depth = registry_depth(row, path)
        depth_histogram[depth] += 1
        relative = _path_without_drive(row, path)
        parent = "/".join(path_segments(path)[:-1])
        if parent:
            children_per_parent[parent] += 1

        if depth <= PROTECTED_MAX_DEPTH:
            continue
        if row_is_opted_out(row):
            opted_out += 1
            continue

        segment = excluded_segment(relative, extra_segments)
        if segment:
            noise_hits.append(
                RegistryHit(
                    sheet_row,
                    drive,
                    path,
                    folder_id,
                    depth,
                    f"noise_segment:{segment}",
                    segment,
                )
            )
        elif depth > max_depth:
            depth_hits.append(
                RegistryHit(
                    sheet_row, drive, path, folder_id, depth, f"over_depth:{depth}"
                )
            )

    wide = sorted(
        (
            (parent, n)
            for parent, n in children_per_parent.items()
            if n > children_budget
        ),
        key=lambda item: (-item[1], item[0]),
    )
    duplicates = {fid: srows for fid, srows in ids_to_rows.items() if len(srows) > 1}

    return RegistryAudit(
        total_rows=len(rows),
        blank_rows=blank,
        rows_per_drive=dict(rows_per_drive),
        depth_histogram=dict(sorted(depth_histogram.items())),
        noise_hits=noise_hits,
        depth_hits=depth_hits,
        opted_out_rows=opted_out,
        missing_path_rows=missing_path,
        wide_parents=wide,
        duplicate_folder_ids=duplicates,
        path_column=path_column,
        id_column=id_column,
    )


def contiguous_row_ranges(sheet_rows: Sequence[int]) -> List[Tuple[int, int]]:
    """Collapse 1-based sheet rows into inclusive ``(first, last)`` ranges,
    sorted descending so each delete leaves the earlier rows where they are."""
    ordered = sorted(set(int(r) for r in sheet_rows))
    ranges: List[Tuple[int, int]] = []
    for row in ordered:
        if ranges and ranges[-1][1] == row - 1:
            ranges[-1] = (ranges[-1][0], row)
        else:
            ranges.append((row, row))
    return sorted(ranges, reverse=True)


def delete_rows_requests(
    sheet_id: int, sheet_rows: Sequence[int]
) -> List[Dict[str, Any]]:
    """``deleteDimension`` requests for ``sheet_rows``, bottom-up."""
    return [
        {
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": first - 1,
                    "endIndex": last,
                }
            }
        }
        for first, last in contiguous_row_ranges(sheet_rows)
    ]


def _clean_extra_segments(exclude_names: Optional[Sequence[str]]) -> List[str]:
    cleaned: List[str] = []
    for name in exclude_names or []:
        value = str(name or "").strip()
        if not value:
            continue
        if "/" in value:
            raise UserInputError(
                f"exclude_names entries are single folder names, not paths: {value!r}"
            )
        cleaned.append(value)
    return cleaned


def _summary_lines(
    audit: RegistryAudit, *, max_depth: int, children_budget: int
) -> List[str]:
    counts = {
        "rows_total": audit.total_rows,
        "rows_blank": audit.blank_rows,
        "rows_missing_path": audit.missing_path_rows,
        "rows_opted_out": audit.opted_out_rows,
        "noise_rows": len(audit.noise_hits),
        "over_depth_rows": len(audit.depth_hits),
        "wide_parents": len(audit.wide_parents),
        "duplicate_folder_ids": len(audit.duplicate_folder_ids),
    }
    lines = [summarise_counts(counts)]
    if audit.rows_per_drive:
        per_drive = sorted(audit.rows_per_drive.items(), key=lambda kv: (-kv[1], kv[0]))
        lines.append(
            "Rows per drive: " + ", ".join(f"{d}={n}" for d, n in per_drive[:15])
        )
        if len(per_drive) > 15:
            lines.append(f"   … and {len(per_drive) - 15} more drive(s)")
    if audit.depth_histogram:
        lines.append(
            "Depth histogram: "
            + ", ".join(f"d{d}={n}" for d, n in audit.depth_histogram.items())
            + f" (max_depth={max_depth})"
        )
    if audit.noise_hits:
        by_segment = Counter(h.segment for h in audit.noise_hits)
        lines.append(
            "Noise segments: "
            + ", ".join(f"{seg}={n}" for seg, n in by_segment.most_common(10))
        )
    if audit.wide_parents:
        lines.append(f"Parents over the {children_budget}-child budget:")
        for parent, n in audit.wide_parents[:10]:
            lines.append(f"   • {parent}: {n} children")
    if audit.duplicate_folder_ids:
        lines.append("Folder IDs registered more than once (rows):")
        for fid, srows in list(audit.duplicate_folder_ids.items())[:10]:
            lines.append(f"   • {fid}: rows {srows}")
    return lines


# ---------------------------------------------------------------------------
# Sheets I/O
# ---------------------------------------------------------------------------


async def _read_registry_values(
    sheets_service, spreadsheet_id: str, sheet_range: str
) -> List[List[Any]]:
    response = await execute_with_backoff(
        lambda: (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=sheet_range)
        ),
        label="sheets.values.get(registry)",
    )
    values = response.get("values") or []
    if not values:
        raise UserInputError(
            f"Registry range '{sheet_range}' in {spreadsheet_id} is empty."
        )
    return [list(row) for row in values]


async def _sheet_ids_by_title(sheets_service, spreadsheet_id: str) -> Dict[str, int]:
    meta = await execute_with_backoff(
        lambda: sheets_service.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets.properties(sheetId,title)"
        ),
        label="sheets.get(tabs)",
    )
    return {
        s["properties"]["title"]: int(s["properties"]["sheetId"])
        for s in meta.get("sheets", [])
        if "properties" in s
    }


async def _ensure_archive_tab(
    sheets_service,
    spreadsheet_id: str,
    tab: str,
    header: List[str],
    titles: Dict[str, int],
) -> None:
    """Create ``tab`` with ``header`` or verify an existing one matches it."""
    header_range = f"{_a1_tab(tab)}!1:1"
    if tab not in titles:
        await execute_with_backoff(
            lambda: sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
            ),
            label="sheets.batchUpdate(addSheet archive)",
            idempotent=False,
        )
        await execute_with_backoff(
            lambda: (
                sheets_service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=spreadsheet_id,
                    range=header_range,
                    valueInputOption="RAW",
                    body={"values": [header]},
                )
            ),
            label="sheets.values.update(archive header)",
            idempotent=False,
        )
        logger.info("[prune_folder_registry] created archive tab %r", tab)
        return
    existing = await execute_with_backoff(
        lambda: (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=header_range)
        ),
        label="sheets.values.get(archive header)",
    )
    first_row = [str(v).strip() for v in ((existing.get("values") or [[]])[0])]
    if not any(first_row):
        await execute_with_backoff(
            lambda: (
                sheets_service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=spreadsheet_id,
                    range=header_range,
                    valueInputOption="RAW",
                    body={"values": [header]},
                )
            ),
            label="sheets.values.update(archive header)",
            idempotent=False,
        )
        return
    if first_row[: len(header)] != header or any(first_row[len(header) :]):
        raise UserInputError(
            f"Archive tab {tab!r} exists but its header does not match the "
            f"registry's columns plus {ARCHIVE_EXTRA_COLUMNS}. Expected {header}; "
            f"found {first_row}. Rename that tab or pass a different archive_tab; "
            "this tool will not append into a mismatched sheet."
        )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@server.tool()
@handle_http_errors("audit_folder_registry", is_read_only=True, service_type="sheets")
@require_google_service("sheets", "sheets_read")
async def audit_folder_registry(
    service,
    user_google_email: str,
    registry_spreadsheet_id: str,
    registry_range: str = DEFAULT_REGISTRY_RANGE,
    max_depth: int = DEFAULT_REGISTRY_MAX_DEPTH,
    exclude_names: Optional[List[str]] = None,
    children_budget: int = DEFAULT_CHILDREN_BUDGET,
) -> str:
    """
    Reports what is clogging the Folder Registry. Read-only.

    Applies the same noise rules as ``walk_drive`` (``.git``, ``node_modules``,
    ``.venv``, ``__pycache__``, ``dist``, ``build``, …, and any dot-prefixed
    name) to every row's path, counts rows per drive and per depth, lists
    parents with more children than a registry should carry, and lists folder
    IDs registered more than once. Rows at depth 0 and 1 are never matched;
    rows with ``exclude=TRUE`` or ``action=skip`` are counted as opted out.

    The matched rows are written as JSONL (one object per row: sheet_row,
    drive, path, folder_id, depth, reason) to the attachment store so
    ``prune_folder_registry`` can be judged before it runs.

    Args:
        user_google_email (str): The user's Google email address. Required.
        registry_spreadsheet_id (str): Spreadsheet ID of the Folder Registry.
        registry_range (str): Tab name or A1 range; the first row is the
            header. Defaults to "FolderRegistry".
        max_depth (int): Rows deeper than this are reported as over depth.
            Drive root is 0. Defaults to 4.
        exclude_names (Optional[List[str]]): Extra folder names to treat as
            noise, on top of the defaults.
        children_budget (int): A parent with more registered children than
            this is reported. Defaults to 200.

    Returns:
        str: Counts, the per-drive and depth breakdown, and the report location.
    """
    if not registry_spreadsheet_id or not registry_spreadsheet_id.strip():
        raise UserInputError("registry_spreadsheet_id is required.")
    if max_depth < PROTECTED_MAX_DEPTH + 1:
        raise UserInputError(f"max_depth must be at least {PROTECTED_MAX_DEPTH + 1}.")
    if children_budget < 1:
        raise UserInputError("children_budget must be at least 1.")
    extra = _clean_extra_segments(exclude_names)
    _, start_row = split_registry_range(registry_range)

    values = await _read_registry_values(
        service, registry_spreadsheet_id, registry_range
    )
    rows = registry_rows_from_values(values)
    audit = classify_registry_rows(
        rows,
        start_row=start_row,
        max_depth=max_depth,
        extra_segments=extra,
        children_budget=children_budget,
    )

    _, path, access_line = write_jsonl_report(
        (h.as_dict() for h in audit.hits), filename="registry_audit.jsonl"
    )
    lines = [f"Folder Registry audit: {registry_range} in {registry_spreadsheet_id}"]
    lines.extend(
        _summary_lines(audit, max_depth=max_depth, children_budget=children_budget)
    )
    if audit.hits:
        lines.append(
            f"{len(audit.noise_hits)} noise row(s) and {len(audit.depth_hits)} "
            "over-depth row(s) would be archived by prune_folder_registry "
            "(over-depth rows only with prune_over_depth=True)."
        )
    else:
        lines.append("✅ No noise rows found.")
    lines.append(f"Hit list (JSONL): {access_line}")
    logger.info("[audit_folder_registry] wrote report to %s", path)
    return "\n".join(lines)


@server.tool()
@handle_http_errors("prune_folder_registry", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def prune_folder_registry(
    service,
    user_google_email: str,
    registry_spreadsheet_id: str,
    registry_range: str = DEFAULT_REGISTRY_RANGE,
    max_depth: int = DEFAULT_REGISTRY_MAX_DEPTH,
    exclude_names: Optional[List[str]] = None,
    prune_over_depth: bool = False,
    archive_tab: str = DEFAULT_ARCHIVE_TAB,
    max_rows: int = DEFAULT_MAX_PRUNE_ROWS,
    dry_run: bool = True,
    confirm: bool = False,
) -> str:
    """
    Archives the Folder Registry's noise rows to an archive tab, then removes
    them from the registry. Dry run by default.

    The hit list is recomputed from the sheet with the same rules as
    ``audit_folder_registry``; it is never taken from the caller, because row
    numbers shift as soon as anything is deleted. A live run:

    1. creates ``archive_tab`` if missing (registry header plus archived_at,
       archived_by, archive_reason, source_row), or refuses if an existing tab
       has a different header;
    2. appends every hit row to the archive and checks the row count Google
       reports back;
    3. re-reads the registry and checks each hit is still the same row
       (path and folder_id); any change aborts before anything is deleted;
    4. deletes the hit rows from the registry tab in one batchUpdate,
       bottom-up.

    Rows at depth 0 and 1, and rows with ``exclude=TRUE`` or ``action=skip``,
    are never pruned. Over-depth rows are pruned only with
    ``prune_over_depth=True``.

    Args:
        user_google_email (str): The user's Google email address. Required.
        registry_spreadsheet_id (str): Spreadsheet ID of the Folder Registry.
        registry_range (str): Tab name or A1 range. Defaults to "FolderRegistry".
        max_depth (int): Depth above which rows count as over depth. Defaults to 4.
        exclude_names (Optional[List[str]]): Extra folder names to treat as noise.
        prune_over_depth (bool): Also prune over-depth rows. Defaults to False.
        archive_tab (str): Tab the pruned rows are copied to first. Defaults
            to "RegistryArchive".
        max_rows (int): Refuse a run that would prune more rows than this.
            Defaults to 5000.
        dry_run (bool): Report the plan only. Defaults to True.
        confirm (bool): Required alongside dry_run=False for a live prune.

    Returns:
        str: The plan or the result, and the JSONL report location.
    """
    if not registry_spreadsheet_id or not registry_spreadsheet_id.strip():
        raise UserInputError("registry_spreadsheet_id is required.")
    if max_depth < PROTECTED_MAX_DEPTH + 1:
        raise UserInputError(f"max_depth must be at least {PROTECTED_MAX_DEPTH + 1}.")
    if max_rows < 1:
        raise UserInputError("max_rows must be at least 1.")
    if not archive_tab or not archive_tab.strip():
        raise UserInputError("archive_tab is required.")
    if not dry_run and not confirm:
        raise UserInputError(LIVE_CONFIRM_MESSAGE)
    extra = _clean_extra_segments(exclude_names)
    tab, start_row = split_registry_range(registry_range)
    archive_tab = archive_tab.strip()
    if archive_tab == tab:
        raise UserInputError("archive_tab must not be the registry tab itself.")

    values = await _read_registry_values(
        service, registry_spreadsheet_id, registry_range
    )
    raw_header = [str(h) for h in values[0]]
    rows = registry_rows_from_values(values)
    audit = classify_registry_rows(
        rows, start_row=start_row, max_depth=max_depth, extra_segments=extra
    )
    hits = list(audit.noise_hits)
    if prune_over_depth:
        hits.extend(audit.depth_hits)
    hits.sort(key=lambda h: h.sheet_row)

    mode = "DRY RUN" if dry_run else "LIVE"
    lines = [
        f"Folder Registry prune ({mode}): {registry_range} in {registry_spreadsheet_id}"
    ]
    lines.extend(
        _summary_lines(
            audit, max_depth=max_depth, children_budget=DEFAULT_CHILDREN_BUDGET
        )
    )

    if not hits:
        lines.append("✅ Nothing to prune.")
        if audit.depth_hits and not prune_over_depth:
            lines.append(
                f"ℹ️ {len(audit.depth_hits)} over-depth row(s) left alone; pass "
                "prune_over_depth=True to include them."
            )
        return "\n".join(lines)

    if len(hits) > max_rows:
        raise UserInputError(
            f"Refusing to prune {len(hits)} rows in one run (max_rows={max_rows}). "
            "Check the audit report; if the count is right, raise max_rows "
            "explicitly and run again."
        )

    _, report_path, access_line = write_jsonl_report(
        (h.as_dict() for h in hits), filename="registry_prune.jsonl"
    )

    if dry_run:
        lines.append(
            f"Would archive {len(hits)} row(s) to {archive_tab!r} and remove them "
            f"from {tab!r}. Re-run with dry_run=False and confirm=True to do it."
        )
        if audit.depth_hits and not prune_over_depth:
            lines.append(
                f"ℹ️ {len(audit.depth_hits)} over-depth row(s) left alone; pass "
                "prune_over_depth=True to include them."
            )
        lines.append(f"Plan (JSONL): {access_line}")
        return "\n".join(lines)

    # --- live ---------------------------------------------------------------
    titles = await _sheet_ids_by_title(service, registry_spreadsheet_id)
    if tab not in titles:
        raise UserInputError(
            f"Registry tab {tab!r} not found in {registry_spreadsheet_id}; "
            f"tabs present: {sorted(titles)}"
        )
    registry_sheet_id = titles[tab]
    archive_header = raw_header + ARCHIVE_EXTRA_COLUMNS
    await _ensure_archive_tab(
        service, registry_spreadsheet_id, archive_tab, archive_header, titles
    )

    archived_at = datetime.now(timezone.utc).isoformat()
    archive_values: List[List[Any]] = []
    for hit in hits:
        raw = values[hit.sheet_row - start_row]
        padded = [str(v) for v in raw] + [""] * (len(raw_header) - len(raw))
        archive_values.append(
            padded[: len(raw_header)]
            + [
                archived_at,
                user_google_email,
                f"prune_folder_registry:{hit.reason}",
                hit.sheet_row,
            ]
        )
    append_response = await execute_with_backoff(
        lambda: (
            service.spreadsheets()
            .values()
            .append(
                spreadsheetId=registry_spreadsheet_id,
                range=f"{_a1_tab(archive_tab)}!A:A",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": archive_values},
            )
        ),
        label="sheets.values.append(archive)",
        idempotent=False,
    )
    updated = int(
        ((append_response or {}).get("updates") or {}).get("updatedRows") or 0
    )
    if updated != len(archive_values):
        raise UserInputError(
            f"Archive append reported {updated} row(s) written but {len(archive_values)} "
            f"were sent; nothing has been removed from {tab!r}. Check the "
            f"{archive_tab!r} tab by hand before retrying."
        )

    # The archive is the evidence. Before deleting, prove the registry has not
    # moved under us: every hit must still hold the same path and folder_id.
    fresh_values = await _read_registry_values(
        service, registry_spreadsheet_id, registry_range
    )
    fresh_rows = registry_rows_from_values(fresh_values)
    fresh_keys = registry_header_keys(fresh_values[0])
    if fresh_keys != registry_header_keys(values[0]):
        raise UserInputError(
            f"The header of {tab!r} changed during the run; nothing was deleted. "
            f"{len(archive_values)} row(s) were already copied to {archive_tab!r}."
        )
    changed: List[str] = []
    for hit in hits:
        index = hit.sheet_row - start_row - 1
        current = fresh_rows[index] if 0 <= index < len(fresh_rows) else {}
        current_path = str(current.get(audit.path_column) or "").strip().strip("/")
        current_id = (
            str(current.get(audit.id_column) or "").strip() if audit.id_column else ""
        )
        if current_path != hit.path or current_id != hit.folder_id:
            changed.append(
                f"row {hit.sheet_row}: expected {hit.path!r}, found {current_path!r}"
            )
    if changed:
        shown = "; ".join(changed[:5])
        raise UserInputError(
            f"{len(changed)} registry row(s) changed between the read and the delete, "
            f"so nothing was deleted ({shown}). {len(archive_values)} row(s) were "
            f"already copied to {archive_tab!r}; re-run to recompute the plan."
        )

    requests = delete_rows_requests(registry_sheet_id, [h.sheet_row for h in hits])
    await execute_with_backoff(
        lambda: service.spreadsheets().batchUpdate(
            spreadsheetId=registry_spreadsheet_id, body={"requests": requests}
        ),
        label="sheets.batchUpdate(deleteDimension registry rows)",
        idempotent=False,
    )
    logger.info(
        "[prune_folder_registry] archived and removed %d row(s) from %r (%d delete ranges)",
        len(hits),
        tab,
        len(requests),
    )
    lines.append(
        f"✅ Archived {len(hits)} row(s) to {archive_tab!r} and removed them from "
        f"{tab!r} in {len(requests)} contiguous range(s). Restore by copying rows "
        f"back from {archive_tab!r} (source_row is the original row number)."
    )
    if audit.depth_hits and not prune_over_depth:
        lines.append(
            f"ℹ️ {len(audit.depth_hits)} over-depth row(s) left alone; pass "
            "prune_over_depth=True to include them."
        )
    lines.append(f"Removed rows (JSONL): {access_line}")
    logger.info("[prune_folder_registry] wrote report to %s", report_path)
    return "\n".join(lines)

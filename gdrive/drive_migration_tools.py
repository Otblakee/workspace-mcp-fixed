"""
Drive migration engine: inventory, folder trees, manifest-driven copy,
reconciliation, and the registry-driven hub rebuild.

These are the tools behind the BIR → OTB migration and the "never drifts"
navigation guarantee. Design constraints that drove the shapes here:

* **The previous crawl was lossy.** It missed six folders and a whole shared
  drive, so every count it produced is a floor. ``walk_drive`` therefore
  paginates exhaustively, then runs an independent drive-wide sweep and
  reconciles the two passes against each other before it reports a number.
* **Cross-tenant content cannot be moved**, only copied. Copies get new IDs and
  no version history, so provenance has to be stamped at copy time and
  verified afterwards — hence ``batch_copy_from_manifest`` +
  ``get_drive_file_metadata`` + ``reconcile_folders``.
* **Everything bulk is resumable.** Copy is idempotent on a provenance key, so
  a half-finished run is fixed by re-running the same manifest.

Reports are written as JSONL into the attachment store rather than returned
inline: a 40k-row inventory does not belong in an MCP tool result.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from auth.service_decorator import require_google_service
from core.server import server
from core.utils import UserInputError, handle_http_errors
from gdrive.drive_batch import (
    FOLDER_MIME_TYPE,
    SHORTCUT_MIME_TYPE,
    execute_with_backoff,
    paginate,
    parse_manifest,
    summarise_counts,
    write_jsonl_report,
)
from gdrive.drive_helpers import get_holding_folder_id, resolve_folder_id
from gdrive.shared_drive_tools import find_existing_shortcut

logger = logging.getLogger(__name__)

# Fields captured for every item in an inventory walk. md5/sha checksums are
# absent on native Google files by design — see NATIVE_GOOGLE_PREFIX below.
INVENTORY_FIELDS = (
    "id, name, mimeType, size, md5Checksum, sha1Checksum, sha256Checksum, "
    "owners(emailAddress), modifiedTime, createdTime, parents, driveId, "
    "trashed, shortcutDetails(targetId, targetMimeType)"
)

METADATA_FIELDS = (
    "id, name, mimeType, size, md5Checksum, sha1Checksum, sha256Checksum, "
    "properties, appProperties, parents, driveId, owners(emailAddress), "
    "createdTime, modifiedTime, version, webViewLink, trashed, "
    "shortcutDetails(targetId, targetMimeType)"
)

NATIVE_GOOGLE_PREFIX = "application/vnd.google-apps."

# Provenance keys. ``properties`` are user-visible in the Drive UI/API;
# ``appProperties`` are private to this app and carry the idempotency key that
# makes a re-run of the same manifest a no-op.
PROV_SOURCE_ID = "mcp_source_file_id"
PROV_BATCH = "mcp_migration_batch"
PROV_COPIED_AT = "mcp_copied_at"

# Guard against a runaway walk on a drive nobody has sized. Callers can raise
# it explicitly; the tool always says when a cap truncated the result.
DEFAULT_MAX_ITEMS = 50_000


def _escape_query_value(value: str) -> str:
    """Escape a literal for use inside a single-quoted Drive query term."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def is_native_google_file(mime_type: Optional[str]) -> bool:
    """Native Google files (Docs/Sheets/Slides/…) expose no checksum or size."""
    return bool(mime_type) and mime_type.startswith(NATIVE_GOOGLE_PREFIX)


async def _list_children(
    service, folder_id: str, *, include_trashed: bool, fields: str = INVENTORY_FIELDS
) -> List[Dict[str, Any]]:
    """List every child of ``folder_id``, draining all pages."""
    trashed_clause = "" if include_trashed else " and trashed=false"
    query = f"'{_escape_query_value(folder_id)}' in parents{trashed_clause}"
    return await paginate(
        lambda token: service.files().list(
            q=query,
            fields=f"nextPageToken, files({fields})",
            pageSize=1000,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ),
        items_key="files",
        label=f"files.list(children of {folder_id})",
    )


async def _inventory_tree(
    service,
    root_id: str,
    *,
    include_trashed: bool = False,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> Tuple[List[Dict[str, Any]], Dict[str, int], List[str], Dict[str, Any]]:
    """Breadth-first inventory of everything under ``root_id``.

    Returns ``(rows, counts, warnings, root_metadata)``. Rows carry a ``path``
    relative to the root so two trees can be compared positionally. Shortcut
    targets are recorded but never followed — a shortcut is an item in its own
    right and following it would double-count the target.
    """
    root = await execute_with_backoff(
        lambda: service.files().get(
            fileId=root_id,
            fields="id, name, mimeType, driveId",
            supportsAllDrives=True,
        ),
        label="files.get(root)",
    )
    if root.get("mimeType") != FOLDER_MIME_TYPE:
        raise UserInputError(
            f"'{root.get('name', root_id)}' is not a folder or shared drive "
            f"(mimeType={root.get('mimeType')}); nothing to walk."
        )

    rows: List[Dict[str, Any]] = []
    warnings: List[str] = []
    seen_ids = {root_id}
    folder_count = 0
    truncated = False

    # deque, not list: ``pop(0)`` on a list shifts every remaining entry, which
    # makes the traversal quadratic in the folder count. At the 50k-item scale
    # this tool advertises, a wide drive would burn real CPU shuffling
    # references between API calls.
    queue: Deque[Tuple[str, str]] = deque([(root_id, "")])
    while queue:
        folder_id, folder_path = queue.popleft()
        folder_count += 1
        children = await _list_children(
            service, folder_id, include_trashed=include_trashed
        )

        for child in children:
            child_id = child.get("id", "")
            child_path = f"{folder_path}/{child.get('name', '')}".lstrip("/")
            if child_id in seen_ids:
                # A file can legitimately have several parents in My Drive.
                # Record the extra placement as a warning rather than emitting
                # the item twice and inflating the count.
                warnings.append(
                    f"multi-parent or cyclic item skipped on second visit: "
                    f"{child.get('name')} ({child_id}) at {child_path}"
                )
                continue
            seen_ids.add(child_id)

            rows.append(_inventory_row(child, child_path, "walk"))
            if len(rows) >= max_items:
                truncated = True
                queue.clear()
                break

            if child.get("mimeType") == FOLDER_MIME_TYPE:
                queue.append((child_id, child_path))

        if truncated:
            warnings.append(
                f"walk stopped at max_items={max_items}; the inventory is a "
                "floor, not a complete count. Re-run with a higher max_items."
            )
            break

    counts = {
        "folders_traversed": folder_count,
        "items_found": len(rows),
        "truncated": int(truncated),
    }
    return rows, counts, warnings, root


def _inventory_row(
    item: Dict[str, Any], path: str, discovered_by: str
) -> Dict[str, Any]:
    shortcut = item.get("shortcutDetails") or {}
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "path": path,
        "mimeType": item.get("mimeType"),
        "size": item.get("size"),
        "md5Checksum": item.get("md5Checksum"),
        "sha1Checksum": item.get("sha1Checksum"),
        "sha256Checksum": item.get("sha256Checksum"),
        "owners": [o.get("emailAddress") for o in (item.get("owners") or [])],
        "modifiedTime": item.get("modifiedTime"),
        "createdTime": item.get("createdTime"),
        "parents": item.get("parents") or [],
        "driveId": item.get("driveId"),
        "trashed": item.get("trashed", False),
        "shortcutTargetId": shortcut.get("targetId"),
        "discovered_by": discovered_by,
    }


async def _drive_wide_sweep(
    service, drive_id: str, *, include_trashed: bool, max_items: int
) -> List[Dict[str, Any]]:
    """Independent second pass: every item in the shared drive, ignoring parents.

    This is what catches a folder the parent-walk never reached — the exact
    failure mode of the original BIR crawl.

    Honours the same ``max_items`` cap as the parent walk: an uncapped sweep of
    a very large drive would blow past the documented safety limit no matter
    what the walk did.
    """
    query = "trashed=true or trashed=false" if include_trashed else "trashed=false"
    return await paginate(
        lambda token: service.files().list(
            q=query,
            corpora="drive",
            driveId=drive_id,
            fields=f"nextPageToken, files({INVENTORY_FIELDS})",
            pageSize=1000,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ),
        items_key="files",
        max_items=max_items,
        label=f"files.list(sweep {drive_id})",
    )


# Bound on how far up a sweep-only item's ancestor chain we will walk while
# rebuilding its path. Far above any real folder depth; exists so a malformed
# or cyclic parent graph cannot spin.
_MAX_ANCESTOR_DEPTH = 64


def _resolve_sweep_path(
    item: Dict[str, Any],
    known_paths: Dict[str, str],
    sweep_by_id: Dict[str, Dict[str, Any]],
    root_id: str,
) -> str:
    """Rebuild the path of an item the parent walk never reached.

    Walks up ``parents`` until it hits a folder the walk *did* reach (whose
    path is therefore known), or the root. Ancestors that were themselves only
    found by the sweep contribute their own name, so a whole missed subtree
    keeps its shape instead of collapsing every member to ``<unreached>/name``
    — which would create false path collisions and make the manifest useless
    for path-keyed reconciliation.
    """
    segments = [item.get("name", "")]
    seen = {item.get("id")}
    current = (item.get("parents") or [None])[0]

    for _ in range(_MAX_ANCESTOR_DEPTH):
        if not current or current == root_id:
            return "/".join(reversed(segments))
        if current in known_paths:
            prefix = known_paths[current]
            ordered = list(reversed(segments))
            return "/".join([prefix, *ordered]) if prefix else "/".join(ordered)
        if current in seen:
            break  # cycle in the parent graph
        seen.add(current)
        ancestor = sweep_by_id.get(current)
        if ancestor is None:
            break  # parent is outside the swept corpus
        segments.append(ancestor.get("name", ""))
        current = (ancestor.get("parents") or [None])[0]

    return "/".join(["<unreached>", *reversed(segments)])


async def _full_inventory(
    service,
    root_id: str,
    *,
    include_trashed: bool = False,
    max_items: int = DEFAULT_MAX_ITEMS,
    self_check: bool = True,
) -> Tuple[
    List[Dict[str, Any]],
    Dict[str, int],
    List[str],
    Dict[str, Any],
    List[Dict[str, Any]],
]:
    """Two-pass inventory: parent walk, then the independent drive-wide sweep.

    Returns ``(rows, counts, warnings, root_metadata, sweep_only_rows)`` with
    ``rows`` already including the sweep-only items.

    Shared by ``walk_drive`` and ``reconcile_folders`` so the pilot go/no-go
    gate sees exactly the same set of items the inventory does. Reconciling
    with the parent walk alone would let an item that is unreachable on the
    source *and* absent from the destination disappear from both sides of the
    comparison and yield a clean verdict over lost content.
    """
    rows, counts, warnings, root_meta = await _inventory_tree(
        service, root_id, include_trashed=include_trashed, max_items=max_items
    )

    sweep_only: List[Dict[str, Any]] = []
    drive_id = root_meta.get("driveId")
    is_drive_root = bool(drive_id) and drive_id == root_meta.get("id")

    if self_check and is_drive_root:
        # Fetch one past the cap as a sentinel. Capping the sweep at exactly
        # max_items would be indistinguishable from "the drive holds exactly
        # that many", and items dropped by the cap would then disappear with no
        # truncation warning at all — the silent undercount this tool exists to
        # eliminate.
        sweep = await _drive_wide_sweep(
            service,
            drive_id,
            include_trashed=include_trashed,
            max_items=max_items + 1,
        )
        sweep_truncated = len(sweep) > max_items
        if sweep_truncated:
            sweep = sweep[:max_items]
            warnings.append(
                f"drive-wide sweep hit max_items={max_items}; some items were "
                "not examined at all. Re-run with a higher max_items."
            )
            counts["truncated"] = 1

        known_paths = {row["id"]: row["path"] for row in rows}
        sweep_by_id = {item.get("id"): item for item in sweep}
        for item in sweep:
            item_id = item.get("id")
            if item_id in known_paths or item_id == root_id:
                continue
            path = _resolve_sweep_path(item, known_paths, sweep_by_id, root_id)
            sweep_only.append(_inventory_row(item, path, "sweep"))

        # Sort before applying the cap so truncation is deterministic rather
        # than dependent on the order Drive happened to return pages in.
        sweep_only.sort(
            key=lambda r: (r.get("path") or "", r.get("name") or "", r.get("id") or "")
        )
        room = max(0, max_items - len(rows))
        if len(sweep_only) > room:
            warnings.append(
                f"sweep found {len(sweep_only)} additional item(s) but only "
                f"{room} fit under max_items={max_items}; the inventory is a "
                "floor. Re-run with a higher max_items."
            )
            sweep_only = sweep_only[:room]
            counts["truncated"] = 1

        rows.extend(sweep_only)
        counts["sweep_total"] = len(sweep)
        counts["found_only_by_sweep"] = len(sweep_only)
    elif self_check:
        warnings.append(
            "self_check skipped the drive-wide sweep: root is a folder inside a "
            "drive, not a shared-drive root, so there is no driveId corpus to "
            "sweep. Counts are from the parent walk only."
        )

    return rows, counts, warnings, root_meta, sweep_only


# --- 6. walk_drive ----------------------------------------------------------


@server.tool()
@handle_http_errors("walk_drive", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def walk_drive(
    service,
    user_google_email: str,
    root_id: str,
    include_trashed: bool = False,
    self_check: bool = True,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> str:
    """
    Produces a complete recursive inventory of a shared drive or folder.

    Two independent passes, reconciled against each other:
      1. a breadth-first parent walk with exhaustive pagination;
      2. (shared drives, ``self_check=True``) a drive-wide sweep by driveId
         that ignores the parent graph entirely.

    Anything the sweep finds that the walk missed is added to the manifest,
    tagged ``discovered_by: "sweep"``, and reported as a discrepancy — this is
    what catches folders a parent-walk cannot reach.

    Output is a JSONL manifest (one object per item: id, name, path, mimeType,
    size, checksums, owners, modifiedTime, parents) written to the attachment
    store. Rows are sorted by path so two walks of a static drive produce
    identical output.

    Args:
        user_google_email (str): The user's Google email address. Required.
        root_id (str): Shared drive ID or folder ID to walk. Required.
        include_trashed (bool): Include trashed items. Defaults to False.
        self_check (bool): Run the reconciliation sweep. Defaults to True.
            Turning it off roughly halves the API calls and forfeits the
            completeness guarantee.
        max_items (int): Safety cap on inventory size. Defaults to 50000.
            A truncated walk says so explicitly.

    Returns:
        str: Summary counts, discrepancies, and the manifest location.
    """
    if not root_id or not root_id.strip():
        raise UserInputError("root_id is required.")
    if max_items < 1:
        raise UserInputError("max_items must be at least 1.")

    logger.info("[walk_drive] root=%s self_check=%s", root_id, self_check)
    rows, counts, warnings, root_meta, sweep_only = await _full_inventory(
        service,
        root_id,
        include_trashed=include_trashed,
        max_items=max_items,
        self_check=self_check,
    )
    drive_id = root_meta.get("driveId")
    is_drive_root = bool(drive_id) and drive_id == root_meta.get("id")

    # Deterministic ordering: the acceptance criterion is that two walks of a
    # static drive return identical rows, not merely identical counts.
    rows.sort(
        key=lambda r: (r.get("path") or "", r.get("name") or "", r.get("id") or "")
    )

    folders = sum(1 for r in rows if r.get("mimeType") == FOLDER_MIME_TYPE)
    counts.update(
        {
            "items_total": len(rows),
            "folders": folders,
            "files": len(rows) - folders,
        }
    )

    safe_name = (root_meta.get("name") or root_id).replace("/", "_")[:60]
    _, path, access_line = write_jsonl_report(rows, filename=f"walk_{safe_name}.jsonl")

    lines = [
        f"Walk complete: '{root_meta.get('name', root_id)}' ({root_id})",
        summarise_counts(counts),
    ]
    if sweep_only:
        lines.append(
            f"⚠️ {len(sweep_only)} item(s) were reachable only via the drive-wide "
            "sweep — the parent walk could not reach them:"
        )
        for row in sweep_only[:20]:
            lines.append(f"   • {row['name']} ({row['id']}) at {row['path']}")
        if len(sweep_only) > 20:
            lines.append(f"   … and {len(sweep_only) - 20} more (see manifest)")
    elif self_check and is_drive_root:
        lines.append("✅ Self-check passed: the sweep found nothing the walk missed.")
    for warning in warnings[:20]:
        lines.append(f"⚠️ {warning}")
    if len(warnings) > 20:
        lines.append(f"⚠️ … and {len(warnings) - 20} more warnings")
    lines.append(f"Manifest (JSONL): {access_line}")
    logger.info("[walk_drive] wrote manifest to %s", path)
    return "\n".join(lines)


# --- 8. get_drive_file_metadata ---------------------------------------------


@server.tool()
@handle_http_errors("get_drive_file_metadata", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def get_drive_file_metadata(
    service,
    user_google_email: str,
    file_id: str,
    fields: Optional[str] = None,
) -> str:
    """
    Fetches Drive file metadata, including checksums, for copy verification.

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_id (str): Drive file or folder ID. Required.
        fields (Optional[str]): Comma-separated Drive ``files.get`` field list.
            Defaults to a set covering id/name/mimeType/size/md5Checksum/
            sha1Checksum/sha256Checksum/properties/appProperties/parents/driveId.

    Returns:
        str: The metadata as formatted JSON.

    Note:
        Native Google files (Docs, Sheets, Slides) have **no checksum and no
        size** — Drive does not expose one because the canonical form is not a
        byte stream. Verify a copied native file by exported byte-size or by
        revision presence instead; ``reconcile_folders`` reports them as
        ``unverifiable_native`` rather than pretending they matched.
    """
    if not file_id or not file_id.strip():
        raise UserInputError("file_id is required.")

    requested_fields = fields or METADATA_FIELDS
    metadata = await execute_with_backoff(
        lambda: service.files().get(
            fileId=file_id, fields=requested_fields, supportsAllDrives=True
        ),
        label="files.get(metadata)",
    )

    body = json.dumps(metadata, indent=2, ensure_ascii=False, default=str)
    note = ""
    if is_native_google_file(metadata.get("mimeType")):
        note = (
            "\n\nNote: this is a native Google file — Drive exposes no "
            "md5/sha checksum and no size for it. Verify copies by exported "
            "byte-size or revision presence."
        )
    return f"Metadata for {file_id}:\n{body}{note}"


# --- 10. create_folder_tree -------------------------------------------------


async def _find_child_folder(
    service, parent_id: str, name: str
) -> Optional[Dict[str, Any]]:
    query = (
        f"'{_escape_query_value(parent_id)}' in parents and "
        f"name='{_escape_query_value(name)}' and "
        f"mimeType='{FOLDER_MIME_TYPE}' and trashed=false"
    )
    matches = await paginate(
        lambda token: service.files().list(
            q=query,
            fields="nextPageToken, files(id, name)",
            pageSize=10,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ),
        items_key="files",
        label="files.list(child folder)",
    )
    return matches[0] if matches else None


def _manifest_paths(
    paths: Optional[Sequence[str]],
    manifest_json: Optional[str],
    manifest_path: Optional[str],
    drive: Optional[str] = None,
) -> List[str]:
    """Normalise the three accepted input shapes into a list of folder paths.

    The tab-02 manifest carries a ``drive`` column because one sheet describes
    every destination drive in the architecture. ``root_id`` names exactly one
    of them, so a mixed-drive manifest must be filtered by ``drive`` — building
    the whole architecture under a single root would be silent, and expensive
    to unpick.
    """
    if paths:
        return [p for p in (s.strip().strip("/") for s in paths) if p]

    rows = parse_manifest(manifest_json, manifest_path, required_keys=("folder_path",))
    active = [
        row
        for row in rows
        if str(row.get("action") or "create").strip().lower()
        not in {"skip", "none", "ignore"}
    ]

    drives_present = {
        str(row.get("drive")).strip()
        for row in active
        if str(row.get("drive") or "").strip()
    }
    wanted_drive = (drive or "").strip()

    if wanted_drive:
        if drives_present and wanted_drive not in drives_present:
            raise UserInputError(
                f"No manifest rows for drive {wanted_drive!r}. Drives present: "
                f"{', '.join(sorted(drives_present))}."
            )
        active = [
            row for row in active if str(row.get("drive") or "").strip() == wanted_drive
        ]
    elif len(drives_present) > 1:
        raise UserInputError(
            "Manifest spans multiple drives "
            f"({', '.join(sorted(drives_present))}) but no drive was selected. "
            "Pass drive='<name>' so only that drive's folders are created "
            "under root_id."
        )

    wanted: List[str] = []
    for row in active:
        folder_path = str(row.get("folder_path") or "").strip().strip("/")
        if folder_path:
            wanted.append(folder_path)
    return wanted


@server.tool()
@handle_http_errors("create_folder_tree", service_type="drive")
@require_google_service("drive", "drive_full")
async def create_folder_tree(
    service,
    user_google_email: str,
    root_id: str,
    paths: Optional[List[str]] = None,
    manifest_json: Optional[str] = None,
    manifest_path: Optional[str] = None,
    drive: Optional[str] = None,
    dry_run: bool = False,
) -> str:
    """
    Creates a folder tree from a path manifest, idempotently.

    Replaces the ~60 sequential ``create_drive_folder`` calls a template
    instantiation used to need. An existing path is reused, never duplicated,
    so re-running the same manifest after a partial failure is safe.

    A path that fails (for example, a location the caller cannot write to) is
    recorded and the run continues with the paths that do not depend on it, so
    one bad row cannot block the rest of the batch.

    Args:
        user_google_email (str): The user's Google email address. Required.
        root_id (str): Shared drive ID or folder ID the tree is built under.
        paths (Optional[List[str]]): Folder paths relative to root, e.g.
            ``["01_Governance", "01_Governance/01_Policies"]``. Intermediate
            segments are created automatically.
        manifest_json (Optional[str]): JSON array / JSONL of rows shaped like
            the architecture xlsx tab 02: ``{"drive":…, "folder_path":…,
            "action":…}``. Rows with ``action`` of skip/none/ignore are passed
            over.
        manifest_path (Optional[str]): Path to a JSON/JSONL manifest file.
        drive (Optional[str]): Which manifest ``drive`` value to build. Required
            when the manifest spans more than one drive — ``root_id`` names one
            drive, so an unfiltered multi-drive manifest is refused rather than
            creating every drive's folders under this root.
        dry_run (bool): Report what would be created without creating it.

    Returns:
        str: Per-path created/existing status plus the folder IDs, ready for
            registry write-back.
    """
    if not root_id or not root_id.strip():
        raise UserInputError("root_id is required.")
    if paths and (manifest_json or manifest_path):
        raise UserInputError("Pass paths or a manifest, not both.")

    wanted = _manifest_paths(paths, manifest_json, manifest_path, drive)
    if not wanted:
        raise UserInputError("No folder paths to create.")

    resolved_root = await resolve_folder_id(service, root_id)
    # path -> folder id, seeded with the root so single-segment paths resolve.
    known: Dict[str, str] = {"": resolved_root}
    created_paths: List[str] = []
    existing_paths: List[str] = []
    planned: List[str] = []
    failures: List[Tuple[str, str]] = []

    for folder_path in sorted(set(wanted)):
        segments = [s for s in folder_path.split("/") if s.strip()]
        current_path = ""
        try:
            for segment in segments:
                parent_id = known[current_path]
                current_path = f"{current_path}/{segment}".lstrip("/")
                if current_path in known:
                    continue

                match = await _find_child_folder(service, parent_id, segment)
                if match:
                    known[current_path] = match["id"]
                    existing_paths.append(current_path)
                    continue

                if dry_run:
                    # No ID exists yet; use a placeholder so deeper segments of
                    # the same path can still be planned.
                    known[current_path] = f"<would-create:{current_path}>"
                    planned.append(current_path)
                    continue

                created = await execute_with_backoff(
                    lambda p=parent_id, s=segment: service.files().create(
                        body={
                            "name": s,
                            "parents": [p],
                            "mimeType": FOLDER_MIME_TYPE,
                        },
                        fields="id, name",
                        supportsAllDrives=True,
                    ),
                    label="files.create(folder)",
                )
                known[current_path] = created["id"]
                created_paths.append(current_path)
        except Exception as exc:  # noqa: BLE001 - partial-failure continuation
            # Independent paths must still be attempted; a single unwritable
            # location should not abort the batch and force a manual manifest
            # edit before a re-run can make progress.
            logger.warning(
                "[create_folder_tree] path %r failed at %r: %s",
                folder_path,
                current_path,
                exc,
            )
            failures.append((folder_path, f"{type(exc).__name__}: {exc}"))

    header = "DRY RUN — no folders were created." if dry_run else "Folder tree ready."
    counts = {
        "paths_requested": len(set(wanted)),
        "folders_created" if not dry_run else "folders_to_create": (
            len(created_paths) if not dry_run else len(planned)
        ),
        "folders_already_present": len(existing_paths),
        "paths_failed": len(failures),
    }
    lines = [header, summarise_counts(counts)]
    if failures:
        lines.append("")
        lines.append(f"❌ {len(failures)} path(s) failed (the rest were processed):")
        for folder_path, error in failures[:10]:
            lines.append(f"   • {folder_path}: {error}")
        if len(failures) > 10:
            lines.append(f"   … and {len(failures) - 10} more")
    lines.extend(["", "Resolved folder IDs:"])
    for folder_path in sorted(set(wanted)):
        lines.append(f"   {folder_path} → {known.get(folder_path, '(not resolved)')}")
    return "\n".join(lines)


# --- 7. batch_copy_from_manifest --------------------------------------------


async def _already_copied(
    service, dest_folder_id: str, source_id: str
) -> Optional[Dict[str, Any]]:
    """Find a prior copy of ``source_id`` in ``dest_folder_id`` by provenance key."""
    query = (
        f"'{_escape_query_value(dest_folder_id)}' in parents and trashed=false and "
        f"appProperties has {{ key='{PROV_SOURCE_ID}' and "
        f"value='{_escape_query_value(source_id)}' }}"
    )
    matches = await paginate(
        lambda token: service.files().list(
            q=query,
            fields="nextPageToken, files(id, name)",
            pageSize=10,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ),
        items_key="files",
        label="files.list(provenance)",
    )
    return matches[0] if matches else None


async def _copy_one_row(
    service, row: Dict[str, Any], migration_batch: str, dry_run: bool
) -> Dict[str, Any]:
    """Copy a single manifest row. Never raises — failures become result rows."""
    source_id = str(row.get("source_id") or "").strip()
    dest_folder_id = str(row.get("dest_folder_id") or "").strip()
    result: Dict[str, Any] = {
        "source_id": source_id,
        "dest_folder_id": dest_folder_id,
        "status": "pending",
    }
    try:
        existing = await _already_copied(service, dest_folder_id, source_id)
        if existing:
            result.update(
                status="skipped",
                reason="already copied (provenance key present)",
                copy_id=existing.get("id"),
                copy_name=existing.get("name"),
            )
            return result

        source = await execute_with_backoff(
            lambda: service.files().get(
                fileId=source_id,
                fields="id, name, mimeType, driveId, size, md5Checksum",
                supportsAllDrives=True,
            ),
            label="files.get(copy source)",
        )
        new_name = str(row.get("new_name") or source.get("name") or "Untitled")
        result["source_name"] = source.get("name")
        result["new_name"] = new_name

        if dry_run:
            result.update(status="would_copy")
            return result

        copied_at = datetime.now(timezone.utc).isoformat()
        body = {
            "name": new_name,
            "parents": [dest_folder_id],
            # User-visible provenance, per the migration decision record.
            "properties": {
                "sourceFileId": source_id,
                "sourceDrive": str(source.get("driveId") or ""),
                "migrationBatch": migration_batch,
            },
            # Private provenance: this is the idempotency key.
            "appProperties": {
                PROV_SOURCE_ID: source_id,
                PROV_BATCH: migration_batch,
                PROV_COPIED_AT: copied_at,
            },
        }
        copied = await execute_with_backoff(
            lambda: service.files().copy(
                fileId=source_id,
                body=body,
                supportsAllDrives=True,
                fields="id, name, mimeType, size, md5Checksum, appProperties",
            ),
            label="files.copy",
        )
        stamped = (copied.get("appProperties") or {}).get(PROV_SOURCE_ID)
        result.update(
            status="copied",
            copy_id=copied.get("id"),
            copy_name=copied.get("name"),
            copied_at=copied_at,
            source_md5=source.get("md5Checksum"),
            copy_md5=copied.get("md5Checksum"),
            provenance_stamped=stamped == source_id,
        )
        if stamped != source_id:
            result["reason"] = "provenance property missing on the copy"
    except Exception as exc:  # noqa: BLE001 - partial-failure continuation
        logger.warning(
            "[batch_copy_from_manifest] row failed source=%s dest=%s: %s",
            source_id,
            dest_folder_id,
            exc,
        )
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    return result


@server.tool()
@handle_http_errors("batch_copy_from_manifest", service_type="drive")
@require_google_service("drive", "drive_full")
async def batch_copy_from_manifest(
    service,
    user_google_email: str,
    manifest_json: Optional[str] = None,
    manifest_path: Optional[str] = None,
    migration_batch: str = "",
    batch_size: int = 10,
    dry_run: bool = False,
    max_rows: Optional[int] = None,
) -> str:
    """
    Copies files from a manifest into destination folders, idempotently.

    Each row is ``{"source_id": …, "dest_folder_id": …, "new_name": …}``
    (``new_name`` optional). Behaviour:

      * **Idempotent** — a row whose source has already been copied into the
        destination (detected via the ``mcp_source_file_id`` appProperty) is
        skipped, so re-running a manifest copies nothing. Repeated
        ``(source_id, dest_folder_id)`` pairs within one manifest are collapsed
        before any work starts.
      * **Provenance-stamped** — every copy gets user-visible ``sourceFileId`` /
        ``sourceDrive`` / ``migrationBatch`` properties plus the private
        idempotency key.
      * **Partial-failure tolerant** — a failing row is logged and the run
        continues; the per-row result log records why.
      * **Rate-limit aware** — every call retries with exponential backoff and
        jitter.

    Cross-tenant note: content cannot be *moved* between organisations, only
    copied. Copies get new IDs and no version history — that is expected, not a
    defect.

    Args:
        user_google_email (str): The user's Google email address. Required.
        manifest_json (Optional[str]): JSON array or JSONL of rows.
        manifest_path (Optional[str]): Path to a JSON/JSONL manifest file.
        migration_batch (str): Label stamped on every copy for later filtering.
        batch_size (int): Progress-logging interval, in rows. Defaults to 10.
            Rows are issued sequentially — the injected Google client's
            transport is not thread-safe, so concurrent rows would share one
            connection across worker threads.
        dry_run (bool): Resolve and validate every row, copy nothing.
        max_rows (Optional[int]): Process at most this many rows.

    Returns:
        str: Summary counts, the first failures inline, and the JSONL result log.
    """
    rows = parse_manifest(
        manifest_json,
        manifest_path,
        required_keys=("source_id", "dest_folder_id"),
    )
    if max_rows is not None:
        if max_rows < 1:
            raise UserInputError("max_rows must be at least 1.")
        if len(rows) > max_rows:
            logger.info(
                "[batch_copy_from_manifest] capping %d rows at max_rows=%d",
                len(rows),
                max_rows,
            )
        rows = rows[:max_rows]
    if batch_size < 1:
        raise UserInputError("batch_size must be at least 1.")

    # Collapse repeated (source, destination) pairs before doing any work. The
    # provenance pre-check cannot see a copy that does not exist yet, so a
    # manifest that names the same pair twice — easy to produce by
    # concatenating two manifests — would otherwise pass the check twice and
    # create two provenance-stamped copies, breaking the idempotency guarantee
    # that is the whole point of the tool.
    deduped: List[Dict[str, Any]] = []
    seen_keys = set()
    duplicate_rows = 0
    for row in rows:
        key = (
            str(row.get("source_id") or "").strip(),
            str(row.get("dest_folder_id") or "").strip(),
        )
        if key in seen_keys:
            duplicate_rows += 1
            continue
        seen_keys.add(key)
        deduped.append(row)
    rows = deduped

    # Rows run sequentially. Every request here is built from the one injected
    # ``service``, whose underlying googleapiclient/httplib2 transport is not
    # thread-safe, and execute_with_backoff hands each request to a worker
    # thread — so issuing rows concurrently would share one connection across
    # threads and can interleave or corrupt HTTP activity on a real run. Drive's
    # per-user write quota caps throughput well before this server does, so the
    # cost is small; see FOLLOWUPS.md for the per-worker-transport option that
    # would restore parallelism safely.
    results: List[Dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        results.append(await _copy_one_row(service, row, migration_batch, dry_run))
        if index % batch_size == 0:
            logger.info(
                "[batch_copy_from_manifest] processed %d/%d rows", index, len(rows)
            )

    counts = {
        "rows": len(rows),
        "duplicate_rows_collapsed": duplicate_rows,
        "copied": sum(1 for r in results if r["status"] == "copied"),
        "would_copy": sum(1 for r in results if r["status"] == "would_copy"),
        "skipped_already_copied": sum(1 for r in results if r["status"] == "skipped"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "unstamped_copies": sum(
            1
            for r in results
            if r["status"] == "copied" and not r.get("provenance_stamped")
        ),
    }

    _, _, access_line = write_jsonl_report(
        results, filename=f"copy_results_{migration_batch or 'batch'}.jsonl"
    )

    header = (
        "DRY RUN — nothing was copied."
        if dry_run
        else f"Batch copy complete (batch label: '{migration_batch or 'unset'}')."
    )
    lines = [header, summarise_counts(counts)]
    failures = [r for r in results if r["status"] == "failed"]
    if failures:
        lines.append(f"❌ {len(failures)} row(s) failed:")
        for failure in failures[:10]:
            lines.append(
                f"   • {failure['source_id']} → {failure['dest_folder_id']}: "
                f"{failure.get('error')}"
            )
        if len(failures) > 10:
            lines.append(f"   … and {len(failures) - 10} more (see result log)")
    if counts["unstamped_copies"]:
        lines.append(
            f"⚠️ {counts['unstamped_copies']} copy/copies came back without the "
            "provenance property — re-check before reconciling."
        )
    if max_rows is not None and len(rows) == max_rows:
        lines.append(f"ℹ️ Capped at max_rows={max_rows}; more rows may remain.")
    lines.append(f"Per-row result log (JSONL): {access_line}")
    return "\n".join(lines)


# --- 9. reconcile_folders ---------------------------------------------------


def _group_by_path(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Bucket inventory rows by path.

    A list per path, not a single row: Drive permits several items with the
    same name in one folder, and collapsing them would let "two source
    ``foo.pdf`` versus one destination ``foo.pdf``" report a clean migration
    while a file was silently never copied.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["path"], []).append(row)
    return grouped


def _pairing_key(row: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """Stable ordering for same-path siblings so the two sides pair up.

    Checksum first, then size: identical content lines up across the two
    inventories even when Drive returns the siblings in a different order.
    """
    return (
        str(row.get("md5Checksum") or ""),
        str(row.get("size") or ""),
        str(row.get("mimeType") or ""),
        str(row.get("id") or ""),
    )


def _compare_one(
    path: str,
    source: Dict[str, Any],
    dest: Dict[str, Any],
    *,
    compare_checksums: bool,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Compare a single matched source/destination pair.

    Returns ``(discrepancy_or_None, is_unverifiable)``.
    """
    if source.get("mimeType") != dest.get("mimeType"):
        return {
            "kind": "mime_mismatch",
            "path": path,
            "source_id": source["id"],
            "dest_id": dest["id"],
            "source_mimeType": source.get("mimeType"),
            "dest_mimeType": dest.get("mimeType"),
        }, False

    if source.get("mimeType") == FOLDER_MIME_TYPE:
        return None, False

    if is_native_google_file(source.get("mimeType")):
        # No checksum exists for native Google files. Say so rather than
        # counting it as a verified match.
        return {
            "kind": "unverifiable_native",
            "path": path,
            "source_id": source["id"],
            "dest_id": dest["id"],
            "note": (
                "native Google file: no checksum or size available; "
                "verify by exported byte-size or revision presence"
            ),
        }, True

    if source.get("size") != dest.get("size"):
        return {
            "kind": "size_mismatch",
            "path": path,
            "source_id": source["id"],
            "dest_id": dest["id"],
            "source_size": source.get("size"),
            "dest_size": dest.get("size"),
        }, False

    if compare_checksums:
        source_md5 = source.get("md5Checksum")
        dest_md5 = dest.get("md5Checksum")
        if source_md5 and dest_md5 and source_md5 != dest_md5:
            return {
                "kind": "checksum_mismatch",
                "path": path,
                "source_id": source["id"],
                "dest_id": dest["id"],
                "source_md5": source_md5,
                "dest_md5": dest_md5,
            }, False
        if not source_md5 or not dest_md5:
            return {
                "kind": "checksum_unavailable",
                "path": path,
                "source_id": source["id"],
                "dest_id": dest["id"],
                "note": "Drive returned no md5 for one or both sides",
            }, True

    return None, False


def _compare_inventories(
    source_rows: List[Dict[str, Any]],
    dest_rows: List[Dict[str, Any]],
    *,
    compare_checksums: bool,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Diff two path-keyed inventories, returning discrepancies and counts.

    Every row is compared, including same-name siblings: the two sides are
    paired by content key within each path, and any surplus on either side is
    reported as missing/extra rather than dropped.
    """
    source_by_path = _group_by_path(source_rows)
    dest_by_path = _group_by_path(dest_rows)

    discrepancies: List[Dict[str, Any]] = []
    matched = 0
    unverifiable = 0

    for path in sorted(source_by_path):
        sources = sorted(source_by_path[path], key=_pairing_key)
        dests = sorted(dest_by_path.get(path, []), key=_pairing_key)

        for source, dest in zip(sources, dests):
            discrepancy, is_unverifiable = _compare_one(
                path, source, dest, compare_checksums=compare_checksums
            )
            if is_unverifiable:
                unverifiable += 1
            if discrepancy is not None:
                discrepancies.append(discrepancy)
            else:
                matched += 1

        # Surplus on either side of a shared path is a real discrepancy.
        for source in sources[len(dests) :]:
            discrepancies.append(
                {
                    "kind": "missing_in_dest",
                    "path": path,
                    "source_id": source["id"],
                    "mimeType": source.get("mimeType"),
                }
            )
        for dest in dests[len(sources) :]:
            discrepancies.append(
                {
                    "kind": "extra_in_dest",
                    "path": path,
                    "dest_id": dest["id"],
                    "mimeType": dest.get("mimeType"),
                }
            )

    for path in sorted(dest_by_path):
        if path in source_by_path:
            continue
        for dest in dest_by_path[path]:
            discrepancies.append(
                {
                    "kind": "extra_in_dest",
                    "path": path,
                    "dest_id": dest["id"],
                    "mimeType": dest.get("mimeType"),
                }
            )

    counts = {
        "source_items": len(source_rows),
        "dest_items": len(dest_rows),
        "matched": matched,
        "unverifiable": unverifiable,
        "discrepancies": len(discrepancies),
    }
    return discrepancies, counts


@server.tool()
@handle_http_errors("reconcile_folders", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def reconcile_folders(
    service,
    user_google_email: str,
    source_folder_id: str,
    dest_folder_id: str,
    compare_checksums: bool = True,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> str:
    """
    Compares a source folder tree against its migrated destination.

    Inventories both sides with the same two-pass walk ``walk_drive`` uses —
    parent traversal plus, for shared-drive roots, the independent drive-wide
    sweep — then keys every item by its path relative to its root and reports
    each of: ``missing_in_dest``, ``extra_in_dest``, ``mime_mismatch``,
    ``size_mismatch``, ``checksum_mismatch``, ``checksum_unavailable``, and
    ``unverifiable_native``.

    Using the sweep matters here: an item unreachable from the source's parent
    graph *and* absent from the destination would otherwise drop out of both
    sides of the comparison and return a clean verdict over lost content.

    Same-name siblings are all compared. Drive allows several items with one
    name in a folder, so the two sides are paired by content within each path
    and any surplus is reported rather than dropped.

    A clean report (0 blocking discrepancies, native files acknowledged) is the
    gate the pilot has to pass before a full migration run is allowed.

    Args:
        user_google_email (str): The user's Google email address. Required.
        source_folder_id (str): Source folder or shared drive ID.
        dest_folder_id (str): Destination folder or shared drive ID.
        compare_checksums (bool): Compare md5 checksums where Drive provides
            them. Defaults to True.
        max_items (int): Safety cap per side. Defaults to 50000.

    Returns:
        str: Summary counts, the first discrepancies inline, and a JSONL report.

    Note:
        Native Google files carry no checksum, so they are reported as
        ``unverifiable_native`` rather than silently counted as matching.
    """
    if not source_folder_id or not dest_folder_id:
        raise UserInputError("source_folder_id and dest_folder_id are both required.")

    (
        source_rows,
        source_counts,
        source_warnings,
        _,
        source_sweep,
    ) = await _full_inventory(service, source_folder_id, max_items=max_items)
    dest_rows, dest_counts, dest_warnings, _, dest_sweep = await _full_inventory(
        service, dest_folder_id, max_items=max_items
    )

    discrepancies, counts = _compare_inventories(
        source_rows, dest_rows, compare_checksums=compare_checksums
    )

    _, _, access_line = write_jsonl_report(
        discrepancies or [{"kind": "clean", "note": "no discrepancies found"}],
        filename="reconcile_report.jsonl",
    )

    by_kind: Dict[str, int] = {}
    for item in discrepancies:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1

    blocking = sum(
        count
        for kind, count in by_kind.items()
        if kind not in {"unverifiable_native", "checksum_unavailable"}
    )
    verdict = (
        "✅ Reconciliation clean — no blocking discrepancies."
        if blocking == 0
        else f"❌ {blocking} blocking discrepancy/discrepancies — do not proceed."
    )

    lines = [
        f"Reconciliation: {source_folder_id} → {dest_folder_id}",
        verdict,
        summarise_counts(counts),
    ]
    if by_kind:
        lines.append("By kind:")
        lines.append(summarise_counts(by_kind))
        for item in discrepancies[:20]:
            lines.append(f"   • [{item['kind']}] {item.get('path')}")
        if len(discrepancies) > 20:
            lines.append(f"   … and {len(discrepancies) - 20} more (see report)")
    if source_sweep or dest_sweep:
        lines.append(
            f"ℹ️ Drive-wide sweep contributed {len(source_sweep)} source and "
            f"{len(dest_sweep)} destination item(s) the parent walk could not "
            "reach; they are included in this comparison."
        )
    for warning in (source_warnings + dest_warnings)[:10]:
        lines.append(f"⚠️ {warning}")
    if source_counts.get("truncated") or dest_counts.get("truncated"):
        lines.append("⚠️ At least one side hit max_items — this report is incomplete.")
    lines.append(f"Discrepancy report (JSONL): {access_line}")
    return "\n".join(lines)


# --- 11. rebuild_hub --------------------------------------------------------


@require_google_service("sheets", "sheets_read")
async def _hub_registry_service(service, user_google_email: str):
    """Lazily acquire a Sheets service for rebuild_hub.

    Kept separate so the rest of this module needs only Drive scope; mirrors
    the ``_create_doc_drive_service`` pattern in gdocs/docs_tools.py.
    """
    return service


async def _read_registry_rows(
    sheets_service, spreadsheet_id: str, sheet_range: str
) -> List[Dict[str, str]]:
    """Read the Folder Registry into dicts keyed by its header row."""
    response = await execute_with_backoff(
        lambda: (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=sheet_range)
        ),
        label="sheets.values.get",
    )
    values = response.get("values") or []
    if not values:
        raise UserInputError(
            f"Registry range '{sheet_range}' in {spreadsheet_id} is empty."
        )
    header = [str(h).strip().lower().replace(" ", "_") for h in values[0]]
    rows: List[Dict[str, str]] = []
    for raw in values[1:]:
        row = {
            header[i]: str(raw[i]).strip() for i in range(min(len(header), len(raw)))
        }
        rows.append(row)
    return rows


async def _soft_delete_shortcut(
    service,
    *,
    shortcut_id: str,
    parent_id: str,
    holding_folder_id: str,
    user_google_email: str,
) -> None:
    """Move an orphaned hub shortcut into the soft-delete holding folder.

    Writes the same ``appProperties`` markers as
    ``gdrive.drive_tools.soft_delete_drive_file`` so ``restore_drive_file``
    can reverse it.
    """
    await execute_with_backoff(
        lambda: service.files().update(
            fileId=shortcut_id,
            addParents=holding_folder_id,
            removeParents=parent_id,
            supportsAllDrives=True,
            fields="id, name, parents",
            body={
                "appProperties": {
                    "mcp_softdeleted": "true",
                    "mcp_orig_parents": parent_id,
                    "mcp_deleted_at": datetime.now(timezone.utc).isoformat(),
                    "mcp_deleted_by": user_google_email,
                    "mcp_reason": "rebuild_hub: shortcut not in Folder Registry",
                }
            },
        ),
        label="files.update(soft-delete orphan shortcut)",
    )


def _registry_column(
    rows: List[Dict[str, str]], candidates: Sequence[str]
) -> Optional[str]:
    for candidate in candidates:
        if any(candidate in row for row in rows):
            return candidate
    return None


async def _list_section_shortcuts(service, section_id: str) -> List[Dict[str, Any]]:
    """Every shortcut directly inside a hub section folder."""
    return await paginate(
        lambda token: service.files().list(
            q=(
                f"'{_escape_query_value(section_id)}' in parents and "
                f"mimeType='{SHORTCUT_MIME_TYPE}' and trashed=false"
            ),
            fields=(
                "nextPageToken, files(id, name, "
                "shortcutDetails(targetId, targetMimeType))"
            ),
            pageSize=1000,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ),
        items_key="files",
        label="files.list(hub shortcuts)",
    )


async def _list_child_folders(service, parent_id: str) -> List[Dict[str, Any]]:
    """Every folder directly inside ``parent_id``."""
    return await paginate(
        lambda token: service.files().list(
            q=(
                f"'{_escape_query_value(parent_id)}' in parents and "
                f"mimeType='{FOLDER_MIME_TYPE}' and trashed=false"
            ),
            fields="nextPageToken, files(id, name)",
            pageSize=1000,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ),
        items_key="files",
        label="files.list(hub sections)",
    )


def _group_shortcuts_by_target(
    shortcuts: List[Dict[str, Any]],
) -> Dict[Optional[str], List[Dict[str, Any]]]:
    """Bucket shortcuts by the target they point at.

    A list per target, not a single shortcut: a section can already hold
    several shortcuts to the same target, and keeping only one would hide the
    duplicates from the diff so a rebuild could never converge on the promised
    one-shortcut-per-target state.
    """
    grouped: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for shortcut in shortcuts:
        target_id = (shortcut.get("shortcutDetails") or {}).get("targetId")
        grouped.setdefault(target_id, []).append(shortcut)
    for bucket in grouped.values():
        # Stable order so "which one is kept" doesn't vary between runs.
        bucket.sort(key=lambda s: str(s.get("id") or ""))
    return grouped


async def _record_orphan(
    service,
    *,
    section: str,
    section_id: str,
    shortcut: Dict[str, Any],
    target_id: str,
    reason: str,
    orphans: List[Dict[str, str]],
    removed: List[str],
    remove_orphans: bool,
    dry_run: bool,
    holding_folder_id: str,
    user_google_email: str,
) -> None:
    """Record a hub shortcut that should not be there, removing it if asked."""
    orphans.append(
        {
            "section": section,
            "shortcut_id": shortcut.get("id", ""),
            "name": shortcut.get("name", ""),
            "target_id": target_id,
            "reason": reason,
        }
    )
    if remove_orphans and not dry_run:
        # This server never trashes or hard-deletes a Drive file, and a
        # shortcut is a Drive file. Orphans move to the holding folder with the
        # same markers soft_delete_drive_file writes, so restore_drive_file can
        # put them back. The target the shortcut pointed at is never touched.
        await _soft_delete_shortcut(
            service,
            shortcut_id=shortcut.get("id", ""),
            parent_id=section_id,
            holding_folder_id=holding_folder_id,
            user_google_email=user_google_email,
        )
        removed.append(f"{section}/{shortcut.get('name')}")


@server.tool()
@handle_http_errors("rebuild_hub", service_type="drive")
@require_google_service("drive", "drive_full")
async def rebuild_hub(
    service,
    user_google_email: str,
    registry_spreadsheet_id: str,
    hub_folder_id: str,
    registry_range: str = "Folder Registry",
    remove_orphans: bool = False,
    dry_run: bool = False,
) -> str:
    """
    Rebuilds the OTB-Hub shortcut layer from the Folder Registry.

    Reads the registry's ``hub_section`` column, ensures one sub-folder per
    distinct section inside the hub, then diffs the shortcuts that should exist
    against the ones that do — creating what is missing. This is the "never
    drifts" guarantee behind the navigation decision: the registry is the
    source of truth and the hub is derived from it.

    The diff covers the whole hub, not just the sections the registry still
    mentions. Three things count as orphans: a shortcut whose target left the
    registry, every shortcut past the first pointing at the same target in one
    section, and every shortcut in a section the registry no longer mentions at
    all (including when the registry has no hub rows left).

    Args:
        user_google_email (str): The user's Google email address. Required.
        registry_spreadsheet_id (str): Spreadsheet ID of the Folder Registry.
        hub_folder_id (str): OTB-Hub shared drive ID or folder ID.
        registry_range (str): A1 range or sheet name to read. Defaults to
            "Folder Registry". The first row must be the header; a
            ``folder_id`` and a ``hub_section`` column are required.
        remove_orphans (bool): Report-only by default. When True, shortcuts in
            the hub that the registry no longer calls for are soft-deleted —
            moved to DRIVE_HOLDING_FOLDER_ID with the standard markers, so
            ``restore_drive_file`` reverses it. Nothing is trashed or
            hard-deleted, and the shortcut's target is never touched. Fails
            closed if DRIVE_HOLDING_FOLDER_ID is unset.
        dry_run (bool): Report the full plan without touching the hub.

    Returns:
        str: Per-section create/keep/orphan counts and the resulting plan.
    """
    if not registry_spreadsheet_id or not hub_folder_id:
        raise UserInputError(
            "registry_spreadsheet_id and hub_folder_id are both required."
        )

    # rebuild_hub is the one Drive tool that also needs Sheets. If the server
    # was started with the drive service but not sheets, the consent flow never
    # requested a Sheets scope and this acquisition fails — with an error that
    # says nothing about why. Translate it into the actionable instruction.
    try:
        sheets_service = await _hub_registry_service(
            user_google_email=user_google_email
        )
    except Exception as exc:  # noqa: BLE001 - re-raised with actionable guidance
        raise UserInputError(
            "rebuild_hub needs read access to the Folder Registry spreadsheet, "
            "and the Sheets scope is not available on these credentials. Enable "
            "the 'sheets' service alongside 'drive' (TOOLS / --tools) and "
            "re-authenticate, then retry. Underlying error: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    rows = await _read_registry_rows(
        sheets_service, registry_spreadsheet_id, registry_range
    )

    id_column = _registry_column(rows, ("folder_id", "id", "drive_folder_id"))
    section_column = _registry_column(rows, ("hub_section",))
    name_column = _registry_column(
        rows, ("folder_name", "name", "folder_path", "title")
    )
    if not id_column or not section_column:
        raise UserInputError(
            "Registry must have a folder_id column and a hub_section column; "
            f"found columns: {sorted(set().union(*(r.keys() for r in rows))) if rows else []}"
        )

    # section -> list of (target_id, label)
    desired: Dict[str, List[Tuple[str, str]]] = {}
    for row in rows:
        section = (row.get(section_column) or "").strip()
        target_id = (row.get(id_column) or "").strip()
        if not section or not target_id:
            continue
        label = (row.get(name_column) or "").strip() if name_column else ""
        desired.setdefault(section, []).append((target_id, label))

    # An empty ``desired`` is not "nothing to do": every shortcut currently in
    # the hub is now an orphan, and returning early would leave exactly the
    # stale navigation this tool exists to prevent.
    resolved_hub = await resolve_folder_id(service, hub_folder_id)

    # Resolve the holding folder up-front so a removal run fails closed before
    # it has half-rebuilt the hub, rather than partway through.
    holding_folder_id = ""
    if remove_orphans and not dry_run:
        holding_folder_id = await resolve_folder_id(service, get_holding_folder_id())

    created: List[str] = []
    kept: List[str] = []
    orphans: List[Dict[str, str]] = []
    removed: List[str] = []
    sections_created: List[str] = []
    seen_section_ids: set[str] = set()

    for section in sorted(desired):
        section_folder = await _find_child_folder(service, resolved_hub, section)
        if section_folder is None:
            if dry_run:
                sections_created.append(section)
                # Cannot enumerate or create shortcuts under a folder that does
                # not exist yet; everything in this section is a create.
                for target_id, label in desired[section]:
                    created.append(f"{section}/{label or target_id}")
                continue
            section_folder = await execute_with_backoff(
                lambda s=section: service.files().create(
                    body={
                        "name": s,
                        "parents": [resolved_hub],
                        "mimeType": FOLDER_MIME_TYPE,
                    },
                    fields="id, name",
                    supportsAllDrives=True,
                ),
                label="files.create(hub section)",
            )
            sections_created.append(section)

        section_id = section_folder["id"]
        seen_section_ids.add(section_id)
        existing_by_target = _group_shortcuts_by_target(
            await _list_section_shortcuts(service, section_id)
        )
        wanted_targets = {target_id for target_id, _ in desired[section]}

        for target_id, label in desired[section]:
            if target_id in existing_by_target:
                kept.append(f"{section}/{existing_by_target[target_id][0].get('name')}")
                continue
            if dry_run:
                created.append(f"{section}/{label or target_id}")
                continue
            # find_existing_shortcut re-checks under concurrency; cheap enough
            # relative to a create and keeps this idempotent.
            if await find_existing_shortcut(service, section_id, target_id):
                kept.append(f"{section}/{label or target_id}")
                continue
            target_meta = await execute_with_backoff(
                lambda t=target_id: service.files().get(
                    fileId=t, fields="id, name", supportsAllDrives=True
                ),
                label="files.get(hub target)",
            )
            shortcut_name = label or target_meta.get("name") or target_id
            await execute_with_backoff(
                lambda n=shortcut_name, sid=section_id, t=target_id: (
                    service.files().create(
                        body={
                            "name": n,
                            "mimeType": SHORTCUT_MIME_TYPE,
                            "parents": [sid],
                            "shortcutDetails": {"targetId": t},
                        },
                        fields="id, name",
                        supportsAllDrives=True,
                    )
                ),
                label="files.create(hub shortcut)",
            )
            created.append(f"{section}/{shortcut_name}")

        for target_id, shortcuts in existing_by_target.items():
            # Every shortcut beyond the first for a target is a duplicate, even
            # when the target is wanted: the promised state is one shortcut per
            # target per section.
            surplus = shortcuts if target_id not in wanted_targets else shortcuts[1:]
            reason = (
                "not in registry"
                if target_id not in wanted_targets
                else "duplicate shortcut to the same target"
            )
            for shortcut in surplus:
                await _record_orphan(
                    service,
                    section=section,
                    section_id=section_id,
                    shortcut=shortcut,
                    target_id=target_id or "",
                    reason=reason,
                    orphans=orphans,
                    removed=removed,
                    remove_orphans=remove_orphans,
                    dry_run=dry_run,
                    holding_folder_id=holding_folder_id,
                    user_google_email=user_google_email,
                )

    # Sections the registry no longer mentions at all still hold shortcuts.
    # Without this pass, dropping the last registry row for a section would
    # strand its whole shortcut set: never reported, never removed.
    stale_sections = 0
    for section_folder in await _list_child_folders(service, resolved_hub):
        section_id = section_folder.get("id", "")
        if section_id in seen_section_ids:
            continue
        stale_sections += 1
        section_name = section_folder.get("name", section_id)
        for target_id, shortcuts in _group_shortcuts_by_target(
            await _list_section_shortcuts(service, section_id)
        ).items():
            for shortcut in shortcuts:
                await _record_orphan(
                    service,
                    section=section_name,
                    section_id=section_id,
                    shortcut=shortcut,
                    target_id=target_id or "",
                    reason="section no longer in registry",
                    orphans=orphans,
                    removed=removed,
                    remove_orphans=remove_orphans,
                    dry_run=dry_run,
                    holding_folder_id=holding_folder_id,
                    user_google_email=user_google_email,
                )

    counts = {
        "sections": len(desired),
        "sections_created": len(sections_created),
        "stale_sections_scanned": stale_sections,
        "shortcuts_to_create" if dry_run else "shortcuts_created": len(created),
        "shortcuts_already_correct": len(kept),
        "orphan_shortcuts": len(orphans),
        "orphans_removed": len(removed),
    }

    header = (
        "DRY RUN — the hub was not modified."
        if dry_run
        else f"Hub rebuilt from registry {registry_spreadsheet_id}."
    )
    lines = [header, summarise_counts(counts)]
    if created:
        lines.append("Shortcuts " + ("to create:" if dry_run else "created:"))
        lines.extend(f"   + {item}" for item in created[:30])
        if len(created) > 30:
            lines.append(f"   … and {len(created) - 30} more")
    if orphans and not remove_orphans:
        lines.append(
            f"ℹ️ {len(orphans)} shortcut(s) in the hub do not match the registry. "
            "Re-run with remove_orphans=True to soft-delete them (targets are "
            "never touched):"
        )
        lines.extend(
            f"   - {o['section']}/{o['name']} → {o['target_id']} ({o['reason']})"
            for o in orphans[:20]
        )
        if len(orphans) > 20:
            lines.append(f"   … and {len(orphans) - 20} more")
    if removed:
        lines.append(f"Removed {len(removed)} orphan shortcut(s).")
    return "\n".join(lines)


__all__ = [
    "walk_drive",
    "get_drive_file_metadata",
    "create_folder_tree",
    "batch_copy_from_manifest",
    "reconcile_folders",
    "rebuild_hub",
    "is_native_google_file",
]

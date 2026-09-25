"""
Shared drive theme (banner) tools.

Gives every OTB Group shared drive consistent entity branding: the banner
image and colour shown at the top of the drive in the Drive UI.

Drive API facts this module relies on (from the Drive v3 discovery document
bundled with googleapiclient, ``schemas.Drive``):

* ``themeId`` and ``backgroundImageFile`` are write-only and mutually
  exclusive on one ``drives.update`` request. A theme sets both image and
  colour; a custom image sets only the image.
* ``backgroundImageFile`` needs **all** of ``id``, ``xCoordinate``,
  ``yCoordinate`` and ``width``. Coordinates and width are fractions (0 to 1)
  of the source image. Crop height follows from a fixed 80:9 width:height
  ratio, and the cropped area must be at least 1280 x 144 pixels.
* ``backgroundImageLink`` is output-only and short-lived. It is reported for
  before/after comparison, never stored.
* ``capabilities.canChangeDriveBackground`` says whether the caller may change
  the banner. It is the permission check used here: only a drive Manager
  (``organizer``) holds it, unless the caller acts as a domain admin with
  ``useDomainAdminAccess``.

Nothing here deletes or shares anything. The only write is ``drives.update``
with a theme or image body, which is idempotent: replaying it sets the same
banner again.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from googleapiclient.errors import HttpError

from auth.service_decorator import require_google_service
from core.server import server
from core.utils import UserInputError, handle_http_errors
from gdrive.drive_batch import execute_with_backoff
from gdrive.drive_migration_tools import _hub_registry_service, _read_registry_rows
from gdrive.shared_drive_tools import _get_shared_drive

logger = logging.getLogger(__name__)

# Fields read before and after a theme change. ``capabilities`` drives the
# Manager check; the rest is the before/after report.
_THEME_FIELDS = (
    "id, name, themeId, colorRgb, backgroundImageLink, "
    "capabilities(canChangeDriveBackground)"
)

# Google accepts JPG and PNG banners.
ALLOWED_IMAGE_MIME_TYPES = ("image/jpeg", "image/png")

# Banner geometry from the Drive API: width:height = 80:9, and the cropped
# area must be at least 1280 x 144 pixels.
BANNER_ASPECT_W = 80
BANNER_ASPECT_H = 9
BANNER_MIN_WIDTH_PX = 1280
BANNER_MIN_HEIGHT_PX = 144

# Float tolerance for "crop fits inside the image" checks.
_EPS = 1e-6

# Registry categories the bulk tool maps to an image. Entity categories come
# from the registry's ``entity`` column; the other three from its
# ``restricted`` / ``external`` / ``hub`` flags.
THEME_CATEGORIES = ("OTB", "JIT", "VALE", "BIR", "Restricted", "Hub", "ExternalShare")
_CATEGORY_LOOKUP = {c.lower(): c for c in THEME_CATEGORIES}

_TRUTHY = {"true", "yes", "y", "1"}


# ---------------------------------------------------------------------------
# Pure helpers (no API calls)
# ---------------------------------------------------------------------------


def compute_banner_crop(
    image_width: Optional[int],
    image_height: Optional[int],
    x_coordinate: Optional[float] = None,
    y_coordinate: Optional[float] = None,
    width: Optional[float] = None,
) -> Tuple[Dict[str, float], List[str]]:
    """Resolve the crop for a banner image, filling gaps with sensible defaults.

    The default is the largest centred 80:9 area of the image: full width
    when the image is taller than a banner, full height when it is wider.
    Any value the caller passes is kept; only the missing ones are derived.

    Returns ``(crop, notes)`` where ``crop`` has ``xCoordinate``,
    ``yCoordinate`` and ``width``. Raises ``UserInputError`` when the crop is
    out of range, overruns the image, or is below Google's minimum size.
    """
    notes: List[str] = []
    for label, value in (
        ("x_coordinate", x_coordinate),
        ("y_coordinate", y_coordinate),
        ("width", width),
    ):
        if value is not None and not (0.0 <= float(value) <= 1.0):
            raise UserInputError(f"{label} must be between 0 and 1; got {value}.")
    if width is not None and float(width) <= 0.0:
        raise UserInputError("width must be greater than 0.")

    dims_known = bool(image_width) and bool(image_height)

    if not dims_known:
        # Without pixel sizes nothing can be centred or size-checked. Fall
        # back to a full-width crop from the top and let Google validate.
        crop = {
            "xCoordinate": float(x_coordinate) if x_coordinate is not None else 0.0,
            "yCoordinate": float(y_coordinate) if y_coordinate is not None else 0.0,
            "width": float(width) if width is not None else 1.0,
        }
        if crop["xCoordinate"] + crop["width"] > 1.0 + _EPS:
            raise UserInputError(
                "x_coordinate + width must not exceed 1 (the crop would run off "
                "the right edge of the image)."
            )
        notes.append(
            "Image dimensions are not available from Drive, so the crop could "
            "not be centred or size-checked. Google will reject it if the "
            "cropped area is under 1280x144 px."
        )
        return _round_crop(crop), notes

    img_w = float(image_width)
    img_h = float(image_height)

    if width is None:
        # Largest 80:9 box that fits.
        full_width_crop_h = img_w * BANNER_ASPECT_H / BANNER_ASPECT_W
        if full_width_crop_h <= img_h:
            w = 1.0
        else:
            w = (img_h * BANNER_ASPECT_W / BANNER_ASPECT_H) / img_w
    else:
        w = float(width)

    crop_w_px = w * img_w
    crop_h_px = crop_w_px * BANNER_ASPECT_H / BANNER_ASPECT_W
    crop_h_frac = crop_h_px / img_h

    x = float(x_coordinate) if x_coordinate is not None else max(0.0, (1.0 - w) / 2)
    y = (
        float(y_coordinate)
        if y_coordinate is not None
        else max(0.0, (1.0 - crop_h_frac) / 2)
    )

    if x + w > 1.0 + _EPS:
        raise UserInputError(
            f"x_coordinate ({x}) + width ({w}) exceeds 1: the crop runs off the "
            "right edge of the image."
        )
    if y + crop_h_frac > 1.0 + _EPS:
        raise UserInputError(
            f"The crop runs off the bottom of the image: at width={w:.4f} the "
            f"80:9 banner is {crop_h_px:.0f}px tall, but only "
            f"{img_h - y * img_h:.0f}px remain below y_coordinate={y}. Use a "
            "smaller width or y_coordinate, or omit them for an automatic crop."
        )
    if (
        crop_w_px + _EPS < BANNER_MIN_WIDTH_PX
        or crop_h_px + _EPS < BANNER_MIN_HEIGHT_PX
    ):
        raise UserInputError(
            f"The cropped banner would be {crop_w_px:.0f}x{crop_h_px:.0f}px; "
            f"Google requires at least {BANNER_MIN_WIDTH_PX}x{BANNER_MIN_HEIGHT_PX}px. "
            f"The source image is {int(img_w)}x{int(img_h)}px. Use a larger "
            "image (1920x216px or bigger is a safe banner size) or a wider crop."
        )

    return _round_crop({"xCoordinate": x, "yCoordinate": y, "width": w}), notes


def _round_crop(crop: Dict[str, float]) -> Dict[str, float]:
    """Round to 6 dp without pushing the crop past the image edge."""
    rounded = {k: round(v, 6) for k, v in crop.items()}
    overrun = rounded["xCoordinate"] + rounded["width"] - 1.0
    if overrun > 0:
        rounded["xCoordinate"] = round(max(0.0, rounded["xCoordinate"] - overrun), 6)
    return rounded


def normalise_entity_images(entity_images: Dict[str, str]) -> Dict[str, str]:
    """Validate the bulk mapping and key it by canonical category name."""
    if not entity_images:
        raise UserInputError(
            "entity_images is required: map at least one of "
            f"{', '.join(THEME_CATEGORIES)} to an image file ID."
        )
    normalised: Dict[str, str] = {}
    for key, value in entity_images.items():
        category = _CATEGORY_LOOKUP.get(str(key).strip().lower())
        if category is None:
            raise UserInputError(
                f"Unknown category '{key}' in entity_images. Allowed: "
                f"{', '.join(THEME_CATEGORIES)}."
            )
        file_id = str(value or "").strip()
        if not file_id:
            raise UserInputError(f"entity_images['{key}'] has a blank image file ID.")
        normalised[category] = file_id
    return normalised


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def classify_registry_drive(row: Dict[str, str]) -> Tuple[Optional[str], str]:
    """Decide which banner category a drive gets, from its drive-level row.

    Precedence: ``restricted`` flag, then ``external`` flag, then ``hub``
    flag, then the ``entity`` column. The flags win because a restricted or
    external-share drive should look different from an ordinary entity drive
    even though it still belongs to an entity.

    Returns ``(category, reason)``; ``category`` is None when nothing matched.
    """
    if _truthy(row.get("restricted")):
        return "Restricted", "restricted=TRUE"
    if _truthy(row.get("external")):
        return "ExternalShare", "external=TRUE"
    if _truthy(row.get("hub")):
        return "Hub", "hub=TRUE"
    entity = (row.get("entity") or "").strip()
    category = _CATEGORY_LOOKUP.get(entity.lower())
    if category:
        return category, f"entity={entity}"
    return None, f"entity='{entity}' is not a known category"


def drives_from_registry(rows: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Collapse the Folder Registry (one row per folder) to one entry per drive.

    The drive ID comes from the drive-level row: ``depth`` 0, or the row whose
    ``path`` equals the drive name. Drives with no such row are returned with
    ``drive_id`` None so the caller can report them rather than guess an ID.
    """
    by_name: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for row in rows:
        name = (row.get("drive") or "").strip()
        if not name:
            continue
        if name not in by_name:
            by_name[name] = {"drive_name": name, "drive_id": None, "row": None}
            order.append(name)
        entry = by_name[name]
        depth = (row.get("depth") or "").strip()
        path = (row.get("path") or "").strip()
        is_drive_row = depth == "0" or (not depth and path == name)
        if is_drive_row and entry["row"] is None:
            entry["row"] = row
            entry["drive_id"] = (row.get("folder_id") or "").strip() or None
    return [by_name[n] for n in order]


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def _http_status(error: HttpError) -> Optional[int]:
    return getattr(getattr(error, "resp", None), "status", None)


async def resolve_theme_access(
    service, drive_id: str, use_domain_admin_access: Optional[bool]
) -> Tuple[Dict[str, Any], bool]:
    """Work out how the caller may change this drive's banner.

    ``use_domain_admin_access``:

    * ``True``: act as a domain admin. Google refuses if the caller is not one.
    * ``False``: act as a drive member; require ``canChangeDriveBackground``
      (drive Manager).
    * ``None`` (auto): use member access when the caller is a Manager, else
      try domain-admin access, else refuse.

    Returns ``(drive, admin_mode)``.
    """
    admin_refusal = UserInputError(
        f"Cannot change the banner on shared drive {drive_id}: the caller is "
        "not a Manager of the drive and domain-admin access was refused. Ask a "
        "Manager to run it, or grant Manager (organizer) access first."
    )

    if use_domain_admin_access is True:
        try:
            drive = await _get_shared_drive(
                service, drive_id, use_domain_admin_access=True, fields=_THEME_FIELDS
            )
        except HttpError as error:
            if _http_status(error) in (401, 403):
                raise UserInputError(
                    f"Domain-admin access to shared drive {drive_id} was refused "
                    f"(HTTP {_http_status(error)}). The caller must be a "
                    "Workspace admin to use use_domain_admin_access=True."
                ) from error
            raise
        if drive is None:
            raise UserInputError(f"'{drive_id}' is not a shared drive.")
        return drive, True

    try:
        drive = await _get_shared_drive(service, drive_id, fields=_THEME_FIELDS)
    except HttpError as error:
        if _http_status(error) not in (401, 403):
            raise
        drive = None

    if drive is not None:
        capabilities = drive.get("capabilities") or {}
        if capabilities.get("canChangeDriveBackground"):
            return drive, False
        if use_domain_admin_access is False:
            raise UserInputError(
                f"You cannot change the banner on '{drive.get('name', drive_id)}' "
                f"({drive_id}): only a Manager of the drive can. Ask a Manager, "
                "or re-run with use_domain_admin_access=True if you are a "
                "Workspace admin."
            )
    elif use_domain_admin_access is False:
        raise UserInputError(
            f"'{drive_id}' is not a shared drive you are a member of. If you "
            "are a Workspace admin, re-run with use_domain_admin_access=True."
        )

    # Auto mode: member access was not enough, so try as a domain admin.
    try:
        admin_drive = await _get_shared_drive(
            service, drive_id, use_domain_admin_access=True, fields=_THEME_FIELDS
        )
    except HttpError as error:
        if _http_status(error) in (401, 403):
            raise admin_refusal from error
        raise
    if admin_drive is None:
        if drive is None:
            raise UserInputError(f"'{drive_id}' is not a shared drive.")
        raise admin_refusal
    return admin_drive, True


async def _get_banner_image(service, image_file_id: str) -> Dict[str, Any]:
    """Fetch and validate the image that will become the banner."""
    try:
        meta = await execute_with_backoff(
            lambda: service.files().get(
                fileId=image_file_id,
                fields="id, name, mimeType, trashed, imageMediaMetadata(width, height)",
                supportsAllDrives=True,
            ),
            label="files.get(banner-image)",
        )
    except HttpError as error:
        if _http_status(error) == 404:
            raise UserInputError(
                f"Image file '{image_file_id}' was not found, or you cannot see it."
            ) from error
        raise
    mime = meta.get("mimeType")
    if mime not in ALLOWED_IMAGE_MIME_TYPES:
        raise UserInputError(
            f"'{meta.get('name', image_file_id)}' is {mime}; a banner must be a "
            "JPG or PNG."
        )
    if meta.get("trashed"):
        raise UserInputError(
            f"'{meta.get('name', image_file_id)}' is in the trash; restore it first."
        )
    return meta


async def _validate_theme_id(service, theme_id: str) -> str:
    """Check ``theme_id`` against ``about.get(driveThemes)``.

    Returns a note for the output. An unknown theme is refused; if the theme
    list itself cannot be read, the call goes ahead and Drive has the final
    word.
    """
    try:
        about = await execute_with_backoff(
            lambda: service.about().get(fields="driveThemes(id)"),
            label="about.get(driveThemes)",
        )
    except Exception as exc:  # noqa: BLE001 - validation is best effort
        logger.info("[set_shared_drive_theme] could not list drive themes: %s", exc)
        return (
            "\n   ℹ️ Could not read the list of Google themes to check the "
            "theme_id; Drive will reject it if it is not valid."
        )
    valid = sorted(t.get("id") for t in (about.get("driveThemes") or []) if t.get("id"))
    if valid and theme_id not in valid:
        raise UserInputError(
            f"'{theme_id}' is not a Google shared drive theme. Valid theme IDs: "
            f"{', '.join(valid)}."
        )
    return ""


def _theme_snapshot(drive: Dict[str, Any]) -> Dict[str, Optional[str]]:
    return {
        "themeId": drive.get("themeId"),
        "colorRgb": drive.get("colorRgb"),
        "backgroundImageLink": drive.get("backgroundImageLink"),
    }


def _format_snapshot(label: str, snap: Dict[str, Optional[str]]) -> List[str]:
    return [
        f"   {label}:",
        f"     themeId: {snap.get('themeId') or '(custom/none)'}",
        f"     colorRgb: {snap.get('colorRgb') or '(none)'}",
        f"     backgroundImageLink: {snap.get('backgroundImageLink') or '(none)'}",
    ]


async def apply_drive_theme(
    service,
    *,
    drive_id: str,
    theme_id: Optional[str] = None,
    image_file_id: Optional[str] = None,
    x_coordinate: Optional[float] = None,
    y_coordinate: Optional[float] = None,
    width: Optional[float] = None,
    use_domain_admin_access: Optional[bool] = None,
    dry_run: bool = False,
    image_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Set one drive's banner. Shared by the single and bulk tools.

    ``image_meta`` lets the bulk tool pass an image it already validated, so a
    shared image is fetched once per run rather than once per drive.

    Returns a dict with ``drive_id``, ``name``, ``admin_mode``, ``before``,
    ``after`` (None on dry run), ``body`` and ``notes``.
    """
    theme_id = (theme_id or "").strip() or None
    image_file_id = (image_file_id or "").strip() or None
    if bool(theme_id) == bool(image_file_id):
        raise UserInputError("Pass exactly one of theme_id or image_file_id.")
    if theme_id and any(v is not None for v in (x_coordinate, y_coordinate, width)):
        raise UserInputError(
            "Crop values (x_coordinate, y_coordinate, width) only apply to "
            "image_file_id, not theme_id."
        )

    drive, admin_mode = await resolve_theme_access(
        service, drive_id, use_domain_admin_access
    )
    before = _theme_snapshot(drive)
    notes: List[str] = []

    if theme_id:
        note = await _validate_theme_id(service, theme_id)
        if note:
            notes.append(note.strip())
        body: Dict[str, Any] = {"themeId": theme_id}
    else:
        meta = image_meta or await _get_banner_image(service, image_file_id)
        dims = meta.get("imageMediaMetadata") or {}
        crop, crop_notes = compute_banner_crop(
            dims.get("width"), dims.get("height"), x_coordinate, y_coordinate, width
        )
        notes.extend(crop_notes)
        body = {"backgroundImageFile": {"id": image_file_id, **crop}}

    result: Dict[str, Any] = {
        "drive_id": drive_id,
        "name": drive.get("name", drive_id),
        "admin_mode": admin_mode,
        "before": before,
        "after": None,
        "body": body,
        "notes": notes,
    }
    if dry_run:
        return result

    await execute_with_backoff(
        lambda: service.drives().update(
            driveId=drive_id,
            body=body,
            useDomainAdminAccess=admin_mode,
            fields="id, themeId, colorRgb, backgroundImageLink",
        ),
        label="drives.update(theme)",
    )

    # Re-read rather than trusting the update response, same as
    # update_shared_drive: the change counts once drives.get shows it.
    after_drive = await _get_shared_drive(
        service, drive_id, use_domain_admin_access=admin_mode, fields=_THEME_FIELDS
    )
    after = _theme_snapshot(after_drive or {})
    result["after"] = after

    if theme_id and after.get("themeId") != theme_id:
        notes.append(
            f"⚠️ drives.get reports themeId={after.get('themeId')!r} after the "
            "update; verify the banner in the Drive UI."
        )
    if image_file_id and (
        not after.get("backgroundImageLink")
        or after.get("backgroundImageLink") == before.get("backgroundImageLink")
    ):
        notes.append(
            "⚠️ The banner image link did not change after the update; verify "
            "the banner in the Drive UI."
        )

    logger.info(
        "[set_shared_drive_theme] %s on %s (%s) admin_mode=%s",
        f"themeId={theme_id}" if theme_id else f"image={image_file_id}",
        result["name"],
        drive_id,
        admin_mode,
    )
    return result


def _format_result(result: Dict[str, Any], dry_run: bool) -> List[str]:
    access = "domain admin" if result["admin_mode"] else "drive Manager"
    body = result["body"]
    if "themeId" in body:
        change = f"themeId={body['themeId']}"
    else:
        image = body["backgroundImageFile"]
        change = (
            f"image={image['id']} crop(x={image['xCoordinate']}, "
            f"y={image['yCoordinate']}, width={image['width']})"
        )
    head = "DRY RUN — no change applied." if dry_run else "✅ Banner updated."
    lines = [
        head,
        f"   Shared drive: '{result['name']}' ({result['drive_id']})",
        f"   Access: {access}",
        f"   {'Would set' if dry_run else 'Set'}: {change}",
    ]
    lines += _format_snapshot("Before", result["before"])
    if result["after"] is not None:
        lines += _format_snapshot("After", result["after"])
    lines += [f"   {n}" for n in result["notes"]]
    return lines


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@server.tool()
@handle_http_errors("get_shared_drive_theme", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def get_shared_drive_theme(
    service,
    user_google_email: str,
    drive_id: str,
    use_domain_admin_access: bool = False,
) -> str:
    """
    Reads a shared drive's banner: its theme ID, colour and image link.

    Args:
        user_google_email (str): The user's Google email address. Required.
        drive_id (str): ID of the shared drive. Required.
        use_domain_admin_access (bool): Read as a domain admin, for drives the
            caller is not a member of. Defaults to False.

    Returns:
        str: themeId, colorRgb and backgroundImageLink. themeId is empty when
            the banner is a custom image. The image link is short-lived.
    """
    if not drive_id or not drive_id.strip():
        raise UserInputError("drive_id is required.")
    drive = await _get_shared_drive(
        service,
        drive_id,
        use_domain_admin_access=use_domain_admin_access,
        fields=_THEME_FIELDS,
    )
    if drive is None:
        raise UserInputError(
            f"'{drive_id}' is not a shared drive you can see. If you are a "
            "Workspace admin, retry with use_domain_admin_access=True."
        )
    snap = _theme_snapshot(drive)
    can_change = (drive.get("capabilities") or {}).get("canChangeDriveBackground")
    lines = [f"Shared drive '{drive.get('name', drive_id)}' ({drive_id}) banner:"]
    lines += [
        f"   themeId: {snap['themeId'] or '(custom/none)'}",
        f"   colorRgb: {snap['colorRgb'] or '(none)'}",
        f"   backgroundImageLink: {snap['backgroundImageLink'] or '(none)'}",
        f"   You can change it: {'yes' if can_change else 'no (Manager only)'}",
    ]
    return "\n".join(lines)


@server.tool()
@handle_http_errors("set_shared_drive_theme", service_type="drive")
@require_google_service("drive", "drive_full")
async def set_shared_drive_theme(
    service,
    user_google_email: str,
    drive_id: str,
    theme_id: Optional[str] = None,
    image_file_id: Optional[str] = None,
    x_coordinate: Optional[float] = None,
    y_coordinate: Optional[float] = None,
    width: Optional[float] = None,
    use_domain_admin_access: Optional[bool] = None,
    dry_run: bool = False,
) -> str:
    """
    Sets a shared drive's banner, from a Google stock theme or a JPG/PNG in Drive.

    Pass exactly one of ``theme_id`` or ``image_file_id``.

    For an image, the crop is the largest centred 80:9 area of the image
    unless you pass crop values. Google needs the cropped area to be at
    least 1280x144 px, so 1920x216 px or larger is a safe banner size.

    Access: a drive Manager changes it as a member. When the caller is not a
    Manager, domain-admin access is tried automatically (leave
    ``use_domain_admin_access`` unset), and the call is refused if that fails.

    Args:
        user_google_email (str): The user's Google email address. Required.
        drive_id (str): ID of the shared drive. Required.
        theme_id (Optional[str]): A Google stock theme ID (see
            ``about.get`` driveThemes). Sets image and colour.
        image_file_id (Optional[str]): ID of a JPG or PNG already in Drive.
        x_coordinate (Optional[float]): Left edge of the crop, 0 to 1 of the
            image width. Default: centred.
        y_coordinate (Optional[float]): Top edge of the crop, 0 to 1 of the
            image height. Default: centred.
        width (Optional[float]): Crop width, 0 to 1 of the image width.
            Height follows from the 80:9 ratio. Default: the largest that fits.
        use_domain_admin_access (Optional[bool]): True to act as a domain
            admin, False to require Manager, unset to pick automatically.
        dry_run (bool): Validate and report without changing anything.

    Returns:
        str: Before and after theme ID, colour and image link.
    """
    if not drive_id or not drive_id.strip():
        raise UserInputError("drive_id is required.")
    result = await apply_drive_theme(
        service,
        drive_id=drive_id.strip(),
        theme_id=theme_id,
        image_file_id=image_file_id,
        x_coordinate=x_coordinate,
        y_coordinate=y_coordinate,
        width=width,
        use_domain_admin_access=use_domain_admin_access,
        dry_run=dry_run,
    )
    return "\n".join(_format_result(result, dry_run))


@server.tool()
@handle_http_errors("set_shared_drive_themes_from_registry", service_type="drive")
@require_google_service("drive", "drive_full")
async def set_shared_drive_themes_from_registry(
    service,
    user_google_email: str,
    registry_spreadsheet_id: str,
    entity_images: Dict[str, str],
    registry_range: str = "FolderRegistry",
    use_domain_admin_access: Optional[bool] = None,
    dry_run: bool = False,
) -> str:
    """
    Brands every shared drive in the Folder Registry with its entity's banner.

    Reads the registry (one row per folder), takes each drive's drive-level
    row (``depth`` 0) for its ID, picks a category, and applies the mapped
    image with an automatic centred crop.

    Category precedence per drive: ``restricted`` flag → Restricted,
    ``external`` flag → ExternalShare, ``hub`` flag → Hub, otherwise the
    ``entity`` column (OTB, JIT, VALE, BIR). Run with ``dry_run=True`` first
    and check each drive's category and reason.

    Each drive is handled on its own: one failure is reported and the run
    carries on. Every image is checked once, before any drive is touched.

    Args:
        user_google_email (str): The user's Google email address. Required.
        registry_spreadsheet_id (str): Spreadsheet ID of the Folder Registry.
        entity_images (Dict[str, str]): Category → image file ID. Keys from
            OTB, JIT, VALE, BIR, Restricted, Hub, ExternalShare (any case).
            Categories left out are skipped.
        registry_range (str): Sheet name or A1 range. Defaults to
            "FolderRegistry". Needs ``drive``, ``folder_id`` and ``entity``
            columns; ``depth``, ``path``, ``restricted``, ``external`` and
            ``hub`` are used when present.
        use_domain_admin_access (Optional[bool]): As for
            set_shared_drive_theme. Unset picks per drive.
        dry_run (bool): Report the plan without changing any drive.

    Returns:
        str: One block per drive (applied, would apply, skipped or failed)
            and a summary count.
    """
    if not registry_spreadsheet_id or not registry_spreadsheet_id.strip():
        raise UserInputError("registry_spreadsheet_id is required.")
    images = normalise_entity_images(entity_images)

    # Check every image up-front so a bad file ID stops the run before any
    # drive changes, instead of half-branding the estate.
    image_meta: Dict[str, Dict[str, Any]] = {}
    for file_id in sorted(set(images.values())):
        meta = await _get_banner_image(service, file_id)
        dims = meta.get("imageMediaMetadata") or {}
        compute_banner_crop(dims.get("width"), dims.get("height"))
        image_meta[file_id] = meta

    try:
        sheets_service = await _hub_registry_service(
            user_google_email=user_google_email
        )
    except Exception as exc:  # noqa: BLE001 - re-raised with actionable guidance
        raise UserInputError(
            "set_shared_drive_themes_from_registry needs read access to the "
            "Folder Registry spreadsheet, and the Sheets scope is not available "
            "on these credentials. Enable the 'sheets' service alongside "
            "'drive' and re-authenticate. Underlying error: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    rows = await _read_registry_rows(
        sheets_service, registry_spreadsheet_id.strip(), registry_range
    )
    columns = set().union(*(r.keys() for r in rows)) if rows else set()
    missing = {"drive", "folder_id", "entity"} - columns
    if missing:
        raise UserInputError(
            f"Registry is missing column(s) {sorted(missing)}; found {sorted(columns)}."
        )

    drives = drives_from_registry(rows)
    counts = {"applied": 0, "planned": 0, "skipped": 0, "failed": 0}
    blocks: List[str] = []

    for entry in drives:
        name = entry["drive_name"]
        if entry["drive_id"] is None:
            counts["skipped"] += 1
            blocks.append(
                f"⏭️ '{name}': skipped, no drive-level (depth 0) row with a "
                "folder_id in the registry."
            )
            continue
        category, reason = classify_registry_drive(entry["row"])
        if category is None:
            counts["skipped"] += 1
            blocks.append(f"⏭️ '{name}' ({entry['drive_id']}): skipped, {reason}.")
            continue
        if category not in images:
            counts["skipped"] += 1
            blocks.append(
                f"⏭️ '{name}' ({entry['drive_id']}): skipped, category "
                f"{category} ({reason}) has no image in entity_images."
            )
            continue

        file_id = images[category]
        try:
            result = await apply_drive_theme(
                service,
                drive_id=entry["drive_id"],
                image_file_id=file_id,
                use_domain_admin_access=use_domain_admin_access,
                dry_run=dry_run,
                image_meta=image_meta[file_id],
            )
        except Exception as exc:  # noqa: BLE001 - per-drive failure, run continues
            counts["failed"] += 1
            logger.warning(
                "[set_shared_drive_themes_from_registry] %s (%s) failed: %s",
                name,
                entry["drive_id"],
                exc,
            )
            blocks.append(
                f"❌ '{name}' ({entry['drive_id']}): {category} ({reason}) failed: "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        counts["planned" if dry_run else "applied"] += 1
        lines = _format_result(result, dry_run)
        lines.insert(1, f"   Category: {category} ({reason})")
        blocks.append("\n".join(lines))

    verb = "would apply" if dry_run else "applied"
    done = counts["planned"] if dry_run else counts["applied"]
    header = (
        f"{'DRY RUN — ' if dry_run else ''}Shared drive banners from registry "
        f"{registry_spreadsheet_id}: {len(drives)} drive(s); {verb} {done}, "
        f"skipped {counts['skipped']}, failed {counts['failed']}."
    )
    return "\n\n".join([header] + blocks)


__all__ = [
    "get_shared_drive_theme",
    "set_shared_drive_theme",
    "set_shared_drive_themes_from_registry",
    "apply_drive_theme",
    "compute_banner_crop",
    "classify_registry_drive",
    "drives_from_registry",
    "normalise_entity_images",
    "resolve_theme_access",
]

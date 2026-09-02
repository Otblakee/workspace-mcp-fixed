"""
Google Drive Helper Functions

Shared utilities for Google Drive operations including permission checking.
"""

import asyncio
import os
import re
import time
from typing import List, Dict, Any, Optional, Set, Tuple

VALID_SHARE_ROLES = {"reader", "commenter", "writer"}
VALID_SHARE_TYPES = {"user", "group", "domain", "anyone"}


def check_public_link_permission(permissions: List[Dict[str, Any]]) -> bool:
    """
    Check if file has 'anyone with the link' permission.

    Args:
        permissions: List of permission objects from Google Drive API

    Returns:
        bool: True if file has public link sharing enabled
    """
    return any(
        p.get("type") == "anyone" and p.get("role") in ["reader", "writer", "commenter"]
        for p in permissions
    )


def format_public_sharing_error(file_name: str, file_id: str) -> str:
    """
    Format error message for files without public sharing.

    Args:
        file_name: Name of the file
        file_id: Google Drive file ID

    Returns:
        str: Formatted error message
    """
    return (
        f"❌ Permission Error: '{file_name}' not shared publicly. "
        f"Set 'Anyone with the link' → 'Viewer' in Google Drive sharing. "
        f"File: https://drive.google.com/file/d/{file_id}/view"
    )


def get_drive_image_url(file_id: str) -> str:
    """
    Get the correct Drive URL format for publicly shared images.

    Args:
        file_id: Google Drive file ID

    Returns:
        str: URL for embedding Drive images
    """
    return f"https://drive.google.com/uc?export=view&id={file_id}"


def validate_share_role(role: str) -> None:
    """
    Validate that the role is valid for sharing.

    Args:
        role: The permission role to validate

    Raises:
        ValueError: If role is not reader, commenter, or writer
    """
    if role not in VALID_SHARE_ROLES:
        raise ValueError(
            f"Invalid role '{role}'. Must be one of: {', '.join(sorted(VALID_SHARE_ROLES))}"
        )


def validate_share_type(share_type: str) -> None:
    """
    Validate that the share type is valid.

    Args:
        share_type: The type of sharing to validate

    Raises:
        ValueError: If share_type is not user, group, domain, or anyone
    """
    if share_type not in VALID_SHARE_TYPES:
        raise ValueError(
            f"Invalid share_type '{share_type}'. Must be one of: {', '.join(sorted(VALID_SHARE_TYPES))}"
        )


RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


def validate_expiration_time(expiration_time: str) -> None:
    """
    Validate that expiration_time is in RFC 3339 format.

    Args:
        expiration_time: The expiration time string to validate

    Raises:
        ValueError: If expiration_time is not valid RFC 3339 format
    """
    if not RFC3339_PATTERN.match(expiration_time):
        raise ValueError(
            f"Invalid expiration_time '{expiration_time}'. "
            "Must be RFC 3339 format (e.g., '2025-01-15T00:00:00Z')"
        )


def format_permission_info(permission: Dict[str, Any]) -> str:
    """
    Format a permission object for display.

    Args:
        permission: Permission object from Google Drive API

    Returns:
        str: Human-readable permission description with ID
    """
    perm_type = permission.get("type", "unknown")
    role = permission.get("role", "unknown")
    perm_id = permission.get("id", "")

    if perm_type == "anyone":
        base = f"Anyone with the link ({role}) [id: {perm_id}]"
    elif perm_type == "user":
        email = permission.get("emailAddress", "unknown")
        base = f"User: {email} ({role}) [id: {perm_id}]"
    elif perm_type == "group":
        email = permission.get("emailAddress", "unknown")
        base = f"Group: {email} ({role}) [id: {perm_id}]"
    elif perm_type == "domain":
        domain = permission.get("domain", "unknown")
        base = f"Domain: {domain} ({role}) [id: {perm_id}]"
    else:
        base = f"{perm_type} ({role}) [id: {perm_id}]"

    extras = []
    if permission.get("expirationTime"):
        extras.append(f"expires: {permission['expirationTime']}")

    perm_details = permission.get("permissionDetails", [])
    if perm_details:
        for detail in perm_details:
            if detail.get("inherited") and detail.get("inheritedFrom"):
                extras.append(f"inherited from: {detail['inheritedFrom']}")
                break

    if extras:
        return f"{base} | {', '.join(extras)}"
    return base


# Precompiled regex patterns for Drive query detection
DRIVE_QUERY_PATTERNS = [
    re.compile(r'\b\w+\s*(=|!=|>|<)\s*[\'"].*?[\'"]', re.IGNORECASE),  # field = 'value'
    re.compile(r"\b\w+\s*(=|!=|>|<)\s*\d+", re.IGNORECASE),  # field = number
    re.compile(r"\bcontains\b", re.IGNORECASE),  # contains operator
    re.compile(r"\bin\s+parents\b", re.IGNORECASE),  # in parents
    re.compile(r"\bhas\s*\{", re.IGNORECASE),  # has {properties}
    re.compile(r"\btrashed\s*=\s*(true|false)\b", re.IGNORECASE),  # trashed=true/false
    re.compile(r"\bstarred\s*=\s*(true|false)\b", re.IGNORECASE),  # starred=true/false
    re.compile(
        r'[\'"][^\'"]+[\'"]\s+in\s+parents', re.IGNORECASE
    ),  # 'parentId' in parents
    re.compile(r"\bfullText\s+contains\b", re.IGNORECASE),  # fullText contains
    re.compile(r"\bname\s*(=|contains)\b", re.IGNORECASE),  # name = or name contains
    re.compile(r"\bmimeType\s*(=|!=)\b", re.IGNORECASE),  # mimeType operators
]


def build_drive_list_params(
    query: str,
    page_size: int,
    drive_id: Optional[str] = None,
    include_items_from_all_drives: bool = True,
    corpora: Optional[str] = None,
    detailed: bool = True,
) -> Dict[str, Any]:
    """
    Helper function to build common list parameters for Drive API calls.

    Args:
        query: The search query string
        page_size: Maximum number of items to return
        drive_id: Optional shared drive ID
        include_items_from_all_drives: Whether to include items from all drives
        corpora: Optional corpus specification
        detailed: Whether to request size, modifiedTime, and webViewLink fields.
                  Defaults to True to preserve existing behavior.

    Returns:
        Dictionary of parameters for Drive API list calls
    """
    if detailed:
        fields = "nextPageToken, files(id, name, mimeType, webViewLink, iconLink, modifiedTime, size)"
    else:
        fields = "nextPageToken, files(id, name, mimeType)"
    list_params = {
        "q": query,
        "pageSize": page_size,
        "fields": fields,
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": include_items_from_all_drives,
    }

    if drive_id:
        list_params["driveId"] = drive_id
        if corpora:
            list_params["corpora"] = corpora
        else:
            list_params["corpora"] = "drive"
    elif corpora:
        list_params["corpora"] = corpora

    return list_params


SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
BASE_SHORTCUT_FIELDS = (
    "id, mimeType, parents, shortcutDetails(targetId, targetMimeType)"
)


async def resolve_drive_item(
    service,
    file_id: str,
    *,
    extra_fields: Optional[str] = None,
    max_depth: int = 5,
) -> Tuple[str, Dict[str, Any]]:
    """
    Resolve a Drive shortcut so downstream callers operate on the real item.

    Returns the resolved file ID and its metadata. Raises if shortcut targets loop
    or exceed max_depth to avoid infinite recursion.
    """
    current_id = file_id
    depth = 0
    fields = BASE_SHORTCUT_FIELDS
    if extra_fields:
        fields = f"{fields}, {extra_fields}"

    while True:
        metadata = await asyncio.to_thread(
            service.files()
            .get(fileId=current_id, fields=fields, supportsAllDrives=True)
            .execute
        )
        mime_type = metadata.get("mimeType")
        if mime_type != SHORTCUT_MIME_TYPE:
            return current_id, metadata

        shortcut_details = metadata.get("shortcutDetails") or {}
        target_id = shortcut_details.get("targetId")
        if not target_id:
            raise Exception(f"Shortcut '{current_id}' is missing target details.")

        depth += 1
        if depth > max_depth:
            raise Exception(
                f"Shortcut resolution exceeded {max_depth} hops starting from '{file_id}'."
            )
        current_id = target_id


def get_holding_folder_id() -> str:
    """Resolve the soft-delete holding folder ID from the environment.

    Read lazily (not at import) so the value can be set per-deploy and so
    tests can patch it. Raises if unset: soft-delete fails closed rather than
    silently doing nothing.
    """
    folder_id = os.getenv("DRIVE_HOLDING_FOLDER_ID", "").strip()
    if not folder_id:
        raise Exception(
            "DRIVE_HOLDING_FOLDER_ID is not set. Refusing to soft-delete: set it "
            "to the ID of a private holding folder you own and empty manually."
        )
    return folder_id


INTERNAL_SHARED_DRIVES_ENV = "DRIVE_INTERNAL_SHARED_DRIVE_IDS"
_SHARED_DRIVE_TRUST_TTL_S = 300.0
_shared_drive_trust: Dict[str, Tuple[float, bool, str]] = {}


def _internal_shared_drive_ids() -> Set[str]:
    raw = os.getenv(INTERNAL_SHARED_DRIVES_ENV, "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def _organizer_addresses(perm: Dict[str, Any]) -> List[str]:
    """Addresses (or domains, as ``@domain``) a permission entry grants to."""
    kind = (perm.get("type") or "").lower()
    if kind in ("user", "group"):
        return [perm.get("emailAddress") or ""]
    if kind == "domain":
        return [f"organizer@{perm.get('domain') or ''}"]
    return ["anyone@"]  # anyone/unknown: never internal


async def _shared_drive_is_internal(service, drive_id: str) -> Tuple[bool, str]:
    """Whether the shared drive is controlled by this organisation.

    Drive v3 exposes no owning-customer field to an ordinary member, and a
    user can be a member of a drive another organisation owns. The tenant
    signal an ordinary member *can* read is the drive's organizer list: a
    drive is treated as internal only when it is named in
    ``DRIVE_INTERNAL_SHARED_DRIVE_IDS``, or when every organizer is on an
    internal domain (and at least one is visible). Anything else, including a
    permissions listing the caller may not read, is external. Verdicts are
    cached per process for a few minutes.
    """
    from core.access_policy import external_addresses

    if drive_id in _internal_shared_drive_ids():
        return True, "listed in DRIVE_INTERNAL_SHARED_DRIVE_IDS"
    cached = _shared_drive_trust.get(drive_id)
    now = time.monotonic()
    if cached and now - cached[0] < _SHARED_DRIVE_TRUST_TTL_S:
        return cached[1], cached[2]

    organizers: List[str] = []
    try:
        token = None
        pages = 0
        while True:
            pages += 1
            if pages > 20:  # a shared drive never has 2,000 members
                raise RuntimeError("organizer listing did not terminate")
            page = await asyncio.to_thread(
                service.permissions()
                .list(
                    fileId=drive_id,
                    supportsAllDrives=True,
                    useDomainAdminAccess=False,
                    pageSize=100,
                    pageToken=token,
                    fields="nextPageToken, permissions(role, type, emailAddress, domain)",
                )
                .execute
            )
            entries = page.get("permissions") if isinstance(page, dict) else None
            for perm in entries or []:
                if not isinstance(perm, dict):
                    continue
                if (perm.get("role") or "").lower() == "organizer":
                    organizers.extend(_organizer_addresses(perm))
            token = page.get("nextPageToken") if isinstance(page, dict) else None
            if not isinstance(token, str) or not token:
                break
    except Exception as exc:  # 403 for non-organizers, network, anything
        verdict = (
            False,
            f"its organizers could not be read ({exc.__class__.__name__})",
        )
        _shared_drive_trust[drive_id] = (now, *verdict)
        return verdict

    if not organizers:
        verdict = (False, "no organizer is visible")
    else:
        outside = external_addresses(organizers)
        if outside:
            verdict = (
                False,
                f"it has organizers outside the organisation ({', '.join(outside)})",
            )
        else:
            verdict = (True, "every organizer is on an internal domain")
    _shared_drive_trust[drive_id] = (now, *verdict)
    return verdict


async def assert_internal_destination(
    service, resolved_folder_id: str, *, action: str
) -> None:
    """Refuse to write into a destination controlled outside the organisation
    unless the caller holds the ``external_share`` capability.

    Anything created in, copied to or moved into a folder is readable by
    whoever controls it, so an externally owned destination is a share. For
    a My Drive folder that is its owner; for a shared drive it is the
    drive's organizers (see ``_shared_drive_is_internal``), since a user can
    be a member of a drive another organisation owns. Inert until
    OAUTH_ALLOWED_EMAIL_DOMAINS defines "inside".
    """
    from core.access_policy import (
        caller_has_capability,
        external_addresses,
        internal_email_domains,
    )

    if not internal_email_domains() or not resolved_folder_id:
        return
    if resolved_folder_id == "root":
        return
    meta = await asyncio.to_thread(
        service.files()
        .get(
            fileId=resolved_folder_id,
            fields="id, driveId, owners(emailAddress)",
            supportsAllDrives=True,
        )
        .execute
    )
    drive_id = meta.get("driveId") if isinstance(meta, dict) else None
    if isinstance(drive_id, str) and drive_id:
        internal, why = await _shared_drive_is_internal(service, drive_id)
        if internal or await caller_has_capability("external_share"):
            return
        from core.utils import UserInputError

        raise UserInputError(
            f"{action} refused: destination {resolved_folder_id} is in shared "
            f"drive {drive_id}, which is not treated as internal because {why}. "
            "Anything written there is readable by the drive's organizers, so "
            "this needs the 'external_share' capability from the access policy. "
            f"If the drive is this organisation's, add its ID to "
            f"{INTERNAL_SHARED_DRIVES_ENV}."
        )
    owners = [o.get("emailAddress") for o in (meta.get("owners") or [])]
    outside = external_addresses(owners)
    if outside and not await caller_has_capability("external_share"):
        from core.utils import UserInputError

        raise UserInputError(
            f"{action} refused: destination folder {resolved_folder_id} is owned "
            f"by {', '.join(outside)}, outside "
            f"{', '.join(sorted(internal_email_domains()))}. Anything written "
            "there is readable by its owner, so this needs the 'external_share' "
            "capability from the access policy."
        )


async def resolve_folder_id(
    service,
    folder_id: str,
    *,
    max_depth: int = 5,
) -> str:
    """
    Resolve a folder ID that might be a shortcut and ensure the final target is a folder.
    """
    resolved_id, metadata = await resolve_drive_item(
        service,
        folder_id,
        max_depth=max_depth,
    )
    mime_type = metadata.get("mimeType")
    if mime_type != FOLDER_MIME_TYPE:
        raise Exception(
            f"Resolved ID '{resolved_id}' (from '{folder_id}') is not a folder; mimeType={mime_type}."
        )
    return resolved_id

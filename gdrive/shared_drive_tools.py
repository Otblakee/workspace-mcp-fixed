"""
Shared drive, permission and shortcut tools.

These are the P1 blockers for the OTB target Drive architecture build
(OTB_IT_TargetDriveArchitecture_2026-07-31_v1): creating and configuring the
new shared drives, granting the group-level access that the Drive_Permissions
rows describe, and building the shortcut-based navigation layer.

Security posture
----------------
``core/tool_policy.py`` blocks the legacy, unguarded sharing tools
(``share_drive_file``, ``set_drive_file_permissions``, ``update_drive_permission``,
``remove_drive_permission``). Those names stay blocked. The tools here are their
guarded replacements:

* ``set_drive_permission`` grants to a **group** by default; an individual grant
  requires ``allow_individual=True`` passed explicitly. ``anyone`` and
  ``domain`` principals are refused outright, so no tool on this server can
  create a public link.
* ``revoke_drive_permission`` refuses to revoke the caller's own access and
  refuses to remove the last organizer of a shared drive, so it cannot orphan
  a drive or lock the operator out.

The removal tool is deliberately **not** named ``remove_drive_permission``:
that name is on the hard denylist in ``core/tool_policy.py`` and would be
refused at the registration chokepoint. Un-blocking it would also re-expose the
unguarded legacy implementation still present in ``gdrive/drive_tools.py``.
``revoke_drive_permission`` is the guarded equivalent.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from googleapiclient.errors import HttpError

from auth.service_decorator import require_google_service
from core.server import server
from core.utils import UserInputError, handle_http_errors
from gdrive.drive_batch import (
    SHORTCUT_MIME_TYPE,
    execute_with_backoff,
    paginate,
    resolve_principal,
    validate_drive_role,
)
from gdrive.drive_helpers import (
    format_permission_info,
    resolve_folder_id,
    validate_expiration_time,
)

logger = logging.getLogger(__name__)

_DRIVE_FIELDS = (
    "id, name, themeId, colorRgb, createdTime, hidden, "
    "restrictions(adminManagedRestrictions, copyRequiresWriterPermission, "
    "domainUsersOnly, driveMembersOnly, sharingFoldersRequiresOrganizerPermission)"
)
_PERMISSION_FIELDS = (
    "nextPageToken, permissions(id, type, role, emailAddress, domain, "
    "displayName, expirationTime, deleted)"
)


async def _get_shared_drive(service, drive_id: str) -> Optional[Dict[str, Any]]:
    """Return the shared drive resource for ``drive_id``, or None if it is a file."""
    try:
        return await execute_with_backoff(
            lambda: service.drives().get(driveId=drive_id, fields=_DRIVE_FIELDS),
            label="drives.get",
        )
    except HttpError as error:
        if getattr(getattr(error, "resp", None), "status", None) in (404, 400):
            return None
        raise


async def _describe_target(service, target_id: str) -> Dict[str, Any]:
    """Classify a permission target as a shared drive or an ordinary file.

    Shared-drive IDs and their root-folder IDs are the same value, so callers
    can pass either and get consistent behaviour.
    """
    drive = await _get_shared_drive(service, target_id)
    if drive is not None:
        return {"kind": "drive", "id": target_id, "name": drive.get("name", target_id)}

    meta = await execute_with_backoff(
        lambda: service.files().get(
            fileId=target_id,
            fields="id, name, mimeType, driveId",
            supportsAllDrives=True,
        ),
        label="files.get",
    )
    return {
        "kind": "file",
        "id": meta.get("id", target_id),
        "name": meta.get("name", target_id),
        "mimeType": meta.get("mimeType"),
    }


async def _list_permissions(service, target_id: str) -> List[Dict[str, Any]]:
    return await paginate(
        lambda token: service.permissions().list(
            fileId=target_id,
            supportsAllDrives=True,
            fields=_PERMISSION_FIELDS,
            pageSize=100,
            pageToken=token,
        ),
        items_key="permissions",
        label="permissions.list",
    )


# --- 1. create_shared_drive -------------------------------------------------


@server.tool()
@handle_http_errors("create_shared_drive", service_type="drive")
@require_google_service("drive", "drive_full")
async def create_shared_drive(
    service,
    user_google_email: str,
    name: str,
    theme_id: Optional[str] = None,
    hidden: bool = False,
    dry_run: bool = False,
) -> str:
    """
    Creates a new Google shared drive and makes the caller its organizer.

    Args:
        user_google_email (str): The user's Google email address. Required.
        name (str): Name for the new shared drive. Required.
        theme_id (Optional[str]): Drive theme ID (from drives.list themeId
            values). Omit for the Google default.
        hidden (bool): Whether the drive is hidden from the caller's default
            shared-drive view. Defaults to False.
        dry_run (bool): When True, validate and report what would happen
            without creating anything. Defaults to False.

    Returns:
        str: Confirmation with the new driveId, or the dry-run plan.

    Note:
        Placing the new drive in an OU (e.g. ``99 _SYSTEM/_SharedDrives
        (Restricted)``) is an Admin console step — there is no reliable public
        API for moving shared drives between OUs.
    """
    drive_name = (name or "").strip()
    if not drive_name:
        raise UserInputError("name is required and cannot be blank.")

    if dry_run:
        return (
            "DRY RUN — no shared drive was created.\n"
            f"   Would create shared drive: '{drive_name}'\n"
            f"   theme_id: {theme_id or '(Google default)'}\n"
            f"   hidden: {hidden}\n"
            f"   Organizer: {user_google_email}"
        )

    body: Dict[str, Any] = {"name": drive_name}
    if theme_id:
        body["themeId"] = theme_id
    if hidden:
        body["hidden"] = True

    # requestId makes drives.create idempotent across our own retries: a
    # replayed request with the same ID returns the drive created the first
    # time instead of a duplicate.
    request_id = str(uuid.uuid4())
    created = await execute_with_backoff(
        lambda: service.drives().create(
            requestId=request_id, body=body, fields=_DRIVE_FIELDS
        ),
        label="drives.create",
    )

    drive_id = created.get("id", "")
    logger.info(
        "[create_shared_drive] created '%s' (%s) for %s",
        drive_name,
        drive_id,
        user_google_email,
    )
    return (
        f"✅ Created shared drive '{created.get('name', drive_name)}'\n"
        f"   drive_id: {drive_id}\n"
        f"   theme_id: {created.get('themeId', '(default)')}\n"
        f"   Link: https://drive.google.com/drive/folders/{drive_id}\n"
        "   Next: grant group access with set_drive_permission; OU placement "
        "is an Admin console step."
    )


# --- 2. update_shared_drive -------------------------------------------------


@server.tool()
@handle_http_errors("update_shared_drive", service_type="drive")
@require_google_service("drive", "drive_full")
async def update_shared_drive(
    service,
    user_google_email: str,
    drive_id: str,
    name: Optional[str] = None,
    admin_managed_restrictions: Optional[bool] = None,
    copy_requires_writer_permission: Optional[bool] = None,
    domain_users_only: Optional[bool] = None,
    drive_members_only: Optional[bool] = None,
    sharing_folders_requires_organizer_permission: Optional[bool] = None,
    use_domain_admin_access: bool = False,
    dry_run: bool = False,
) -> str:
    """
    Renames a shared drive and/or sets its restriction flags.

    Args:
        user_google_email (str): The user's Google email address. Required.
        drive_id (str): ID of the shared drive to update. Required.
        name (Optional[str]): New name for the drive.
        admin_managed_restrictions (Optional[bool]): Restrict drive settings
            changes to domain administrators.
        copy_requires_writer_permission (Optional[bool]): Disable copy /
            download / print for commenters and readers.
        domain_users_only (Optional[bool]): Restrict access to users inside
            the drive's domain.
        drive_members_only (Optional[bool]): Restrict item access to drive
            members only (no per-item sharing outward).
        sharing_folders_requires_organizer_permission (Optional[bool]):
            Only organizers may share folders.
        use_domain_admin_access (bool): Issue the update as a domain
            administrator. Requires a Workspace admin account.
        dry_run (bool): When True, report the intended change without
            applying it. Defaults to False.

    Returns:
        str: Before/after summary, re-read from ``drives.get`` so the change
            is confirmed rather than assumed.
    """
    if not drive_id or not drive_id.strip():
        raise UserInputError("drive_id is required.")

    restriction_args = {
        "adminManagedRestrictions": admin_managed_restrictions,
        "copyRequiresWriterPermission": copy_requires_writer_permission,
        "domainUsersOnly": domain_users_only,
        "driveMembersOnly": drive_members_only,
        "sharingFoldersRequiresOrganizerPermission": (
            sharing_folders_requires_organizer_permission
        ),
    }
    restrictions = {k: v for k, v in restriction_args.items() if v is not None}

    if name is None and not restrictions:
        raise UserInputError(
            "Nothing to update: pass name and/or at least one restriction flag."
        )

    before = await _get_shared_drive(service, drive_id)
    if before is None:
        raise UserInputError(
            f"'{drive_id}' is not a shared drive (drives.get returned not-found). "
            "Use update_drive_file to rename a folder or file."
        )

    body: Dict[str, Any] = {}
    if name is not None:
        stripped = name.strip()
        if not stripped:
            raise UserInputError("name cannot be blank.")
        body["name"] = stripped
    if restrictions:
        body["restrictions"] = restrictions

    if dry_run:
        return (
            "DRY RUN — no changes applied.\n"
            f"   Shared drive: '{before.get('name')}' ({drive_id})\n"
            f"   Would set: {body}\n"
            f"   Current restrictions: {before.get('restrictions', {})}"
        )

    await execute_with_backoff(
        lambda: service.drives().update(
            driveId=drive_id,
            body=body,
            useDomainAdminAccess=use_domain_admin_access,
            fields=_DRIVE_FIELDS,
        ),
        label="drives.update",
    )

    # Re-read rather than trusting the update response: the acceptance
    # criterion is that a rename round-trips via drives.get.
    after = await _get_shared_drive(service, drive_id)
    after = after or {}

    lines = [
        f"✅ Updated shared drive {drive_id}",
        f"   Name: '{before.get('name')}' → '{after.get('name')}'",
    ]
    before_restrictions = before.get("restrictions") or {}
    after_restrictions = after.get("restrictions") or {}
    for key in restrictions:
        lines.append(
            f"   {key}: {before_restrictions.get(key)} → {after_restrictions.get(key)}"
        )
    if name is not None and after.get("name") != body.get("name"):
        lines.append("   ⚠️ Rename did not round-trip via drives.get — verify manually.")
    return "\n".join(lines)


# --- 12. list_shared_drives -------------------------------------------------


@server.tool()
@handle_http_errors("list_shared_drives", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def list_shared_drives(
    service,
    user_google_email: str,
    query: Optional[str] = None,
    use_domain_admin_access: bool = False,
    max_results: int = 100,
) -> str:
    """
    Lists shared drives the user can see (or every drive in the domain).

    A direct listing makes reconciliation and the daily scan cheap, and
    catches drives that a search- or registry-driven crawl misses.

    Args:
        user_google_email (str): The user's Google email address. Required.
        query (Optional[str]): Drive query for the ``drives`` collection,
            e.g. ``name contains 'OTB'``.
        use_domain_admin_access (bool): List every shared drive in the domain
            rather than only the caller's. Requires a Workspace admin account.
        max_results (int): Cap on returned drives. Defaults to 100.

    Returns:
        str: One line per shared drive with its ID, name and restrictions.
    """
    if max_results < 1:
        raise UserInputError("max_results must be at least 1.")

    list_fields = f"nextPageToken, drives({_DRIVE_FIELDS})"
    drives = await paginate(
        lambda token: service.drives().list(
            pageSize=min(100, max_results),
            pageToken=token,
            q=query or None,
            useDomainAdminAccess=use_domain_admin_access,
            fields=list_fields,
        ),
        items_key="drives",
        max_items=max_results,
        label="drives.list",
    )

    if not drives:
        scope_note = "domain-wide" if use_domain_admin_access else "visible to you"
        return f"No shared drives found ({scope_note})" + (
            f" for query '{query}'." if query else "."
        )

    lines = [f"Shared drives ({len(drives)}) for {user_google_email}:"]
    for drive in drives:
        restrictions = drive.get("restrictions") or {}
        active = [k for k, v in restrictions.items() if v]
        suffix = f" | restrictions: {', '.join(active)}" if active else ""
        hidden = " | hidden" if drive.get("hidden") else ""
        lines.append(f"- {drive.get('name')} (ID: {drive.get('id')}){hidden}{suffix}")
    if len(drives) == max_results:
        lines.append(
            f"⚠️ Result capped at max_results={max_results}; more drives may exist."
        )
    return "\n".join(lines)


# --- 3a. set_drive_permission -----------------------------------------------


@server.tool()
@handle_http_errors("set_drive_permission", service_type="drive")
@require_google_service("drive", "drive_full")
async def set_drive_permission(
    service,
    user_google_email: str,
    file_or_drive_id: str,
    principal: str,
    role: str,
    allow_individual: bool = False,
    expiration_time: Optional[str] = None,
    send_notification_email: bool = False,
    dry_run: bool = False,
) -> str:
    """
    Grants (or updates) access to a shared drive, folder or file for a group.

    Groups-only by default: Drive access is granted to a Google Group, never
    to an individual, so access follows group membership and survives
    offboarding (Build Sheet decision 2026-01-21). Pass
    ``allow_individual=True`` to grant to a person explicitly.

    Public (``anyone``) and domain-wide sharing are refused outright — no tool
    on this server can create a public link.

    Idempotent: if the principal already holds the requested role the call is a
    no-op; if it holds a different role the existing permission is updated
    rather than duplicated.

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_or_drive_id (str): Shared drive ID, folder ID or file ID. Required.
        principal (str): Group email address (or user email with
            ``allow_individual=True``). Required.
        role (str): One of organizer, fileOrganizer, writer, commenter, reader.
        allow_individual (bool): Explicit opt-in to grant an individual rather
            than a group. Defaults to False.
        expiration_time (Optional[str]): RFC 3339 expiry. Google only accepts
            expiries on reader/commenter/writer file permissions — shared drive
            memberships cannot expire.
        send_notification_email (bool): Defaults to False (silent grant).
        dry_run (bool): Report the intended grant without applying it.

    Returns:
        str: Confirmation including the resulting permission ID.
    """
    if not file_or_drive_id or not file_or_drive_id.strip():
        raise UserInputError("file_or_drive_id is required.")

    validate_drive_role(role)
    principal_type, email = resolve_principal(
        principal, allow_individual=allow_individual
    )
    if expiration_time:
        validate_expiration_time(expiration_time)

    target = await _describe_target(service, file_or_drive_id)
    existing = await _list_permissions(service, file_or_drive_id)
    match = next(
        (
            p
            for p in existing
            if (p.get("emailAddress") or "").lower() == email.lower()
            and not p.get("deleted")
        ),
        None,
    )

    if match and match.get("role") == role and not expiration_time:
        return (
            f"ℹ️ No change: {principal_type} {email} already has role '{role}' on "
            f"{target['kind']} '{target['name']}' ({file_or_drive_id}). "
            f"[permission id: {match.get('id')}]"
        )

    action = "update" if match else "create"
    if dry_run:
        current = f" (currently '{match.get('role')}')" if match else ""
        return (
            "DRY RUN — no permission was changed.\n"
            f"   Target: {target['kind']} '{target['name']}' ({file_or_drive_id})\n"
            f"   Would {action} permission: {principal_type}={email} role={role}"
            f"{current}\n"
            f"   sendNotificationEmail: {send_notification_email}"
        )

    if match:
        body: Dict[str, Any] = {"role": role}
        if expiration_time:
            body["expirationTime"] = expiration_time
        permission = await execute_with_backoff(
            lambda: service.permissions().update(
                fileId=file_or_drive_id,
                permissionId=match["id"],
                body=body,
                supportsAllDrives=True,
                fields="id, type, role, emailAddress, expirationTime",
            ),
            label="permissions.update",
        )
        verb = f"Updated (was '{match.get('role')}')"
    else:
        body = {"type": principal_type, "role": role, "emailAddress": email}
        if expiration_time:
            body["expirationTime"] = expiration_time
        permission = await execute_with_backoff(
            lambda: service.permissions().create(
                fileId=file_or_drive_id,
                body=body,
                supportsAllDrives=True,
                sendNotificationEmail=send_notification_email,
                fields="id, type, role, emailAddress, expirationTime",
            ),
            label="permissions.create",
        )
        verb = "Granted"

    logger.info(
        "[set_drive_permission] %s %s=%s role=%s on %s for %s",
        verb,
        principal_type,
        email,
        role,
        file_or_drive_id,
        user_google_email,
    )
    return (
        f"✅ {verb} access on {target['kind']} '{target['name']}' "
        f"({file_or_drive_id})\n"
        f"   {format_permission_info(permission)}"
    )


# --- 3b. revoke_drive_permission --------------------------------------------


@server.tool()
@handle_http_errors("revoke_drive_permission", service_type="drive")
@require_google_service("drive", "drive_full")
async def revoke_drive_permission(
    service,
    user_google_email: str,
    file_or_drive_id: str,
    principal: Optional[str] = None,
    permission_id: Optional[str] = None,
    dry_run: bool = False,
) -> str:
    """
    Removes a group's (or user's) access to a shared drive, folder or file.

    Two guardrails, both fail-closed:
      * refuses to revoke the calling user's own access (no self-lockout);
      * refuses to remove the last ``organizer`` of a shared drive (no orphaned
        drive that nobody can administer).

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_or_drive_id (str): Shared drive ID, folder ID or file ID. Required.
        principal (Optional[str]): Email of the group/user whose access to
            remove. Either this or permission_id is required.
        permission_id (Optional[str]): Permission ID to remove (from
            get_drive_file_permissions).
        dry_run (bool): Report what would be removed without removing it.

    Returns:
        str: Confirmation of the removal, or the dry-run plan.
    """
    if not file_or_drive_id or not file_or_drive_id.strip():
        raise UserInputError("file_or_drive_id is required.")
    if not principal and not permission_id:
        raise UserInputError("Pass principal or permission_id.")

    target = await _describe_target(service, file_or_drive_id)
    permissions = await _list_permissions(service, file_or_drive_id)

    if permission_id:
        match = next((p for p in permissions if p.get("id") == permission_id), None)
    else:
        wanted = (principal or "").strip().lower()
        match = next(
            (p for p in permissions if (p.get("emailAddress") or "").lower() == wanted),
            None,
        )

    if match is None:
        wanted = permission_id or principal
        return (
            f"ℹ️ No matching permission for '{wanted}' on {target['kind']} "
            f"'{target['name']}' ({file_or_drive_id}); nothing to revoke."
        )

    match_email = (match.get("emailAddress") or "").lower()
    if match_email and match_email == user_google_email.lower():
        raise UserInputError(
            "Refusing to revoke your own access — that would lock you out of "
            f"'{target['name']}'. Ask another organizer to do it."
        )

    if target["kind"] == "drive" and match.get("role") == "organizer":
        organizers = [
            p
            for p in permissions
            if p.get("role") == "organizer" and not p.get("deleted")
        ]
        if len(organizers) <= 1:
            raise UserInputError(
                f"Refusing to remove the last organizer of shared drive "
                f"'{target['name']}' — the drive would be left with nobody who "
                "can administer it. Add another organizer first."
            )

    if dry_run:
        return (
            "DRY RUN — no permission was removed.\n"
            f"   Target: {target['kind']} '{target['name']}' ({file_or_drive_id})\n"
            f"   Would remove: {format_permission_info(match)}"
        )

    await execute_with_backoff(
        lambda: service.permissions().delete(
            fileId=file_or_drive_id,
            permissionId=match["id"],
            supportsAllDrives=True,
        ),
        label="permissions.delete",
    )
    logger.info(
        "[revoke_drive_permission] removed permission %s (%s) on %s for %s",
        match.get("id"),
        match.get("emailAddress"),
        file_or_drive_id,
        user_google_email,
    )
    return (
        f"✅ Revoked access on {target['kind']} '{target['name']}' "
        f"({file_or_drive_id})\n"
        f"   Removed: {format_permission_info(match)}"
    )


# --- 5. create_shortcut -----------------------------------------------------


async def find_existing_shortcut(
    service, parent_folder_id: str, target_id: str
) -> Optional[Dict[str, Any]]:
    """Return an existing shortcut in ``parent_folder_id`` pointing at ``target_id``.

    Drive's query language cannot filter on ``shortcutDetails.targetId``, so the
    shortcuts in the parent are listed and matched client-side.
    """
    shortcuts = await paginate(
        lambda token: service.files().list(
            q=(
                f"'{parent_folder_id}' in parents and "
                f"mimeType='{SHORTCUT_MIME_TYPE}' and trashed=false"
            ),
            fields=(
                "nextPageToken, files(id, name, shortcutDetails(targetId, "
                "targetMimeType), webViewLink)"
            ),
            pageSize=100,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ),
        items_key="files",
        label="files.list(shortcuts)",
    )
    for shortcut in shortcuts:
        details = shortcut.get("shortcutDetails") or {}
        if details.get("targetId") == target_id:
            return shortcut
    return None


@server.tool()
@handle_http_errors("create_shortcut", service_type="drive")
@require_google_service("drive", "drive_full")
async def create_shortcut(
    service,
    user_google_email: str,
    target_id: str,
    parent_folder_id: str,
    name: Optional[str] = None,
    dry_run: bool = False,
) -> str:
    """
    Creates a Drive shortcut to a file or folder inside a parent folder.

    This is the whole navigation layer: hub drives, the KB shortcut folders,
    the central-services sets, and the exec cross-links are all shortcuts.

    Idempotent: if a shortcut in ``parent_folder_id`` already points at
    ``target_id`` it is reported and reused, never duplicated.

    Args:
        user_google_email (str): The user's Google email address. Required.
        target_id (str): ID of the file or folder the shortcut points at.
        parent_folder_id (str): Folder (or shared drive) the shortcut is
            created in.
        name (Optional[str]): Shortcut name. Defaults to the target's name.
        dry_run (bool): Report the intended shortcut without creating it.

    Returns:
        str: Confirmation with the shortcut ID and the target it resolves to.
    """
    if not target_id or not target_id.strip():
        raise UserInputError("target_id is required.")
    if not parent_folder_id or not parent_folder_id.strip():
        raise UserInputError("parent_folder_id is required.")

    target = await execute_with_backoff(
        lambda: service.files().get(
            fileId=target_id,
            fields="id, name, mimeType",
            supportsAllDrives=True,
        ),
        label="files.get(target)",
    )
    if target.get("mimeType") == SHORTCUT_MIME_TYPE:
        raise UserInputError(
            f"'{target.get('name')}' is itself a shortcut; point the new "
            "shortcut at the real target instead of chaining shortcuts."
        )

    resolved_parent = await resolve_folder_id(service, parent_folder_id)
    shortcut_name = (name or target.get("name") or "Shortcut").strip()

    existing = await find_existing_shortcut(service, resolved_parent, target_id)
    if existing is not None:
        return (
            f"ℹ️ Shortcut already exists: '{existing.get('name')}' "
            f"(ID: {existing.get('id')}) in folder {resolved_parent} → "
            f"target {target_id}. Nothing created."
        )

    if dry_run:
        return (
            "DRY RUN — no shortcut was created.\n"
            f"   Would create shortcut '{shortcut_name}' in {resolved_parent}\n"
            f"   → target: '{target.get('name')}' ({target_id}, "
            f"{target.get('mimeType')})"
        )

    created = await execute_with_backoff(
        lambda: service.files().create(
            body={
                "name": shortcut_name,
                "mimeType": SHORTCUT_MIME_TYPE,
                "parents": [resolved_parent],
                "shortcutDetails": {"targetId": target_id},
            },
            fields="id, name, webViewLink, shortcutDetails(targetId, targetMimeType)",
            supportsAllDrives=True,
        ),
        label="files.create(shortcut)",
    )

    details = created.get("shortcutDetails") or {}
    if details.get("targetId") != target_id:
        raise Exception(
            f"Shortcut {created.get('id')} resolved to {details.get('targetId')!r}, "
            f"expected {target_id!r}."
        )

    logger.info(
        "[create_shortcut] '%s' (%s) → %s in %s",
        shortcut_name,
        created.get("id"),
        target_id,
        resolved_parent,
    )
    return (
        f"✅ Created shortcut '{created.get('name')}' (ID: {created.get('id')})\n"
        f"   In folder: {resolved_parent}\n"
        f"   → target: '{target.get('name')}' ({target_id})\n"
        f"   Link: {created.get('webViewLink', 'N/A')}"
    )


__all__ = [
    "create_shared_drive",
    "update_shared_drive",
    "list_shared_drives",
    "set_drive_permission",
    "revoke_drive_permission",
    "create_shortcut",
    "find_existing_shortcut",
]

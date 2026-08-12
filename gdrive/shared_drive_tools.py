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

  The group requirement is enforced by resolving the address against the Admin
  Directory API before granting, NOT by the permission's declared type. Live
  testing on 2026-08-12 showed Drive accepts a ``type=group`` permission for a
  personal address and silently creates a ``type=user`` one instead, so the
  original "Drive will reject it" assumption was false and the guardrail did
  nothing. Grants are refused when the address is not a group, and refused when
  the Directory cannot be reached to check.
* ``revoke_drive_permission`` refuses to revoke the caller's own access —
  whether held directly or via the group being revoked — and refuses to remove
  the last organizer of a shared drive, so it cannot orphan a drive or lock the
  operator out. Group membership is verified through the Admin Directory API
  when that is reachable; when it is not, the result says the check could not
  be made rather than implying it passed.

The removal tool is deliberately **not** named ``remove_drive_permission``:
that name is on the hard denylist in ``core/tool_policy.py`` and would be
refused at the registration chokepoint. Un-blocking it would also re-expose the
unguarded legacy implementation still present in ``gdrive/drive_tools.py``.
``revoke_drive_permission`` is the guarded equivalent.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

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

# Per-target locks for the revoke check-and-delete sequence. Two concurrent
# revocations of different organizers can each list the same two organizers,
# each conclude another one remains, and both delete — leaving the drive with
# none.
#
# This narrows that window to zero *within one server process*, which is the
# whole exposure for a single-instance deployment. It is NOT a distributed
# lock: two Render instances, or a direct API call, can still race. The
# durable protection is Drive's own refusal to remove the final organizer of a
# shared drive, which this guardrail front-runs with a clearer message.
_revoke_locks: Dict[str, asyncio.Lock] = {}
_revoke_locks_guard = asyncio.Lock()


async def _revoke_lock_for(target_id: str) -> asyncio.Lock:
    async with _revoke_locks_guard:
        lock = _revoke_locks.get(target_id)
        if lock is None:
            lock = asyncio.Lock()
            _revoke_locks[target_id] = lock
        return lock


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


async def assert_principal_is_group(
    principal: str, *, user_google_email: str, allow_unverified_group: bool
) -> str:
    """Verify that ``principal`` really is a Google Group before granting to it.

    This is the groups-only guardrail's actual enforcement point.

    It exists because the original design was wrong in a way only live testing
    exposed. That design declared ``type=group`` on the permission and relied on
    Drive to reject a personal address. On 2026-08-12, against a real shared
    drive, Drive did not reject it: ``set_drive_permission`` called with a
    personal address and ``allow_individual=False`` returned success and created
    a ``type=user`` permission. Google treats the declared type as a hint and
    coerces it, so the guardrail was inoperative for exactly the mistake it was
    written to prevent.

    Returns a short note describing how the principal was verified, for the
    tool's output. Raises ``UserInputError`` when the address is not a group, or
    when membership of the group directory cannot be established at all —
    failing closed, because an unverifiable grant is precisely the case that
    used to slip through.
    """
    try:
        directory = await _directory_service(user_google_email=user_google_email)
    except Exception as exc:  # noqa: BLE001 - service unavailable, not a failure
        if allow_unverified_group:
            logger.warning(
                "[set_drive_permission] granting to '%s' as a group WITHOUT "
                "verification (%s: %s)",
                principal,
                type(exc).__name__,
                exc,
            )
            return (
                "\n   ⚠️ Could not verify that this address is a group (the Admin "
                "Directory service is not available to this server) and "
                "allow_unverified_group=True was passed. Google silently "
                "converts a group grant on a personal address into an "
                "individual one, so confirm the resulting permission type."
            )
        raise UserInputError(
            f"Cannot verify that '{principal}' is a Google Group: the Admin "
            "Directory service is not available to this server "
            f"({type(exc).__name__}). Refusing the grant — Drive does NOT "
            "reject a group grant aimed at a personal address, it silently "
            "creates an individual one, so an unverified grant cannot be "
            "allowed. Enable the 'gadmin' service, or pass "
            "allow_individual=True for a deliberate individual grant, or "
            "allow_unverified_group=True to accept the risk explicitly."
        ) from exc

    try:
        await execute_with_backoff(
            lambda: directory.groups().get(groupKey=principal, fields="id, email"),
            label="directory.groups.get(verify)",
        )
    except HttpError as error:
        status = getattr(getattr(error, "resp", None), "status", None)
        if status in (404, 400):
            # A definitive answer from the Directory: this is not a group.
            raise UserInputError(
                f"'{principal}' is not a Google Group in this domain, so it "
                "cannot be granted group access. Drive would silently create "
                "an INDIVIDUAL permission for this address rather than "
                "refusing. If an individual grant is genuinely intended, pass "
                "allow_individual=True — that choice should be explicit and "
                "visible in the audit log."
            ) from error

        # Any other status (403 for a non-admin caller, a persistent 5xx) means
        # the question went unanswered, not that the answer was "no". That is
        # the same situation as the service being unavailable, so it honours
        # the same override — otherwise the escape hatch cannot unblock the
        # deployments it exists for.
        if allow_unverified_group:
            logger.warning(
                "[set_drive_permission] granting to '%s' as a group WITHOUT "
                "verification (groups.get returned %s)",
                principal,
                status,
            )
            return (
                f"\n   ⚠️ Could not verify that this address is a group "
                f"(directory lookup returned HTTP {status}) and "
                "allow_unverified_group=True was passed. Google silently "
                "converts a group grant on a personal address into an "
                "individual one, so confirm the resulting permission type."
            )
        raise UserInputError(
            f"Could not determine whether '{principal}' is a Google Group: the "
            f"directory lookup failed with HTTP {status}. Refusing the grant — "
            "Drive does NOT reject a group grant aimed at a personal address, "
            "it silently creates an individual one. Check the caller's admin "
            "privileges, or pass allow_individual=True for a deliberate "
            "individual grant, or allow_unverified_group=True to accept the "
            "risk explicitly."
        ) from error
    return ""


# groups.get needs the group *read* scope, not the group-member scope — the
# repo's own gadmin.get_group uses admin_directory_group_read for this exact
# endpoint. Both are requested because this helper also backs the membership
# lookup in revoke_drive_permission, which reads members.
@require_google_service(
    "admin_directory",
    ["admin_directory_group_read", "admin_directory_group_member_read"],
)
async def _directory_service(service, user_google_email: str):
    """Lazily acquire an Admin Directory service for the membership check.

    Separate, like ``_hub_registry_service`` in the migration module, so the
    rest of this file needs only Drive scope. Deployments that do not enable an
    admin service simply cannot verify membership, and the caller is told so.
    """
    return service


async def _caller_is_group_member(
    group_email: str, user_google_email: str
) -> Tuple[bool, bool]:
    """Return ``(is_member, verified)`` for the caller's membership of a group.

    ``verified`` is False when the Admin Directory API could not be reached at
    all — no scope, not an admin, service not enabled. The caller must not read
    ``is_member=False`` as "definitely not a member" in that case.
    """
    try:
        directory = await _directory_service(user_google_email=user_google_email)
        member = await execute_with_backoff(
            lambda: directory.members().get(
                groupKey=group_email, memberKey=user_google_email
            ),
            label="directory.members.get(self)",
        )
        return bool(member), True
    except HttpError as error:
        status = getattr(getattr(error, "resp", None), "status", None)
        if status == 404:
            # Directory answered: definitively not a member.
            return False, True
        logger.info(
            "[revoke_drive_permission] membership check for %s in %s failed: %s",
            user_google_email,
            group_email,
            error,
        )
        return False, False
    except Exception as exc:  # noqa: BLE001 - unavailable service, not a failure
        logger.info(
            "[revoke_drive_permission] Admin Directory unavailable for the "
            "membership check (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return False, False


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

    # Hiding is a per-user view preference with its own endpoint. Drive's
    # discovery document flags `restrictions` as unsettable at create time but
    # says nothing either way about `hidden`, so rather than rely on
    # create-time behaviour we cannot verify, use the dedicated call and report
    # honestly if it fails — the drive itself already exists by then.
    hide_note = ""
    if hidden:
        try:
            await execute_with_backoff(
                lambda: service.drives().hide(driveId=drive_id),
                label="drives.hide",
            )
        except Exception as exc:  # noqa: BLE001 - the drive was still created
            logger.warning(
                "[create_shared_drive] drives.hide failed for %s: %s", drive_id, exc
            )
            hide_note = (
                f"\n   ⚠️ The drive was created but hiding it failed "
                f"({type(exc).__name__}: {exc}). Hide it from the Drive UI, or "
                "ignore if visibility does not matter."
            )

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
        f"{hide_note}"
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
    allow_unverified_group: bool = False,
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

    The group requirement is enforced by resolving the address against the
    Admin Directory API before granting. That check is the guardrail: Drive
    itself does NOT refuse a group grant aimed at a personal address — it
    silently creates an individual permission instead (confirmed against a live
    drive on 2026-08-12). If the Directory cannot be reached, the grant is
    refused rather than attempted.

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
        allow_unverified_group (bool): Proceed with a group grant when the
            Admin Directory service is unavailable to verify it. Defaults to
            False (refuse). Only relevant on deployments without an admin
            service enabled.
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
    # Enforce the groups-only rule here, before anything is created. Declaring
    # type=group is not enough: Drive coerces it to an individual permission
    # when the address belongs to a person.
    verification_note = ""
    if principal_type == "group":
        verification_note = await assert_principal_is_group(
            email,
            user_google_email=user_google_email,
            allow_unverified_group=allow_unverified_group,
        )
    if expiration_time:
        validate_expiration_time(expiration_time)

    target = await _describe_target(service, file_or_drive_id)
    existing = await _list_permissions(service, file_or_drive_id)
    # Match on type as well as address. Matching by email alone would find an
    # existing type=user grant while the caller asked for a group, and then
    # quietly no-op or update it — reporting an individual grant as a group
    # one and skipping the create that Drive would have rejected. The
    # groups-only guardrail depends on that rejection actually happening.
    match = next(
        (
            p
            for p in existing
            if (p.get("emailAddress") or "").lower() == email.lower()
            and p.get("type") == principal_type
            and not p.get("deleted")
        ),
        None,
    )
    conflicting = next(
        (
            p
            for p in existing
            if (p.get("emailAddress") or "").lower() == email.lower()
            and p.get("type") != principal_type
            and not p.get("deleted")
        ),
        None,
    )
    if match is None and conflicting is not None:
        raise UserInputError(
            f"'{email}' already holds a type='{conflicting.get('type')}' "
            f"permission (role '{conflicting.get('role')}') on "
            f"{target['kind']} '{target['name']}', but this call declared "
            f"type='{principal_type}'. Refusing to modify a grant of a "
            "different principal type. Pass allow_individual=True to manage "
            "the individual grant explicitly, or revoke it first with "
            "revoke_drive_permission."
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
            idempotent=False,
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
        f"{verification_note}"
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
    allow_self_lockout: bool = False,
    dry_run: bool = False,
) -> str:
    """
    Removes a group's (or user's) access to a shared drive, folder or file.

    Guardrails:
      * refuses to revoke the calling user's own access, whether held directly
        **or through the group being revoked** — group-granted access is the
        default architecture here, so checking only the direct grant would let
        the tool lock the operator out while claiming it cannot;
      * refuses to remove the last ``organizer`` of a shared drive (no orphaned
        drive that nobody can administer).

    Group membership is checked via the Admin Directory API. When that is not
    available — the ``gadmin``/``gadmin_write`` service is not enabled, or the
    caller is not a Workspace admin — membership cannot be verified, and the
    result says so explicitly rather than implying a check happened.

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_or_drive_id (str): Shared drive ID, folder ID or file ID. Required.
        principal (Optional[str]): Email of the group/user whose access to
            remove. Either this or permission_id is required.
        permission_id (Optional[str]): Permission ID to remove (from
            get_drive_file_permissions).
        allow_self_lockout (bool): Explicit opt-in to revoke access the caller
            themselves depends on. Defaults to False.
        dry_run (bool): Report what would be removed without removing it.

    Returns:
        str: Confirmation of the removal, or the dry-run plan.
    """
    if not file_or_drive_id or not file_or_drive_id.strip():
        raise UserInputError("file_or_drive_id is required.")

    wanted = (principal or "").strip().lower()
    wanted_permission_id = (permission_id or "").strip()
    # Blank-but-supplied is checked first: it is a distinct mistake from
    # supplying neither, and the caller should be told which one they made.
    if principal is not None and not wanted:
        raise UserInputError("principal cannot be blank or whitespace.")
    if not wanted and not wanted_permission_id:
        raise UserInputError("Pass principal or permission_id.")
    if wanted and "@" not in wanted:
        # Without this, a malformed principal normalises to something that can
        # match a permission carrying no emailAddress — i.e. an existing
        # `anyone` or `domain` grant — and revoke the wrong one entirely.
        raise UserInputError(
            f"principal must be an email address; got {principal!r}. To remove "
            "a non-email permission (anyone/domain), pass its permission_id."
        )

    target = await _describe_target(service, file_or_drive_id)

    # Hold the per-target lock across list → check → delete. Reading the
    # permissions outside it would let two concurrent revocations each see the
    # same two organizers, each conclude one remains, and both delete.
    async with await _revoke_lock_for(file_or_drive_id):
        permissions = await _list_permissions(service, file_or_drive_id)

        if wanted_permission_id:
            match = next(
                (p for p in permissions if p.get("id") == wanted_permission_id), None
            )
        else:
            match = next(
                (
                    p
                    for p in permissions
                    if (p.get("emailAddress") or "").lower() == wanted
                ),
                None,
            )

        if match is None:
            return (
                f"ℹ️ No matching permission for "
                f"'{wanted_permission_id or wanted}' on {target['kind']} "
                f"'{target['name']}' ({file_or_drive_id}); nothing to revoke."
            )

        match_email = (match.get("emailAddress") or "").lower()
        if (
            match_email
            and match_email == user_google_email.lower()
            and not allow_self_lockout
        ):
            raise UserInputError(
                "Refusing to revoke your own access — that would lock you out of "
                f"'{target['name']}'. Ask another organizer to do it, or pass "
                "allow_self_lockout=True if you mean it."
            )

        # Group-granted access is the default architecture, so a direct-email
        # check alone is a guardrail that misses the common case.
        membership_note = ""
        if match.get("type") == "group" and match_email:
            is_member, verified = await _caller_is_group_member(
                match_email, user_google_email
            )
            if is_member and not allow_self_lockout:
                raise UserInputError(
                    f"Refusing to revoke '{match_email}' — you are a member of "
                    f"that group, so this would remove your own access to "
                    f"'{target['name']}'. Pass allow_self_lockout=True if you "
                    "mean it, or have someone outside the group do it."
                )
            if not verified:
                membership_note = (
                    f"\n⚠️ Could not verify whether you belong to '{match_email}' "
                    "(the Admin Directory service is not available to this "
                    "server). If you are a member, you have just removed your "
                    "own access."
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
                f"   Target: {target['kind']} '{target['name']}' "
                f"({file_or_drive_id})\n"
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
        f"{membership_note}"
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
        idempotent=False,
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

"""
Admin SDK Directory **write** tools — groups and group membership only.

Why this is a separate module and a separate service
----------------------------------------------------
``gadmin/admin_tools.py`` is read-only by a hard contract that
``tests/test_admin_readonly.py`` enforces at source level: no Admin SDK write
method may be invoked there, and ``ADMIN_SCOPES`` contains no write scope.
That contract is not relaxed. Instead, group writes live here, under their own
service key (``gadmin_write``) with their own scope list
(``ADMIN_WRITE_SCOPES``), and the service is opt-in via ``OPT_IN_TOOLS`` in
``main.py``.

The practical consequence: a deployment that enables ``gadmin`` still requests
only readonly Directory scopes. The broader
``https://www.googleapis.com/auth/admin.directory.group`` scope is requested
**only** when ``gadmin_write`` is explicitly named in ``--tools`` / ``TOOLS``.

Deliberately not implemented here: user creation/suspension/deletion, OU
writes, role assignment, and group deletion. Those stay on GAM CLI / the Admin
Console. The Drive architecture build needs groups and membership; it does not
need the rest of the Directory write surface, and every method absent from this
module is a method no connected client can reach.

Scope prerequisite
------------------
``ADMIN_DIRECTORY_GROUP_SCOPE`` must be on the OAuth consent screen (and on the
domain-wide delegation allowlist, if used) before these tools will work; a
missing scope surfaces as 403 ``insufficient_scope`` on every call. The calling
identity must also hold a Workspace admin role with Groups admin privileges —
scope alone is not enough.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from googleapiclient.errors import HttpError

from auth.service_decorator import require_google_service
from core.server import server
from core.utils import UserInputError, handle_http_errors
from gdrive.drive_batch import execute_with_backoff, paginate

logger = logging.getLogger(__name__)

# Directory membership roles. OWNER is accepted but noted: an owner can manage
# the group itself, not just its membership.
VALID_MEMBER_ROLES = ("MEMBER", "MANAGER", "OWNER")

_GROUP_FIELDS = "id, email, name, description, directMembersCount, adminCreated"


def _normalise_email(value: str, *, field: str) -> str:
    email = (value or "").strip().lower()
    if not email or "@" not in email:
        raise UserInputError(f"{field} must be an email address; got {value!r}.")
    return email


async def _get_group(service, group_key: str) -> Optional[Dict[str, Any]]:
    """Fetch a group by email or ID; None when it does not exist."""
    try:
        return await execute_with_backoff(
            lambda: service.groups().get(groupKey=group_key, fields=_GROUP_FIELDS),
            label="directory.groups.get",
        )
    except HttpError as error:
        if getattr(getattr(error, "resp", None), "status", None) == 404:
            return None
        raise


async def _get_member(
    service, group_key: str, member_key: str
) -> Optional[Dict[str, Any]]:
    try:
        return await execute_with_backoff(
            lambda: service.members().get(groupKey=group_key, memberKey=member_key),
            label="directory.members.get",
        )
    except HttpError as error:
        if getattr(getattr(error, "resp", None), "status", None) == 404:
            return None
        raise


# --- 4a. create_group -------------------------------------------------------


@server.tool()
@handle_http_errors("create_group", service_type="admin_directory")
@require_google_service("admin_directory", "admin_directory_group_write")
async def create_group(
    service,
    user_google_email: str,
    email: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    dry_run: bool = False,
) -> str:
    """
    WRITE: Creates a Google Workspace group.

    Groups are how Drive access is granted on this deployment (Build Sheet
    decision 2026-01-21), so this is a prerequisite for the Drive_Permissions
    rows of the target architecture.

    Idempotent: if the group already exists it is reported and left untouched
    rather than erroring or being reconfigured.

    Args:
        user_google_email (str): The user's Google email address. Required.
            Must hold a Workspace admin role with Groups privileges.
        email (str): Group email address, e.g. ``otb-premises@otbgroup.co.uk``.
        name (Optional[str]): Display name. Defaults to the local part of the
            address.
        description (Optional[str]): Group description.
        dry_run (bool): Report what would be created without creating it.

    Returns:
        str: Confirmation with the group ID, or the dry-run plan.

    Note:
        This tool never deletes or renames a group. Group deletion stays on GAM
        CLI / the Admin Console.
    """
    group_email = _normalise_email(email, field="email")

    existing = await _get_group(service, group_email)
    if existing is not None:
        return (
            f"ℹ️ Group '{group_email}' already exists (ID: {existing.get('id')}, "
            f"members: {existing.get('directMembersCount', '?')}). Nothing created."
        )

    display_name = (name or group_email.split("@", 1)[0]).strip()

    if dry_run:
        return (
            "DRY RUN — no group was created.\n"
            f"   Would create group: {group_email}\n"
            f"   name: {display_name}\n"
            f"   description: {description or '(none)'}"
        )

    body: Dict[str, Any] = {"email": group_email, "name": display_name}
    if description:
        body["description"] = description

    created = await execute_with_backoff(
        lambda: service.groups().insert(body=body), label="directory.groups.insert"
    )
    logger.info(
        "[create_group] created %s (%s) by %s",
        group_email,
        created.get("id"),
        user_google_email,
    )
    return (
        f"✅ Created group '{created.get('name', display_name)}'\n"
        f"   email: {created.get('email', group_email)}\n"
        f"   group_id: {created.get('id')}\n"
        "   Group propagation can take a few minutes before Drive accepts it "
        "as a permission principal."
    )


# --- 4b. add_group_member ---------------------------------------------------


@server.tool()
@handle_http_errors("add_group_member", service_type="admin_directory")
@require_google_service("admin_directory", "admin_directory_group_write")
async def add_group_member(
    service,
    user_google_email: str,
    group_email: str,
    member_email: str,
    role: str = "MEMBER",
    dry_run: bool = False,
) -> str:
    """
    WRITE: Adds a member to a Google Workspace group.

    Idempotent: a member already in the group at the requested role is a no-op;
    at a different role, the existing membership is updated rather than
    duplicated.

    Args:
        user_google_email (str): The user's Google email address. Required.
        group_email (str): Group to add to.
        member_email (str): User (or nested group) to add.
        role (str): MEMBER, MANAGER or OWNER. Defaults to MEMBER.
        dry_run (bool): Report the change without applying it.

    Returns:
        str: Confirmation of the membership.
    """
    group_key = _normalise_email(group_email, field="group_email")
    member_key = _normalise_email(member_email, field="member_email")
    member_role = (role or "MEMBER").strip().upper()
    if member_role not in VALID_MEMBER_ROLES:
        raise UserInputError(
            f"Invalid role '{role}'. Must be one of: {', '.join(VALID_MEMBER_ROLES)}."
        )

    group = await _get_group(service, group_key)
    if group is None:
        raise UserInputError(
            f"Group '{group_key}' does not exist. Create it first with create_group."
        )

    existing = await _get_member(service, group_key, member_key)
    if existing is not None and (existing.get("role") or "").upper() == member_role:
        return (
            f"ℹ️ {member_key} is already a {member_role} of {group_key}. "
            "Nothing changed."
        )

    if dry_run:
        current = f" (currently {existing.get('role')})" if existing is not None else ""
        verb = "update" if existing is not None else "add"
        return (
            "DRY RUN — no membership was changed.\n"
            f"   Would {verb}: {member_key} → {group_key} as {member_role}{current}"
        )

    if existing is not None:
        updated = await execute_with_backoff(
            lambda: service.members().update(
                groupKey=group_key, memberKey=member_key, body={"role": member_role}
            ),
            label="directory.members.update",
        )
        logger.info(
            "[add_group_member] role change %s in %s → %s by %s",
            member_key,
            group_key,
            member_role,
            user_google_email,
        )
        return (
            f"✅ Updated {member_key} in {group_key}: "
            f"{existing.get('role')} → {updated.get('role', member_role)}"
        )

    added = await execute_with_backoff(
        lambda: service.members().insert(
            groupKey=group_key, body={"email": member_key, "role": member_role}
        ),
        label="directory.members.insert",
    )
    logger.info(
        "[add_group_member] added %s to %s as %s by %s",
        member_key,
        group_key,
        member_role,
        user_google_email,
    )
    return (
        f"✅ Added {added.get('email', member_key)} to {group_key} as "
        f"{added.get('role', member_role)} (member id: {added.get('id')})"
    )


# --- 4c. remove_group_member ------------------------------------------------


@server.tool()
@handle_http_errors("remove_group_member", service_type="admin_directory")
@require_google_service("admin_directory", "admin_directory_group_write")
async def remove_group_member(
    service,
    user_google_email: str,
    group_email: str,
    member_email: str,
    dry_run: bool = False,
) -> str:
    """
    WRITE: Removes a member from a Google Workspace group.

    Removing a member removes whatever Drive access that group conferred, so
    this refuses to remove the group's last OWNER/MANAGER — a group nobody can
    administer is as bad as an orphaned shared drive.

    Args:
        user_google_email (str): The user's Google email address. Required.
        group_email (str): Group to remove from.
        member_email (str): Member to remove.
        dry_run (bool): Report what would be removed without removing it.

    Returns:
        str: Confirmation of the removal.

    Note:
        This removes a membership, never the group itself.
    """
    group_key = _normalise_email(group_email, field="group_email")
    member_key = _normalise_email(member_email, field="member_email")

    group = await _get_group(service, group_key)
    if group is None:
        raise UserInputError(f"Group '{group_key}' does not exist.")

    existing = await _get_member(service, group_key, member_key)
    if existing is None:
        return f"ℹ️ {member_key} is not a member of {group_key}; nothing to remove."

    member_role = (existing.get("role") or "").upper()
    if member_role in {"OWNER", "MANAGER"}:
        members: List[Dict[str, Any]] = await paginate(
            lambda token: service.members().list(
                groupKey=group_key, maxResults=200, pageToken=token
            ),
            items_key="members",
            label="directory.members.list",
        )
        admins = [
            m for m in members if (m.get("role") or "").upper() in {"OWNER", "MANAGER"}
        ]
        if len(admins) <= 1:
            raise UserInputError(
                f"Refusing to remove the last {member_role} of {group_key} — the "
                "group would be left with nobody who can administer it. Add "
                "another manager or owner first."
            )

    if dry_run:
        return (
            "DRY RUN — no membership was removed.\n"
            f"   Would remove: {member_key} ({member_role or 'MEMBER'}) from "
            f"{group_key}"
        )

    await execute_with_backoff(
        lambda: service.members().delete(groupKey=group_key, memberKey=member_key),
        label="directory.members.delete",
    )
    logger.info(
        "[remove_group_member] removed %s from %s by %s",
        member_key,
        group_key,
        user_google_email,
    )
    return f"✅ Removed {member_key} ({member_role or 'MEMBER'}) from {group_key}."


__all__ = ["create_group", "add_group_member", "remove_group_member"]

"""
Google Workspace Admin SDK MCP Tools — READ-ONLY ONLY.

This module exposes a tightly-scoped set of read-only Admin SDK tools over
the Directory and Reports API surfaces. By design, every tool in this file:

  1. Uses ``is_read_only=True`` in its ``@handle_http_errors`` decorator.
  2. Begins its docstring with the literal token "READ-ONLY:" so callers can
     verify the contract at-a-glance.
  3. Calls only read methods (``users().get``, ``users().list``, ``groups().get``,
     ``groups().list``, ``members().list``, ``orgunits().get``,
     ``orgunits().list``, ``roles().list``, ``roleAssignments().list``,
     ``tokens().list``, ``activities().list``, ``userUsageReport().get``).

Write methods from the Admin SDK (``users.delete``, ``users.update``,
``groups.insert``, ``members.delete``, ``roleAssignments.insert``,
``tokens.delete``, ``mobiledevices.action``, etc.) MUST NEVER be added here.
Admin writes stay on GAM CLI / the Admin Console.

Operational prerequisites before this module's tools can succeed:

  * The OTB GCP project's OAuth consent screen must list all 8 admin
    readonly scopes referenced in ``auth.scopes.ADMIN_SCOPES``. None of
    those scopes authorise any Admin SDK write.
  * The OAuth client ID must be added in Google Workspace Admin Console →
    Security → API Controls → Domain-wide delegation with the exact same
    scope list. Without that, every tool here returns ``403`` because
    Admin SDK calls require domain-wide delegation, not just user consent.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from googleapiclient.errors import HttpError
from mcp import Resource

from auth.service_decorator import require_google_service
from core.server import server
from core.utils import handle_http_errors

logger = logging.getLogger(__name__)


# Customer ID alias used by Admin SDK for the caller's own Workspace customer.
# Avoids hard-coding a numeric customer ID and keeps the module portable
# across tenants without code changes.
_MY_CUSTOMER = "my_customer"


# ---------------------------------------------------------------------------
# Formatting helpers
#
# Admin SDK responses are deeply nested; converting to compact summary text
# keeps tool output readable for the LLM caller without dumping raw JSON.
# Every helper here is pure (no API calls).
# ---------------------------------------------------------------------------


def _format_user(user: Dict[str, Any], detailed: bool = False) -> str:
    """Render one User resource into a compact multi-line summary."""
    primary_email = user.get("primaryEmail") or "(no primary email)"
    name = (user.get("name") or {}).get("fullName") or ""
    lines = [f"- {primary_email}"]
    if name:
        lines.append(f"    name: {name}")
    if user.get("suspended"):
        lines.append("    suspended: True")
    if user.get("isAdmin"):
        lines.append("    is_admin: True")
    if user.get("isDelegatedAdmin"):
        lines.append("    is_delegated_admin: True")
    if user.get("orgUnitPath"):
        lines.append(f"    org_unit: {user.get('orgUnitPath')}")
    if not detailed:
        return "\n".join(lines)

    # Detailed view: surface 2SV state, last login, and aliases — the
    # signals an admin investigation actually cares about.
    if "isEnrolledIn2Sv" in user:
        lines.append(f"    enrolled_2sv: {user.get('isEnrolledIn2Sv')}")
    if "isEnforcedIn2Sv" in user:
        lines.append(f"    enforced_2sv: {user.get('isEnforcedIn2Sv')}")
    if user.get("lastLoginTime"):
        lines.append(f"    last_login: {user.get('lastLoginTime')}")
    if user.get("creationTime"):
        lines.append(f"    created: {user.get('creationTime')}")
    aliases = user.get("aliases") or []
    if aliases:
        lines.append(f"    aliases: {', '.join(aliases)}")
    return "\n".join(lines)


def _format_group(group: Dict[str, Any]) -> str:
    email = group.get("email") or "(no email)"
    name = group.get("name") or ""
    desc = group.get("description") or ""
    direct_members = group.get("directMembersCount")
    lines = [f"- {email}"]
    if name:
        lines.append(f"    name: {name}")
    if direct_members is not None:
        lines.append(f"    direct_members: {direct_members}")
    if desc:
        # Cap description length so audit/log noise stays bounded if a group
        # somehow has a multi-paragraph description.
        if len(desc) > 200:
            desc = desc[:200] + "…"
        lines.append(f"    description: {desc}")
    return "\n".join(lines)


def _format_activity(activity: Dict[str, Any]) -> str:
    """Render one Reports API activity. Each Activity may contain multiple
    events; we surface each event on its own indented line so callers can
    grep individual event names without re-parsing JSON."""
    actor = (activity.get("actor") or {}).get("email") or "(unknown actor)"
    timestamp = (activity.get("id") or {}).get("time") or "(no timestamp)"
    ip = activity.get("ipAddress") or ""
    lines = [f"- {timestamp} actor={actor}" + (f" ip={ip}" if ip else "")]
    for ev in activity.get("events") or []:
        ev_name = ev.get("name") or "(unnamed)"
        params = ev.get("parameters") or []
        # Render at most a handful of parameter key/value pairs to keep the
        # summary scannable. Full event payload is available via the raw
        # Reports API if needed.
        rendered_params = []
        for p in params[:6]:
            k = p.get("name")
            v = p.get("value") or p.get("intValue") or p.get("boolValue")
            if k is not None and v is not None:
                rendered_params.append(f"{k}={v}")
        suffix = (" " + " ".join(rendered_params)) if rendered_params else ""
        lines.append(f"    event: {ev_name}{suffix}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Directory API — Users
# ---------------------------------------------------------------------------


@server.tool()
@require_google_service("admin_directory", "admin_directory_user_read")
@handle_http_errors("list_users", is_read_only=True, service_type="admin_directory")
async def list_users(
    service: Resource,
    user_google_email: str,
    domain: Optional[str] = None,
    ou_path: Optional[str] = None,
    query: Optional[str] = None,
    suspended: Optional[bool] = None,
    max_results: int = 100,
) -> str:
    """READ-ONLY: List Workspace users in the caller's domain.

    Args:
        user_google_email: Caller's Google email (must be a Workspace admin).
        domain: Restrict to a specific domain. If unset, defaults to the
            caller's own customer (``customer="my_customer"``) which covers
            all primary and secondary domains.
        ou_path: Restrict to an org-unit path, e.g. "/Finance".
        query: Admin SDK user search query, e.g.
            ``"isSuspended=true"`` or ``"email:alice*"``. Combined with
            ``suspended`` via AND if both are supplied.
        suspended: True to return only suspended users, False to return only
            active users, None for no filter.
        max_results: Page size cap, max 500 per Admin SDK.

    Returns:
        Compact one-user-per-line summary. Includes a continuation token if
        more results exist.
    """
    params: Dict[str, Any] = {"maxResults": min(max_results, 500)}
    if domain:
        params["domain"] = domain
    else:
        params["customer"] = _MY_CUSTOMER
    if ou_path:
        params["query"] = (
            f"{query} orgUnitPath={ou_path}" if query else f"orgUnitPath={ou_path}"
        )
    elif query:
        params["query"] = query
    if suspended is not None:
        suspended_clause = f"isSuspended={'true' if suspended else 'false'}"
        params["query"] = (
            f"{params['query']} {suspended_clause}"
            if params.get("query")
            else suspended_clause
        )

    logger.info(f"[list_users] domain={domain} ou_path={ou_path} suspended={suspended}")

    result = await asyncio.to_thread(service.users().list(**params).execute)
    users = result.get("users") or []
    next_page = result.get("nextPageToken")

    if not users:
        return "No users matched the supplied filters."
    body = "\n".join(_format_user(u) for u in users)
    suffix = f"\n\nnext_page_token: {next_page}" if next_page else ""
    return f"{len(users)} user(s):\n{body}{suffix}"


@server.tool()
@require_google_service("admin_directory", "admin_directory_user_read")
@handle_http_errors("get_user", is_read_only=True, service_type="admin_directory")
async def get_user(
    service: Resource,
    user_google_email: str,
    user_key: str,
) -> str:
    """READ-ONLY: Get the full Workspace user record for one user.

    Surfaces fields admins typically want during an investigation: primary
    email, name, 2SV enrollment/enforcement, suspension status, last login
    time, aliases, org-unit path, admin flags.

    Args:
        user_google_email: Caller's Google email (must be a Workspace admin).
        user_key: ``primaryEmail`` or unique user ID of the user to fetch.
    """
    logger.info(f"[get_user] user_key={user_key}")
    user = await asyncio.to_thread(
        service.users().get(userKey=user_key, projection="full").execute
    )
    return _format_user(user, detailed=True)


# ---------------------------------------------------------------------------
# Directory API — Groups
# ---------------------------------------------------------------------------


@server.tool()
@require_google_service("admin_directory", "admin_directory_group_read")
@handle_http_errors("list_groups", is_read_only=True, service_type="admin_directory")
async def list_groups(
    service: Resource,
    user_google_email: str,
    domain: Optional[str] = None,
    max_results: int = 100,
) -> str:
    """READ-ONLY: List Workspace groups in the caller's domain.

    Args:
        user_google_email: Caller's Google email (must be a Workspace admin).
        domain: Restrict to a specific domain. If unset, defaults to the
            caller's own customer (covers all domains).
        max_results: Page size cap, max 200 per Admin SDK.
    """
    params: Dict[str, Any] = {"maxResults": min(max_results, 200)}
    if domain:
        params["domain"] = domain
    else:
        params["customer"] = _MY_CUSTOMER

    logger.info(f"[list_groups] domain={domain}")
    result = await asyncio.to_thread(service.groups().list(**params).execute)
    groups = result.get("groups") or []
    next_page = result.get("nextPageToken")

    if not groups:
        return "No groups matched the supplied filters."
    body = "\n".join(_format_group(g) for g in groups)
    suffix = f"\n\nnext_page_token: {next_page}" if next_page else ""
    return f"{len(groups)} group(s):\n{body}{suffix}"


@server.tool()
@require_google_service("admin_directory", "admin_directory_group_read")
@handle_http_errors("get_group", is_read_only=True, service_type="admin_directory")
async def get_group(
    service: Resource,
    user_google_email: str,
    group_key: str,
) -> str:
    """READ-ONLY: Get one Workspace group's full record.

    Args:
        user_google_email: Caller's Google email (must be a Workspace admin).
        group_key: Group email or unique group ID.
    """
    logger.info(f"[get_group] group_key={group_key}")
    group = await asyncio.to_thread(
        service.groups().get(groupKey=group_key).execute
    )
    return _format_group(group)


@server.tool()
@require_google_service("admin_directory", "admin_directory_group_member_read")
@handle_http_errors(
    "list_group_members", is_read_only=True, service_type="admin_directory"
)
async def list_group_members(
    service: Resource,
    user_google_email: str,
    group_key: str,
    include_derived: bool = False,
    max_results: int = 200,
) -> str:
    """READ-ONLY: List members of one Workspace group.

    Args:
        user_google_email: Caller's Google email (must be a Workspace admin).
        group_key: Group email or unique group ID.
        include_derived: If True, include members inherited from nested
            groups. Defaults to False (direct members only).
        max_results: Page size cap, max 200 per Admin SDK.
    """
    params: Dict[str, Any] = {
        "groupKey": group_key,
        "maxResults": min(max_results, 200),
        "includeDerivedMembership": include_derived,
    }
    logger.info(f"[list_group_members] group_key={group_key}")
    result = await asyncio.to_thread(service.members().list(**params).execute)
    members = result.get("members") or []
    next_page = result.get("nextPageToken")

    if not members:
        return f"No members found in group {group_key}."
    lines = []
    for m in members:
        email = m.get("email") or "(no email)"
        role = m.get("role") or "MEMBER"
        m_type = m.get("type") or "USER"
        status = m.get("status") or ""
        suffix = f" status={status}" if status else ""
        lines.append(f"- {email} role={role} type={m_type}{suffix}")
    suffix = f"\n\nnext_page_token: {next_page}" if next_page else ""
    return f"{len(members)} member(s) in {group_key}:\n" + "\n".join(lines) + suffix


@server.tool()
@require_google_service("admin_directory", "admin_directory_group_read")
@handle_http_errors(
    "list_user_groups", is_read_only=True, service_type="admin_directory"
)
async def list_user_groups(
    service: Resource,
    user_google_email: str,
    user_key: str,
    max_results: int = 200,
) -> str:
    """READ-ONLY: List groups that a given user is a member of.

    Args:
        user_google_email: Caller's Google email (must be a Workspace admin).
        user_key: ``primaryEmail`` or unique user ID of the user.
        max_results: Page size cap, max 200 per Admin SDK.
    """
    logger.info(f"[list_user_groups] user_key={user_key}")
    result = await asyncio.to_thread(
        service.groups()
        .list(userKey=user_key, maxResults=min(max_results, 200))
        .execute
    )
    groups = result.get("groups") or []
    if not groups:
        return f"User {user_key} is not a member of any groups."
    return f"{user_key} is in {len(groups)} group(s):\n" + "\n".join(
        _format_group(g) for g in groups
    )


# ---------------------------------------------------------------------------
# Directory API — Org Units
# ---------------------------------------------------------------------------


@server.tool()
@require_google_service("admin_directory", "admin_directory_orgunit_read")
@handle_http_errors("list_orgunits", is_read_only=True, service_type="admin_directory")
async def list_orgunits(
    service: Resource,
    user_google_email: str,
    org_unit_type: str = "all",
) -> str:
    """READ-ONLY: List org units in the caller's Workspace customer.

    Args:
        user_google_email: Caller's Google email (must be a Workspace admin).
        org_unit_type: One of ``"all"`` (full tree) or ``"children"`` (only
            immediate children of the customer root). Defaults to ``"all"``.
    """
    logger.info(f"[list_orgunits] type={org_unit_type}")
    result = await asyncio.to_thread(
        service.orgunits()
        .list(customerId=_MY_CUSTOMER, type=org_unit_type)
        .execute
    )
    ous = result.get("organizationUnits") or []
    if not ous:
        return "No org units found."
    lines = []
    for ou in ous:
        path = ou.get("orgUnitPath") or "(no path)"
        name = ou.get("name") or ""
        desc = ou.get("description") or ""
        suffix = f" — {desc}" if desc else ""
        lines.append(f"- {path}  name={name}{suffix}")
    return f"{len(ous)} org unit(s):\n" + "\n".join(lines)


@server.tool()
@require_google_service("admin_directory", "admin_directory_orgunit_read")
@handle_http_errors("get_orgunit", is_read_only=True, service_type="admin_directory")
async def get_orgunit(
    service: Resource,
    user_google_email: str,
    orgunit_path: str,
) -> str:
    """READ-ONLY: Get one org unit by path.

    Args:
        user_google_email: Caller's Google email (must be a Workspace admin).
        orgunit_path: Path like ``/Finance`` or ``/Finance/Accounts``.
    """
    # Admin SDK orgunits.get accepts the path with the leading slash stripped.
    normalized = orgunit_path.lstrip("/")
    logger.info(f"[get_orgunit] orgunit_path={orgunit_path}")
    ou = await asyncio.to_thread(
        service.orgunits()
        .get(customerId=_MY_CUSTOMER, orgUnitPath=normalized)
        .execute
    )
    path = ou.get("orgUnitPath") or orgunit_path
    name = ou.get("name") or ""
    parent = ou.get("parentOrgUnitPath") or ""
    desc = ou.get("description") or ""
    lines = [f"path: {path}", f"name: {name}"]
    if parent:
        lines.append(f"parent: {parent}")
    if desc:
        lines.append(f"description: {desc}")
    if "blockInheritance" in ou:
        lines.append(f"block_inheritance: {ou.get('blockInheritance')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Directory API — Roles & Role Assignments
# ---------------------------------------------------------------------------


@server.tool()
@require_google_service(
    "admin_directory", "admin_directory_rolemanagement_read"
)
@handle_http_errors(
    "list_admin_roles", is_read_only=True, service_type="admin_directory"
)
async def list_admin_roles(
    service: Resource,
    user_google_email: str,
) -> str:
    """READ-ONLY: List all admin roles defined in the caller's customer.

    Args:
        user_google_email: Caller's Google email (must be a Workspace admin).
    """
    logger.info("[list_admin_roles] customer=my_customer")
    result = await asyncio.to_thread(
        service.roles().list(customer=_MY_CUSTOMER).execute
    )
    roles = result.get("items") or []
    if not roles:
        return "No admin roles defined."
    lines = []
    for r in roles:
        rid = r.get("roleId") or "(no id)"
        name = r.get("roleName") or ""
        is_system = r.get("isSystemRole")
        is_super = r.get("isSuperAdminRole")
        flags = []
        if is_system:
            flags.append("system")
        if is_super:
            flags.append("super_admin")
        flag_suffix = f" [{','.join(flags)}]" if flags else ""
        lines.append(f"- {rid} {name}{flag_suffix}")
    return f"{len(roles)} admin role(s):\n" + "\n".join(lines)


@server.tool()
@require_google_service(
    "admin_directory", "admin_directory_rolemanagement_read"
)
@handle_http_errors(
    "list_role_assignments", is_read_only=True, service_type="admin_directory"
)
async def list_role_assignments(
    service: Resource,
    user_google_email: str,
    role_id: Optional[str] = None,
    user_key: Optional[str] = None,
    max_results: int = 200,
) -> str:
    """READ-ONLY: List admin role assignments, optionally filtered.

    Args:
        user_google_email: Caller's Google email (must be a Workspace admin).
        role_id: If supplied, return only assignments of this role.
        user_key: If supplied, return only assignments to this user.
        max_results: Page size cap, max 200 per Admin SDK.
    """
    params: Dict[str, Any] = {
        "customer": _MY_CUSTOMER,
        "maxResults": min(max_results, 200),
    }
    if role_id:
        params["roleId"] = role_id
    if user_key:
        params["userKey"] = user_key
    logger.info(
        f"[list_role_assignments] role_id={role_id} user_key={user_key}"
    )
    result = await asyncio.to_thread(
        service.roleAssignments().list(**params).execute
    )
    assignments = result.get("items") or []
    next_page = result.get("nextPageToken")
    if not assignments:
        return "No role assignments matched the supplied filters."
    lines = []
    for a in assignments:
        rid = a.get("roleId") or "(no role)"
        assignee = a.get("assignedTo") or "(no assignee)"
        scope_type = a.get("scopeType") or ""
        org_unit = a.get("orgUnitId") or ""
        suffix_parts = []
        if scope_type:
            suffix_parts.append(f"scope={scope_type}")
        if org_unit:
            suffix_parts.append(f"org_unit_id={org_unit}")
        suffix = (" " + " ".join(suffix_parts)) if suffix_parts else ""
        lines.append(f"- role_id={rid} assignee={assignee}{suffix}")
    suffix = f"\n\nnext_page_token: {next_page}" if next_page else ""
    return f"{len(assignments)} role assignment(s):\n" + "\n".join(lines) + suffix


# Note on a deliberately-omitted tool: ``list_oauth_tokens_for_user`` was
# previously exposed here. ``users().tokens().list`` requires the
# ``admin.directory.user.security`` scope, which Google grants in pairs —
# the same scope also authorises ``users().tokens().delete``. To keep
# gadmin's consent set strictly read-only, we drop the tool and the
# security scope rather than rely on source-level inspection alone to
# block the delete capability. Token-grant visibility is still available
# via ``query_token_audit_log`` below (authorize / revoke events) under
# the ``admin.reports.audit.readonly`` scope, which is genuinely read-only.


# ---------------------------------------------------------------------------
# Reports API — Audit Activities
#
# The Reports API exposes per-application activity feeds via the same
# ``activities().list`` method, differentiated by ``applicationName``. We
# expose one tool per supported application so the read-only contract and
# the audit-log tagging are unambiguous, and so each tool's name surfaces
# its purpose to the LLM caller without needing to inspect parameters.
# ---------------------------------------------------------------------------


async def _query_activities(
    service: Resource,
    *,
    application_name: str,
    actor_email: Optional[str],
    event_name: Optional[str],
    start_time: Optional[str],
    end_time: Optional[str],
    max_results: int,
) -> str:
    """Shared implementation for the four audit-log query tools.

    Kept private (not decorated with ``@server.tool``) so callers can only
    reach it through one of the four explicit, scope-narrow wrappers.
    """
    params: Dict[str, Any] = {
        "userKey": actor_email or "all",
        "applicationName": application_name,
        "maxResults": min(max_results, 1000),
    }
    if event_name:
        params["eventName"] = event_name
    if start_time:
        params["startTime"] = start_time
    if end_time:
        params["endTime"] = end_time

    result = await asyncio.to_thread(
        service.activities().list(**params).execute
    )
    activities = result.get("items") or []
    next_page = result.get("nextPageToken")
    if not activities:
        return (
            f"No {application_name} audit events matched the supplied filters."
        )
    body = "\n".join(_format_activity(a) for a in activities)
    suffix = f"\n\nnext_page_token: {next_page}" if next_page else ""
    return f"{len(activities)} {application_name} event(s):\n{body}{suffix}"


@server.tool()
@require_google_service("admin_reports", "admin_reports_audit_read")
@handle_http_errors(
    "query_admin_audit_log", is_read_only=True, service_type="admin_reports"
)
async def query_admin_audit_log(
    service: Resource,
    user_google_email: str,
    actor_email: Optional[str] = None,
    event_name: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    max_results: int = 100,
) -> str:
    """READ-ONLY: Query the Admin Console audit log (Reports API, ``admin``).

    Args:
        user_google_email: Caller's Google email (must be a Workspace admin).
        actor_email: Filter to events triggered by this actor. None = all.
        event_name: Filter to one specific Admin audit event name, e.g.
            ``"CHANGE_USER_PRIVILEGE"`` or ``"DELETE_USER"``.
        start_time: RFC3339 timestamp lower bound (e.g. ``"2026-05-01T00:00:00Z"``).
        end_time: RFC3339 timestamp upper bound.
        max_results: Page size cap, max 1000 per Reports API.
    """
    logger.info(
        f"[query_admin_audit_log] actor={actor_email} event={event_name}"
    )
    return await _query_activities(
        service,
        application_name="admin",
        actor_email=actor_email,
        event_name=event_name,
        start_time=start_time,
        end_time=end_time,
        max_results=max_results,
    )


@server.tool()
@require_google_service("admin_reports", "admin_reports_audit_read")
@handle_http_errors(
    "query_login_audit_log", is_read_only=True, service_type="admin_reports"
)
async def query_login_audit_log(
    service: Resource,
    user_google_email: str,
    actor_email: Optional[str] = None,
    event_name: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    max_results: int = 100,
) -> str:
    """READ-ONLY: Query the login audit log (Reports API, ``login``).

    Args:
        user_google_email: Caller's Google email (must be a Workspace admin).
        actor_email: Filter to login events for this user. None = all.
        event_name: e.g. ``"login_success"``, ``"login_failure"``,
            ``"suspicious_login"``.
        start_time: RFC3339 timestamp lower bound.
        end_time: RFC3339 timestamp upper bound.
        max_results: Page size cap, max 1000 per Reports API.
    """
    logger.info(
        f"[query_login_audit_log] actor={actor_email} event={event_name}"
    )
    return await _query_activities(
        service,
        application_name="login",
        actor_email=actor_email,
        event_name=event_name,
        start_time=start_time,
        end_time=end_time,
        max_results=max_results,
    )


@server.tool()
@require_google_service("admin_reports", "admin_reports_audit_read")
@handle_http_errors(
    "query_token_audit_log", is_read_only=True, service_type="admin_reports"
)
async def query_token_audit_log(
    service: Resource,
    user_google_email: str,
    actor_email: Optional[str] = None,
    event_name: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    max_results: int = 100,
) -> str:
    """READ-ONLY: Query the OAuth token audit log (Reports API, ``token``).

    Useful for spotting third-party OAuth grants and revocations.

    Args:
        user_google_email: Caller's Google email (must be a Workspace admin).
        actor_email: Filter to token events for this user. None = all.
        event_name: e.g. ``"authorize"``, ``"revoke"``.
        start_time: RFC3339 timestamp lower bound.
        end_time: RFC3339 timestamp upper bound.
        max_results: Page size cap, max 1000 per Reports API.
    """
    logger.info(
        f"[query_token_audit_log] actor={actor_email} event={event_name}"
    )
    return await _query_activities(
        service,
        application_name="token",
        actor_email=actor_email,
        event_name=event_name,
        start_time=start_time,
        end_time=end_time,
        max_results=max_results,
    )


@server.tool()
@require_google_service("admin_reports", "admin_reports_audit_read")
@handle_http_errors(
    "query_drive_audit_log", is_read_only=True, service_type="admin_reports"
)
async def query_drive_audit_log(
    service: Resource,
    user_google_email: str,
    actor_email: Optional[str] = None,
    event_name: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    max_results: int = 100,
) -> str:
    """READ-ONLY: Query the Drive audit log (Reports API, ``drive``).

    Surfaces file access, sharing, and download events across the Workspace
    customer.

    Args:
        user_google_email: Caller's Google email (must be a Workspace admin).
        actor_email: Filter to drive events for this user. None = all.
        event_name: e.g. ``"view"``, ``"download"``, ``"change_acl_editors"``.
        start_time: RFC3339 timestamp lower bound.
        end_time: RFC3339 timestamp upper bound.
        max_results: Page size cap, max 1000 per Reports API.
    """
    logger.info(
        f"[query_drive_audit_log] actor={actor_email} event={event_name}"
    )
    return await _query_activities(
        service,
        application_name="drive",
        actor_email=actor_email,
        event_name=event_name,
        start_time=start_time,
        end_time=end_time,
        max_results=max_results,
    )


# ---------------------------------------------------------------------------
# Reports API — Usage Reports
# ---------------------------------------------------------------------------


@server.tool()
@require_google_service("admin_reports", "admin_reports_usage_read")
@handle_http_errors(
    "query_usage_report", is_read_only=True, service_type="admin_reports"
)
async def query_usage_report(
    service: Resource,
    user_google_email: str,
    user_key: str = "all",
    date: Optional[str] = None,
    parameters: Optional[List[str]] = None,
) -> str:
    """READ-ONLY: Query the per-user usage report (Reports API).

    Args:
        user_google_email: Caller's Google email (must be a Workspace admin).
        user_key: ``"all"`` (default) for every user in the customer, or a
            specific ``primaryEmail`` / unique user ID.
        date: Day to report on, in ``YYYY-MM-DD`` form. Defaults to two days
            ago because Google's usage reports lag by ~48 hours; passing
            today typically returns 400.
        parameters: List of fully-qualified parameter names to fetch (e.g.
            ``["gmail:num_emails_received", "drive:num_items_created"]``).
            None returns Google's default parameter set.
    """
    from datetime import date as _date, timedelta as _td

    target_date = date or (_date.today() - _td(days=2)).isoformat()
    params: Dict[str, Any] = {"userKey": user_key, "date": target_date}
    if parameters:
        params["parameters"] = ",".join(parameters)

    logger.info(
        f"[query_usage_report] user_key={user_key} date={target_date}"
    )
    result = await asyncio.to_thread(
        service.userUsageReport().get(**params).execute
    )
    usage_reports = result.get("usageReports") or []
    next_page = result.get("nextPageToken")
    if not usage_reports:
        return f"No usage reports for {user_key} on {target_date}."
    lines = []
    for r in usage_reports:
        entity_email = (r.get("entity") or {}).get("userEmail") or "(unknown)"
        params_list = r.get("parameters") or []
        # Render the parameters compactly; full payload available via the
        # raw Reports API when needed.
        rendered = []
        for p in params_list[:20]:
            name = p.get("name")
            for v_key in ("intValue", "boolValue", "stringValue", "datetimeValue"):
                if v_key in p:
                    rendered.append(f"{name}={p[v_key]}")
                    break
        lines.append(
            f"- {entity_email}\n    " + "\n    ".join(rendered) if rendered
            else f"- {entity_email} (no parameter values)"
        )
    suffix = f"\n\nnext_page_token: {next_page}" if next_page else ""
    return f"Usage report ({target_date}):\n" + "\n".join(lines) + suffix

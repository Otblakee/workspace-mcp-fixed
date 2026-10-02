"""Tool profiles: which slice of the tool surface one deployment exposes.

Every tool definition the connector advertises is read by the model on
every turn. The full OTB surface is about 120 tools and 150 KB of JSON,
and Anthropic's guidance is that tool selection degrades past 30 to 50
tools. Most of that weight is admin and migration tooling that an everyday
chat never needs, so the same image runs as two Render services:

- ``everyday``: everything except ``ADMIN_TOOLS``. The main connector.
- ``admin``: only ``ADMIN_TOOLS``. A second connector for Directory reads,
  group writes, shared-drive build, migration runs, banners and signatures.
- ``all``: no profile filtering (the default, and what every deployment
  ran before profiles existed).

A profile is applied in ``core.tool_registry.filter_server_tools`` after
tier filtering and before read-only filtering. It never adds a tool: a tool
that the tier, ``--tools`` or ``BLOCKED_TOOLS`` already keeps out stays
out. ``tests/test_tool_profiles.py`` pins the membership against the
source modules so a new admin tool cannot drift into the everyday set.
"""

from __future__ import annotations

import logging
from typing import FrozenSet, Optional

logger = logging.getLogger(__name__)

PROFILES = ("all", "everyday", "admin")

# Tools that live on the admin connector only. Grouped by source module.
ADMIN_TOOLS: FrozenSet[str] = frozenset(
    {
        # --- gadmin/admin_tools.py (Directory and Reports reads) ----------
        "list_users",
        "get_user",
        "list_groups",
        "get_group",
        "list_group_members",
        "list_user_groups",
        "list_orgunits",
        "get_orgunit",
        "list_admin_roles",
        "list_role_assignments",
        "list_oauth_tokens_for_user",
        "query_admin_audit_log",
        "query_login_audit_log",
        "query_token_audit_log",
        "query_drive_audit_log",
        "query_usage_report",
        # --- gadmin/admin_group_tools.py (the one write carve-out) --------
        "create_group",
        "add_group_member",
        "remove_group_member",
        # --- gsignatures/signature_tools.py --------------------------------
        "preview_email_signature",
        "get_email_signatures",
        "get_email_signature_html",
        "set_email_signature",
        "apply_email_signatures",
        "audit_email_signatures",
        "restore_email_signature",
        # --- gdrive/shared_drive_tools.py (architecture build) -------------
        #   list_shared_drives and create_shortcut stay everyday: the Drive
        #   routing skill uses them to file into hub drives.
        "create_shared_drive",
        "update_shared_drive",
        "set_drive_permission",
        "revoke_drive_permission",
        # --- gdrive/drive_migration_tools.py (migration engine) ------------
        #   get_drive_file_metadata stays everyday: it is a plain read.
        "walk_drive",
        "create_folder_tree",
        "batch_copy_from_manifest",
        "reconcile_folders",
        "rebuild_hub",
        # --- gdrive/shared_drive_theme_tools.py (banners) -----------------
        "list_drive_themes",
        "get_shared_drive_theme",
        "set_shared_drive_theme",
        "set_shared_drive_themes_from_registry",
    }
)

_active_profile: str = "all"


def set_tool_profile(profile: Optional[str]) -> str:
    """Select the active profile. ``None`` or blank means ``all``."""
    global _active_profile
    name = (profile or "all").strip().lower()
    if name not in PROFILES:
        raise ValueError(
            f"Unknown tool profile {profile!r}; choose one of {', '.join(PROFILES)}"
        )
    _active_profile = name
    logger.info("Tool profile: %s", name)
    return name


def get_tool_profile() -> str:
    return _active_profile


def profile_excludes(tool_name: str, profile: Optional[str] = None) -> bool:
    """True when ``tool_name`` must be removed under ``profile`` (default: active)."""
    name = profile or _active_profile
    if name == "everyday":
        return tool_name in ADMIN_TOOLS
    if name == "admin":
        return tool_name not in ADMIN_TOOLS
    return False

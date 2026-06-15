"""
Tool policy: hard denylist of destructive Google Workspace tools.

Single source of truth for the OTB deployment trim. Tools named here are
never registered with the FastMCP server, regardless of ``--tools``,
``--tool-tier``, ``--read-only``, ``tool_tiers.yaml`` contents, or any env
var. Enforcement is fail-closed in ``core.server._audited_tool``: a blocked
tool's ``@server.tool()`` decorator becomes a no-op, so the tool never
registers, never appears in ``list_tools``, and can never be called by any
connected client (Claude chat, Cowork, Code, or the OTB AI Cockpit).

IMPORTANT — what this does NOT do:
Gating here constrains what the MCP, and therefore every Claude surface
wired to it, can do. It does NOT reduce the OAuth token's scope. A leaked
full-scope token can still call the Google API directly and bypass this
denylist entirely. Token security (encryption at rest, short-lived access
tokens, fast revoke) and Google-side controls (Vault retention, Drive
sharing restrictions) remain the controls that survive a token leak.

Soft-delete: the literal delete/trash capability for Drive is replaced by
``soft_delete_drive_file`` (move into a private holding folder, reversible
via ``restore_drive_file``). See ``gdrive/drive_tools.py``.
"""

# Tools that must NEVER be exposed on this deployment. To re-enable one,
# remove it here AND restore it to core/tool_tiers.yaml. The test
# tests/test_tool_policy.py asserts these two stay in sync (no blocked tool
# may appear in any tier).
BLOCKED_TOOLS = frozenset(
    {
        # --- Drive: ownership / access loss -------------------------------
        "transfer_drive_ownership",  # hands a file to another account, may be unrecoverable
        "remove_drive_permission",  # revokes access to a file
        # --- Drive: over-share / exfiltration -----------------------------
        #   Remove from this set and gate to internal-only sharing if the AI
        #   genuinely needs to share. Removed by default: a public link is a
        #   permanent leak, worse than a recoverable trash.
        "share_drive_file",
        "batch_share_drive_file",
        "set_drive_file_permissions",
        "update_drive_permission",
        # --- Calendar: hard delete ----------------------------------------
        "delete_event",
        # --- Contacts: hard delete ----------------------------------------
        "delete_contact",
        "batch_delete_contacts",
        "delete_contact_group",
        # --- Gmail: hard delete (low stakes, but no reason to expose) ------
        "delete_gmail_draft",
        "delete_gmail_filter",
        # --- Gmail: bulk trash via labels ---------------------------------
        #   Applying the TRASH label trashes messages; the batch variant does
        #   it at scale. The single-message variant is left enabled (it is the
        #   normal archive/label path); harden it separately if needed.
        "batch_modify_gmail_message_labels",
        # --- Apps Script: Drive hard-delete bypass ------------------------
        #   delete_script_project deletes the Drive-backed script file via
        #   Drive files().delete (gappsscript/apps_script_tools.py), which
        #   would bypass the Drive soft-delete replacement if appscript is ever
        #   opted in. Blocked here so the gate holds regardless of --tools.
        "delete_script_project",
    }
)


def is_blocked_tool(tool_name: str) -> bool:
    """Return True if ``tool_name`` must never be registered or called."""
    return tool_name in BLOCKED_TOOLS

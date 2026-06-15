"""Tests for the destructive-tool trim (core/tool_policy.py).

Covers the OTB deployment trim that gates destructive Google Workspace tools
at the MCP source so no connected client (Claude chat, Cowork, Code, the OTB
AI Cockpit) can call them:

1. BLOCKED_TOOLS contains the destructive tools we decided to remove.
2. No blocked tool appears in core/tool_tiers.yaml (denylist and tier config
   stay in sync; a blocked tool can never sneak back via a tier).
3. The registration chokepoint actually skips blocked tools: they are defined
   in source but never registered with the FastMCP server.
4. The soft-delete replacement tools are registered, and update_drive_file no
   longer exposes a `trashed` parameter.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
TIERS_PATH = REPO_ROOT / "core" / "tool_tiers.yaml"


def _unwrap(fn):
    """Peel functools.wraps layers down to the original implementation."""
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


# ---------------------------------------------------------------------------
# 1. Denylist content
# ---------------------------------------------------------------------------

EXPECTED_BLOCKED = {
    "transfer_drive_ownership",
    "remove_drive_permission",
    "share_drive_file",
    "batch_share_drive_file",
    "set_drive_file_permissions",
    "update_drive_permission",
    "delete_event",
    "delete_contact",
    "batch_delete_contacts",
    "delete_contact_group",
    "delete_gmail_draft",
    "delete_gmail_filter",
    "batch_modify_gmail_message_labels",
    "delete_script_project",
}


class TestDenylistContent:
    def test_expected_destructive_tools_are_blocked(self):
        from core.tool_policy import BLOCKED_TOOLS

        missing = EXPECTED_BLOCKED - set(BLOCKED_TOOLS)
        assert not missing, f"expected these to be blocked: {sorted(missing)}"

    def test_is_blocked_tool_helper(self):
        from core.tool_policy import is_blocked_tool

        assert is_blocked_tool("transfer_drive_ownership") is True
        assert is_blocked_tool("create_drive_file") is False


# ---------------------------------------------------------------------------
# 2. Denylist and tier config stay in sync
# ---------------------------------------------------------------------------


def _all_tier_tool_names() -> set[str]:
    config = yaml.safe_load(TIERS_PATH.read_text(encoding="utf-8"))
    names: set[str] = set()
    for service_config in config.values():
        for tier_tools in service_config.values():
            if tier_tools:
                names.update(tier_tools)
    return names


class TestTierSync:
    def test_no_blocked_tool_in_any_tier(self):
        from core.tool_policy import BLOCKED_TOOLS

        leaked = set(BLOCKED_TOOLS) & _all_tier_tool_names()
        assert not leaked, (
            f"blocked tools must not appear in tool_tiers.yaml; found: {sorted(leaked)}"
        )

    def test_soft_delete_tools_are_in_a_tier(self):
        tier_tools = _all_tier_tool_names()
        assert "soft_delete_drive_file" in tier_tools
        assert "restore_drive_file" in tier_tools


# ---------------------------------------------------------------------------
# 3 + 4. Registration chokepoint behaviour (integration with the real server)
# ---------------------------------------------------------------------------


class TestRegistrationSkip:
    @pytest.fixture(scope="class")
    def registered_tools(self):
        # Importing the module runs every @server.tool() decorator through the
        # patched chokepoint in core.server, which skips blocked tools.
        import gdrive.drive_tools  # noqa: F401  (import for side effects)
        from core.server import server
        from core.tool_registry import get_tool_components

        return get_tool_components(server)

    @pytest.mark.parametrize(
        "blocked_drive_tool",
        [
            "transfer_drive_ownership",
            "remove_drive_permission",
            "share_drive_file",
            "batch_share_drive_file",
            "set_drive_file_permissions",
            "update_drive_permission",
        ],
    )
    def test_blocked_drive_tool_not_registered(
        self, registered_tools, blocked_drive_tool
    ):
        assert blocked_drive_tool not in registered_tools

    def test_blocked_tool_still_defined_in_source(self):
        """The denylist gates registration; it does not delete the source.

        This proves the skip (not accidental absence) is what keeps the tool
        off the wire.
        """
        import gdrive.drive_tools as drive_tools

        assert hasattr(drive_tools, "transfer_drive_ownership")
        assert hasattr(drive_tools, "share_drive_file")

    def test_soft_delete_tools_registered(self, registered_tools):
        assert "soft_delete_drive_file" in registered_tools
        assert "restore_drive_file" in registered_tools

    def test_update_drive_file_has_no_trashed_param(self, registered_tools):
        tool = registered_tools["update_drive_file"]
        schema = getattr(tool, "parameters", {}) or {}
        properties = schema.get("properties", {})
        assert "trashed" not in properties, (
            "update_drive_file must not expose a trashed parameter; trashing is "
            "replaced by soft_delete_drive_file"
        )


# ---------------------------------------------------------------------------
# 5. restore_drive_file only reverses a soft-delete
# ---------------------------------------------------------------------------


class TestRestoreRefusesActiveFiles:
    @pytest.mark.asyncio
    async def test_restore_refuses_non_soft_deleted_file(self, monkeypatch):
        """With target_folder_id, restore must not move an active file out of
        its real folders; it only reverses a soft-delete."""
        import gdrive.drive_tools as mod

        monkeypatch.setenv("DRIVE_HOLDING_FOLDER_ID", "holding123")
        monkeypatch.setattr(
            mod, "resolve_folder_id", AsyncMock(return_value="holding123")
        )
        # Active file: no marker and not in the holding folder.
        monkeypatch.setattr(
            mod,
            "resolve_drive_item",
            AsyncMock(
                return_value=(
                    "file1",
                    {
                        "name": "Live.doc",
                        "parents": ["realfolder"],
                        "appProperties": {},
                    },
                )
            ),
        )
        service = MagicMock()
        impl = _unwrap(mod.restore_drive_file)
        with pytest.raises(Exception, match="not soft-deleted"):
            await impl(
                service=service,
                user_google_email="oliver@otbgroup.co.uk",
                file_id="file1",
                target_folder_id="somewhere",
            )
        service.files.return_value.update.assert_not_called()

"""Unit tests for the shared drive / permission / shortcut tools.

These are the P1 blockers for the target Drive architecture build. The tests
exercise the acceptance criteria from the gap analysis:

* create/update a shared drive, with the rename verified by re-reading
  ``drives.get`` rather than trusting the update response;
* permissions refuse non-group principals unless ``allow_individual=True``,
  and refuse public/domain sharing outright;
* revoke refuses self-lockout and refuses to orphan a shared drive;
* creating a duplicate shortcut is a no-op, not a second shortcut.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from googleapiclient.errors import HttpError  # noqa: E402

from core.utils import UserInputError  # noqa: E402
from gdrive import shared_drive_tools  # noqa: E402


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


create_shared_drive = _unwrap(shared_drive_tools.create_shared_drive)
update_shared_drive = _unwrap(shared_drive_tools.update_shared_drive)
list_shared_drives = _unwrap(shared_drive_tools.list_shared_drives)
set_drive_permission = _unwrap(shared_drive_tools.set_drive_permission)
revoke_drive_permission = _unwrap(shared_drive_tools.revoke_drive_permission)
create_shortcut = _unwrap(shared_drive_tools.create_shortcut)

USER = "oliver@otbgroup.co.uk"


def _not_found() -> HttpError:
    resp = MagicMock()
    resp.status = 404
    return HttpError(resp, b'{"error": {"errors": [{"reason": "notFound"}]}}')


def _request(result):
    """A googleapiclient-shaped request whose execute() returns ``result``."""
    request = MagicMock()
    if isinstance(result, Exception):
        request.execute.side_effect = result
    else:
        request.execute.return_value = result
    return request


class FakeDrive:
    """Minimal Drive service double recording the calls the tools make."""

    def __init__(self):
        self.calls = []
        self.drives_get_result = None
        self.drives_get_results = None
        self.drives_create_result = {"id": "drv1", "name": "New Drive"}
        self.drives_list_result = {"drives": []}
        self.files_get_result = {}
        self.files_list_result = {"files": []}
        self.files_create_result = {}
        self.permissions_list_result = {"permissions": []}
        self.permissions_create_result = {}
        self.permissions_update_result = {}

    # -- drives ---------------------------------------------------------
    def drives(self):
        parent = self

        class _Drives:
            def get(self, **kwargs):
                parent.calls.append(("drives.get", kwargs))
                if parent.drives_get_results is not None:
                    result = parent.drives_get_results.pop(0)
                else:
                    result = parent.drives_get_result
                if result is None:
                    return _request(_not_found())
                return _request(result)

            def create(self, **kwargs):
                parent.calls.append(("drives.create", kwargs))
                return _request(parent.drives_create_result)

            def update(self, **kwargs):
                parent.calls.append(("drives.update", kwargs))
                return _request({"id": kwargs.get("driveId")})

            def list(self, **kwargs):
                parent.calls.append(("drives.list", kwargs))
                return _request(parent.drives_list_result)

        return _Drives()

    # -- files ----------------------------------------------------------
    def files(self):
        parent = self

        class _Files:
            def get(self, **kwargs):
                parent.calls.append(("files.get", kwargs))
                return _request(parent.files_get_result)

            def list(self, **kwargs):
                parent.calls.append(("files.list", kwargs))
                return _request(parent.files_list_result)

            def create(self, **kwargs):
                parent.calls.append(("files.create", kwargs))
                return _request(parent.files_create_result)

        return _Files()

    # -- permissions ----------------------------------------------------
    def permissions(self):
        parent = self

        class _Permissions:
            def list(self, **kwargs):
                parent.calls.append(("permissions.list", kwargs))
                return _request(parent.permissions_list_result)

            def create(self, **kwargs):
                parent.calls.append(("permissions.create", kwargs))
                return _request(parent.permissions_create_result)

            def update(self, **kwargs):
                parent.calls.append(("permissions.update", kwargs))
                return _request(parent.permissions_update_result)

            def delete(self, **kwargs):
                parent.calls.append(("permissions.delete", kwargs))
                return _request({})

        return _Permissions()

    def call_names(self):
        return [name for name, _ in self.calls]

    def kwargs_for(self, name):
        return [kw for n, kw in self.calls if n == name]


# ---------------------------------------------------------------------------
# 1. create_shared_drive
# ---------------------------------------------------------------------------


class TestCreateSharedDrive:
    @pytest.mark.asyncio
    async def test_creates_and_returns_drive_id(self):
        service = FakeDrive()
        service.drives_create_result = {
            "id": "0AB123",
            "name": "OTB-Premises & Property",
            "themeId": "bok",
        }

        result = await create_shared_drive(
            service, USER, name="OTB-Premises & Property", theme_id="bok"
        )

        assert "0AB123" in result
        create_kwargs = service.kwargs_for("drives.create")[0]
        assert create_kwargs["body"]["name"] == "OTB-Premises & Property"
        assert create_kwargs["body"]["themeId"] == "bok"
        # requestId makes our own retries idempotent.
        assert create_kwargs["requestId"]

    @pytest.mark.asyncio
    async def test_dry_run_creates_nothing(self):
        service = FakeDrive()
        result = await create_shared_drive(service, USER, name="OTB-Hub", dry_run=True)
        assert "DRY RUN" in result
        assert "drives.create" not in service.call_names()

    @pytest.mark.asyncio
    async def test_blank_name_rejected(self):
        with pytest.raises(UserInputError):
            await create_shared_drive(FakeDrive(), USER, name="   ")


# ---------------------------------------------------------------------------
# 2. update_shared_drive
# ---------------------------------------------------------------------------


class TestUpdateSharedDrive:
    @pytest.mark.asyncio
    async def test_rename_round_trips_via_drives_get(self):
        """RC-04: VALE-Heath & Safety → VALE-Health & Safety."""
        service = FakeDrive()
        service.drives_get_results = [
            {"id": "d1", "name": "VALE-Heath & Safety", "restrictions": {}},
            {"id": "d1", "name": "VALE-Health & Safety", "restrictions": {}},
        ]

        result = await update_shared_drive(
            service, USER, drive_id="d1", name="VALE-Health & Safety"
        )

        assert "VALE-Heath & Safety' → 'VALE-Health & Safety" in result
        assert "did not round-trip" not in result
        assert service.kwargs_for("drives.update")[0]["body"]["name"] == (
            "VALE-Health & Safety"
        )

    @pytest.mark.asyncio
    async def test_flags_a_rename_that_did_not_stick(self):
        service = FakeDrive()
        service.drives_get_results = [
            {"id": "d1", "name": "Old", "restrictions": {}},
            {"id": "d1", "name": "Old", "restrictions": {}},  # unchanged
        ]

        result = await update_shared_drive(service, USER, drive_id="d1", name="New")

        assert "did not round-trip" in result

    @pytest.mark.asyncio
    async def test_sets_restriction_flags(self):
        service = FakeDrive()
        service.drives_get_results = [
            {"id": "d1", "name": "Restricted", "restrictions": {}},
            {
                "id": "d1",
                "name": "Restricted",
                "restrictions": {"driveMembersOnly": True},
            },
        ]

        result = await update_shared_drive(
            service, USER, drive_id="d1", drive_members_only=True
        )

        body = service.kwargs_for("drives.update")[0]["body"]
        assert body["restrictions"] == {"driveMembersOnly": True}
        assert "driveMembersOnly: None → True" in result

    @pytest.mark.asyncio
    async def test_rejects_a_no_op_call(self):
        with pytest.raises(UserInputError, match="Nothing to update"):
            await update_shared_drive(FakeDrive(), USER, drive_id="d1")

    @pytest.mark.asyncio
    async def test_rejects_a_non_shared_drive_target(self):
        service = FakeDrive()
        service.drives_get_result = None  # 404 → it's a file, not a drive
        with pytest.raises(UserInputError, match="not a shared drive"):
            await update_shared_drive(service, USER, drive_id="file1", name="x")

    @pytest.mark.asyncio
    async def test_dry_run_applies_nothing(self):
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "A", "restrictions": {}}
        result = await update_shared_drive(
            service, USER, drive_id="d1", name="B", dry_run=True
        )
        assert "DRY RUN" in result
        assert "drives.update" not in service.call_names()


# ---------------------------------------------------------------------------
# 12. list_shared_drives
# ---------------------------------------------------------------------------


class TestListSharedDrives:
    @pytest.mark.asyncio
    async def test_lists_drives_with_restrictions(self):
        service = FakeDrive()
        service.drives_list_result = {
            "drives": [
                {"id": "d1", "name": "OTB-Hub", "restrictions": {}},
                {
                    "id": "d2",
                    "name": "VALE-HR",
                    "restrictions": {"driveMembersOnly": True},
                },
            ]
        }

        result = await list_shared_drives(service, USER)

        assert "OTB-Hub" in result and "d1" in result
        assert "driveMembersOnly" in result

    @pytest.mark.asyncio
    async def test_empty_result_is_reported_clearly(self):
        result = await list_shared_drives(FakeDrive(), USER)
        assert "No shared drives found" in result


# ---------------------------------------------------------------------------
# 3a. set_drive_permission
# ---------------------------------------------------------------------------


class TestSetDrivePermission:
    @pytest.mark.asyncio
    async def test_grants_group_access_to_a_shared_drive(self):
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "OTB-Premises & Property"}
        service.permissions_create_result = {
            "id": "p1",
            "type": "group",
            "role": "writer",
            "emailAddress": "otb-premises@otbgroup.co.uk",
        }

        result = await set_drive_permission(
            service,
            USER,
            file_or_drive_id="d1",
            principal="otb-premises@otbgroup.co.uk",
            role="writer",
        )

        body = service.kwargs_for("permissions.create")[0]["body"]
        assert body == {
            "type": "group",
            "role": "writer",
            "emailAddress": "otb-premises@otbgroup.co.uk",
        }
        assert (
            service.kwargs_for("permissions.create")[0]["sendNotificationEmail"]
            is False
        )
        assert service.kwargs_for("permissions.create")[0]["supportsAllDrives"] is True
        assert "Granted" in result

    @pytest.mark.asyncio
    async def test_individual_principal_is_typed_as_group_without_opt_in(self):
        """The guardrail is the declared type: Drive rejects type=group for a
        personal address, so an individual grant cannot happen by accident."""
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_create_result = {"id": "p1", "type": "group"}

        await set_drive_permission(
            service, USER, "d1", principal="someone@otbgroup.co.uk", role="reader"
        )

        assert service.kwargs_for("permissions.create")[0]["body"]["type"] == "group"

    @pytest.mark.asyncio
    async def test_allow_individual_switches_type_to_user(self):
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_create_result = {"id": "p1", "type": "user"}

        await set_drive_permission(
            service,
            USER,
            "d1",
            principal="someone@otbgroup.co.uk",
            role="reader",
            allow_individual=True,
        )

        assert service.kwargs_for("permissions.create")[0]["body"]["type"] == "user"

    @pytest.mark.asyncio
    async def test_refuses_public_sharing(self):
        with pytest.raises(UserInputError):
            await set_drive_permission(
                FakeDrive(), USER, "d1", principal="anyone", role="reader"
            )

    @pytest.mark.asyncio
    async def test_rejects_owner_role(self):
        with pytest.raises(UserInputError):
            await set_drive_permission(
                FakeDrive(), USER, "d1", principal="g@otbgroup.co.uk", role="owner"
            )

    @pytest.mark.asyncio
    async def test_existing_matching_role_is_a_no_op(self):
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_list_result = {
            "permissions": [
                {
                    "id": "p1",
                    "type": "group",
                    "role": "writer",
                    "emailAddress": "otb-media@otbgroup.co.uk",
                }
            ]
        }

        result = await set_drive_permission(
            service, USER, "d1", principal="otb-media@otbgroup.co.uk", role="writer"
        )

        assert "No change" in result
        assert "permissions.create" not in service.call_names()
        assert "permissions.update" not in service.call_names()

    @pytest.mark.asyncio
    async def test_existing_different_role_is_updated_not_duplicated(self):
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_list_result = {
            "permissions": [
                {
                    "id": "p1",
                    "type": "group",
                    "role": "reader",
                    "emailAddress": "otb-media@otbgroup.co.uk",
                }
            ]
        }
        service.permissions_update_result = {
            "id": "p1",
            "type": "group",
            "role": "writer",
            "emailAddress": "otb-media@otbgroup.co.uk",
        }

        result = await set_drive_permission(
            service, USER, "d1", principal="otb-media@otbgroup.co.uk", role="writer"
        )

        assert "permissions.create" not in service.call_names()
        assert service.kwargs_for("permissions.update")[0]["permissionId"] == "p1"
        assert "Updated" in result

    @pytest.mark.asyncio
    async def test_dry_run_changes_nothing(self):
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}

        result = await set_drive_permission(
            service,
            USER,
            "d1",
            principal="otb-kb-editors@otbgroup.co.uk",
            role="fileOrganizer",
            dry_run=True,
        )

        assert "DRY RUN" in result
        assert "permissions.create" not in service.call_names()

    @pytest.mark.asyncio
    async def test_invalid_expiry_is_rejected(self):
        service = FakeDrive()
        with pytest.raises(ValueError):
            await set_drive_permission(
                service,
                USER,
                "d1",
                principal="g@otbgroup.co.uk",
                role="reader",
                expiration_time="next tuesday",
            )


# ---------------------------------------------------------------------------
# 3b. revoke_drive_permission
# ---------------------------------------------------------------------------


class TestRevokeDrivePermission:
    @pytest.mark.asyncio
    async def test_revokes_a_group_permission(self):
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_list_result = {
            "permissions": [
                {
                    "id": "p1",
                    "type": "group",
                    "role": "writer",
                    "emailAddress": "old-group@otbgroup.co.uk",
                },
                {
                    "id": "p2",
                    "type": "user",
                    "role": "organizer",
                    "emailAddress": USER,
                },
            ]
        }

        result = await revoke_drive_permission(
            service, USER, "d1", principal="old-group@otbgroup.co.uk"
        )

        assert service.kwargs_for("permissions.delete")[0]["permissionId"] == "p1"
        assert "Revoked" in result

    @pytest.mark.asyncio
    async def test_refuses_self_lockout(self):
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_list_result = {
            "permissions": [
                {"id": "p1", "type": "user", "role": "organizer", "emailAddress": USER},
                {
                    "id": "p2",
                    "type": "user",
                    "role": "organizer",
                    "emailAddress": "other@otbgroup.co.uk",
                },
            ]
        }

        with pytest.raises(UserInputError, match="your own access"):
            await revoke_drive_permission(service, USER, "d1", principal=USER)

    @pytest.mark.asyncio
    async def test_refuses_to_orphan_a_shared_drive(self):
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "OTB-Hub"}
        service.permissions_list_result = {
            "permissions": [
                {
                    "id": "p1",
                    "type": "group",
                    "role": "organizer",
                    "emailAddress": "admins@otbgroup.co.uk",
                }
            ]
        }

        with pytest.raises(UserInputError, match="last organizer"):
            await revoke_drive_permission(
                service, USER, "d1", principal="admins@otbgroup.co.uk"
            )

    @pytest.mark.asyncio
    async def test_missing_permission_is_a_no_op(self):
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}

        result = await revoke_drive_permission(
            service, USER, "d1", principal="ghost@otbgroup.co.uk"
        )

        assert "nothing to revoke" in result
        assert "permissions.delete" not in service.call_names()

    @pytest.mark.asyncio
    async def test_dry_run_removes_nothing(self):
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_list_result = {
            "permissions": [
                {
                    "id": "p1",
                    "type": "group",
                    "role": "writer",
                    "emailAddress": "g@otbgroup.co.uk",
                }
            ]
        }

        result = await revoke_drive_permission(
            service, USER, "d1", principal="g@otbgroup.co.uk", dry_run=True
        )

        assert "DRY RUN" in result
        assert "permissions.delete" not in service.call_names()

    @pytest.mark.asyncio
    async def test_requires_a_selector(self):
        with pytest.raises(UserInputError):
            await revoke_drive_permission(FakeDrive(), USER, "d1")


# ---------------------------------------------------------------------------
# 5. create_shortcut
# ---------------------------------------------------------------------------


class TestCreateShortcut:
    @pytest.mark.asyncio
    async def test_creates_shortcut_resolving_to_target(self):
        service = FakeDrive()
        service.files_get_result = {
            "id": "t1",
            "name": "05_Drainage",
            "mimeType": "application/vnd.google-apps.folder",
        }
        service.files_create_result = {
            "id": "s1",
            "name": "05_Drainage",
            "webViewLink": "https://drive.google.com/file/d/s1",
            "shortcutDetails": {"targetId": "t1"},
        }

        with patch(
            "gdrive.shared_drive_tools.resolve_folder_id",
            new_callable=AsyncMock,
            return_value="hub1",
        ):
            result = await create_shortcut(service, USER, "t1", "hub1")

        body = service.kwargs_for("files.create")[0]["body"]
        assert body["mimeType"] == "application/vnd.google-apps.shortcut"
        assert body["shortcutDetails"] == {"targetId": "t1"}
        assert body["parents"] == ["hub1"]
        assert "s1" in result

    @pytest.mark.asyncio
    async def test_duplicate_shortcut_is_a_no_op(self):
        """Acceptance: a second shortcut to the same target in the same parent
        must not be created."""
        service = FakeDrive()
        service.files_get_result = {
            "id": "t1",
            "name": "05_Drainage",
            "mimeType": "application/vnd.google-apps.folder",
        }
        service.files_list_result = {
            "files": [
                {
                    "id": "existing",
                    "name": "05_Drainage",
                    "shortcutDetails": {"targetId": "t1"},
                }
            ]
        }

        with patch(
            "gdrive.shared_drive_tools.resolve_folder_id",
            new_callable=AsyncMock,
            return_value="hub1",
        ):
            result = await create_shortcut(service, USER, "t1", "hub1")

        assert "already exists" in result
        assert "files.create" not in service.call_names()

    @pytest.mark.asyncio
    async def test_refuses_to_chain_shortcuts(self):
        service = FakeDrive()
        service.files_get_result = {
            "id": "s0",
            "name": "A shortcut",
            "mimeType": "application/vnd.google-apps.shortcut",
        }

        with pytest.raises(UserInputError, match="itself a shortcut"):
            await create_shortcut(service, USER, "s0", "hub1")

    @pytest.mark.asyncio
    async def test_dry_run_creates_nothing(self):
        service = FakeDrive()
        service.files_get_result = {
            "id": "t1",
            "name": "Target",
            "mimeType": "application/vnd.google-apps.folder",
        }

        with patch(
            "gdrive.shared_drive_tools.resolve_folder_id",
            new_callable=AsyncMock,
            return_value="hub1",
        ):
            result = await create_shortcut(service, USER, "t1", "hub1", dry_run=True)

        assert "DRY RUN" in result
        assert "files.create" not in service.call_names()

    @pytest.mark.asyncio
    async def test_defaults_name_to_target_name(self):
        service = FakeDrive()
        service.files_get_result = {
            "id": "t1",
            "name": "98_Central Services",
            "mimeType": "application/vnd.google-apps.folder",
        }
        service.files_create_result = {
            "id": "s1",
            "name": "98_Central Services",
            "shortcutDetails": {"targetId": "t1"},
        }

        with patch(
            "gdrive.shared_drive_tools.resolve_folder_id",
            new_callable=AsyncMock,
            return_value="hub1",
        ):
            await create_shortcut(service, USER, "t1", "hub1")

        assert (
            service.kwargs_for("files.create")[0]["body"]["name"]
            == "98_Central Services"
        )


NEW_DRIVE_TOOLS = {
    "create_shared_drive",
    "update_shared_drive",
    "list_shared_drives",
    "set_drive_permission",
    "revoke_drive_permission",
    "create_shortcut",
    "walk_drive",
    "get_drive_file_metadata",
    "create_folder_tree",
    "batch_copy_from_manifest",
    "reconcile_folders",
    "rebuild_hub",
}


class TestRegistrationPolicy:
    def test_every_new_drive_tool_registers(self):
        """The denylist chokepoint silently no-ops a decorator, so a name
        collision with a blocked tool would leave the tool unregistered and
        invisible. Assert they all made it onto the server."""
        import gdrive.drive_migration_tools  # noqa: F401 - registers tools
        import gdrive.shared_drive_tools  # noqa: F401 - registers tools
        from core.server import server
        from core.tool_registry import get_tool_components

        registered = set(get_tool_components(server))
        assert NEW_DRIVE_TOOLS <= registered

    def test_every_new_drive_tool_is_reachable_at_the_extended_tier(self):
        """Render runs TOOL_TIER=extended; a tool declared only at 'complete'
        would never load there."""
        from core.tool_tier_loader import ToolTierLoader

        extended = set(ToolTierLoader().get_tools_up_to_tier("extended", ["drive"]))
        assert NEW_DRIVE_TOOLS <= extended

    def test_new_tools_are_not_on_the_denylist(self):
        from core.tool_policy import BLOCKED_TOOLS

        for name in (
            "create_shared_drive",
            "update_shared_drive",
            "list_shared_drives",
            "set_drive_permission",
            "revoke_drive_permission",
            "create_shortcut",
        ):
            assert name not in BLOCKED_TOOLS

    def test_legacy_unguarded_permission_tools_stay_blocked(self):
        """The guarded tools are additive; they do not un-block the legacy
        unguarded sharing surface."""
        from core.tool_policy import BLOCKED_TOOLS

        for name in (
            "share_drive_file",
            "batch_share_drive_file",
            "set_drive_file_permissions",
            "update_drive_permission",
            "remove_drive_permission",
            "transfer_drive_ownership",
        ):
            assert name in BLOCKED_TOOLS

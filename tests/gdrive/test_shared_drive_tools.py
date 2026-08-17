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
        self.drives_hide_error = None
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

            def hide(self, **kwargs):
                parent.calls.append(("drives.hide", kwargs))
                if parent.drives_hide_error is not None:
                    return _request(parent.drives_hide_error)
                return _request({"id": kwargs.get("driveId")})

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
    async def test_hidden_uses_the_dedicated_endpoint(self):
        """`hidden` is a per-user view preference with its own method. Drive's
        discovery document does not confirm it is honoured on create, so use
        the endpoint that unambiguously is."""
        service = FakeDrive()
        service.drives_create_result = {"id": "0AB", "name": "Scratch"}

        await create_shared_drive(service, USER, name="Scratch", hidden=True)

        assert "hidden" not in service.kwargs_for("drives.create")[0]["body"]
        assert service.kwargs_for("drives.hide")[0]["driveId"] == "0AB"

    @pytest.mark.asyncio
    async def test_hide_is_not_called_when_not_requested(self):
        service = FakeDrive()
        await create_shared_drive(service, USER, name="Scratch")
        assert "drives.hide" not in service.call_names()

    @pytest.mark.asyncio
    async def test_hide_failure_is_reported_without_losing_the_drive(self):
        """The drive exists by then; swallowing or raising would both mislead."""
        service = FakeDrive()
        service.drives_create_result = {"id": "0AB", "name": "Scratch"}
        service.drives_hide_error = RuntimeError("hide unavailable")

        result = await create_shared_drive(service, USER, name="Scratch", hidden=True)

        assert "0AB" in result
        assert "hiding it failed" in result

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


@pytest.fixture(autouse=True)
def _group_verification_passes(request):
    """Most set_drive_permission tests assume the principal is a real group.
    Tests that exercise the verification itself opt out."""
    if "uses_directory" in request.keywords:
        yield
        return
    with patch.object(
        shared_drive_tools,
        "assert_principal_is_group",
        new=AsyncMock(return_value=""),
    ):
        yield


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
    async def test_existing_user_grant_does_not_satisfy_a_group_request(self):
        """Matching on email alone found a type=user grant when the caller
        asked for a group, then quietly no-op'd or updated it — reporting an
        individual grant as a group one and skipping the create that Drive
        would have rejected."""
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_list_result = {
            "permissions": [
                {
                    "id": "p1",
                    "type": "user",
                    "role": "writer",
                    "emailAddress": "someone@otbgroup.co.uk",
                }
            ]
        }

        with pytest.raises(UserInputError, match="different principal type"):
            await set_drive_permission(
                service, USER, "d1", principal="someone@otbgroup.co.uk", role="writer"
            )

        assert "permissions.create" not in service.call_names()
        assert "permissions.update" not in service.call_names()

    @pytest.mark.asyncio
    async def test_existing_user_grant_is_managed_with_allow_individual(self):
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_list_result = {
            "permissions": [
                {
                    "id": "p1",
                    "type": "user",
                    "role": "reader",
                    "emailAddress": "someone@otbgroup.co.uk",
                }
            ]
        }
        service.permissions_update_result = {
            "id": "p1",
            "type": "user",
            "role": "writer",
            "emailAddress": "someone@otbgroup.co.uk",
        }

        result = await set_drive_permission(
            service,
            USER,
            "d1",
            principal="someone@otbgroup.co.uk",
            role="writer",
            allow_individual=True,
        )

        assert service.kwargs_for("permissions.update")[0]["permissionId"] == "p1"
        assert "Updated" in result

    @pytest.mark.asyncio
    async def test_group_grant_matches_only_the_group_permission(self):
        """A user and a group permission can coexist for the same address."""
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_list_result = {
            "permissions": [
                {
                    "id": "puser",
                    "type": "user",
                    "role": "reader",
                    "emailAddress": "shared@otbgroup.co.uk",
                },
                {
                    "id": "pgroup",
                    "type": "group",
                    "role": "writer",
                    "emailAddress": "shared@otbgroup.co.uk",
                },
            ]
        }

        result = await set_drive_permission(
            service, USER, "d1", principal="shared@otbgroup.co.uk", role="writer"
        )

        assert "No change" in result
        assert "pgroup" in result

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


@pytest.fixture(autouse=True)
def _no_directory_lookup(request):
    """Existing revoke tests must not reach for a real Admin Directory
    service. Tests that exercise the membership guard opt out."""
    if "uses_directory" in request.keywords:
        yield
        return
    with patch.object(
        shared_drive_tools,
        "_caller_is_group_member",
        new=AsyncMock(return_value=(False, True)),
    ):
        yield


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

    @pytest.mark.asyncio
    async def test_whitespace_principal_is_rejected(self):
        """A blank principal normalises to '' and would match a permission
        with no emailAddress — i.e. an existing anyone/domain grant — and
        revoke that instead."""
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_list_result = {
            "permissions": [{"id": "pub", "type": "anyone", "role": "reader"}]
        }

        with pytest.raises(UserInputError, match="blank"):
            await revoke_drive_permission(service, USER, "d1", principal="   ")

        assert "permissions.delete" not in service.call_names()

    @pytest.mark.asyncio
    async def test_non_email_principal_cannot_match_a_public_permission(self):
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_list_result = {
            "permissions": [{"id": "pub", "type": "anyone", "role": "reader"}]
        }

        with pytest.raises(UserInputError, match="must be an email address"):
            await revoke_drive_permission(service, USER, "d1", principal="anyone")

        assert "permissions.delete" not in service.call_names()

    @pytest.mark.uses_directory
    @pytest.mark.asyncio
    async def test_refuses_to_revoke_a_group_the_caller_belongs_to(self):
        """Group-granted access is the default architecture, so checking only
        the direct email would let the tool lock the operator out while
        promising it cannot."""
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_list_result = {
            "permissions": [
                {
                    "id": "p1",
                    "type": "group",
                    "role": "writer",
                    "emailAddress": "otb-premises@otbgroup.co.uk",
                },
                {
                    "id": "p2",
                    "type": "user",
                    "role": "organizer",
                    "emailAddress": "other@otbgroup.co.uk",
                },
            ]
        }

        with patch.object(
            shared_drive_tools,
            "_caller_is_group_member",
            new=AsyncMock(return_value=(True, True)),
        ):
            with pytest.raises(UserInputError, match="member of that group"):
                await revoke_drive_permission(
                    service, USER, "d1", principal="otb-premises@otbgroup.co.uk"
                )

        assert "permissions.delete" not in service.call_names()

    @pytest.mark.uses_directory
    @pytest.mark.asyncio
    async def test_allow_self_lockout_overrides_the_group_guard(self):
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_list_result = {
            "permissions": [
                {
                    "id": "p1",
                    "type": "group",
                    "role": "writer",
                    "emailAddress": "otb-premises@otbgroup.co.uk",
                }
            ]
        }

        with patch.object(
            shared_drive_tools,
            "_caller_is_group_member",
            new=AsyncMock(return_value=(True, True)),
        ):
            result = await revoke_drive_permission(
                service,
                USER,
                "d1",
                principal="otb-premises@otbgroup.co.uk",
                allow_self_lockout=True,
            )

        assert "Revoked" in result
        assert service.kwargs_for("permissions.delete")[0]["permissionId"] == "p1"

    @pytest.mark.uses_directory
    @pytest.mark.asyncio
    async def test_unverifiable_membership_is_disclosed_not_implied(self):
        """When Directory is unreachable we must not imply a check happened."""
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_list_result = {
            "permissions": [
                {
                    "id": "p1",
                    "type": "group",
                    "role": "writer",
                    "emailAddress": "otb-premises@otbgroup.co.uk",
                }
            ]
        }

        with patch.object(
            shared_drive_tools,
            "_caller_is_group_member",
            new=AsyncMock(return_value=(False, False)),
        ):
            result = await revoke_drive_permission(
                service, USER, "d1", principal="otb-premises@otbgroup.co.uk"
            )

        assert "Could not verify" in result
        assert "Revoked" in result

    @pytest.mark.asyncio
    async def test_concurrent_organizer_revocations_cannot_both_succeed(self):
        """Two revocations of different organizers could each list the same
        two organizers, each conclude one remains, and both delete."""
        import asyncio as _asyncio

        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_list_result = {
            "permissions": [
                {
                    "id": "o1",
                    "type": "group",
                    "role": "organizer",
                    "emailAddress": "a@otbgroup.co.uk",
                },
                {
                    "id": "o2",
                    "type": "group",
                    "role": "organizer",
                    "emailAddress": "b@otbgroup.co.uk",
                },
            ]
        }

        # After either delete lands, only one organizer remains.
        real_permissions = service.permissions

        def shrinking_permissions():
            handle = real_permissions()
            original_delete = handle.delete

            def delete(**kwargs):
                removed = kwargs["permissionId"]
                service.permissions_list_result = {
                    "permissions": [
                        p
                        for p in service.permissions_list_result["permissions"]
                        if p["id"] != removed
                    ]
                }
                return original_delete(**kwargs)

            handle.delete = delete
            return handle

        service.permissions = shrinking_permissions

        results = await _asyncio.gather(
            revoke_drive_permission(service, USER, "d1", principal="a@otbgroup.co.uk"),
            revoke_drive_permission(service, USER, "d1", principal="b@otbgroup.co.uk"),
            return_exceptions=True,
        )

        deletes = service.kwargs_for("permissions.delete")
        assert len(deletes) == 1, "both revocations deleted; the drive is orphaned"
        assert any(isinstance(r, UserInputError) for r in results)

    @pytest.mark.asyncio
    async def test_a_public_permission_can_still_be_removed_by_id(self):
        """Refusing the malformed principal must not remove the ability to
        clean up an existing public grant deliberately."""
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_list_result = {
            "permissions": [{"id": "pub", "type": "anyone", "role": "reader"}]
        }

        result = await revoke_drive_permission(service, USER, "d1", permission_id="pub")

        assert service.kwargs_for("permissions.delete")[0]["permissionId"] == "pub"
        assert "Revoked" in result


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


class TestGroupsOnlyGuardrailIsEnforced:
    """Regression for the live failure found on 2026-08-12.

    The original guardrail declared type=group and trusted Drive to reject a
    personal address. Against a real shared drive Drive did not reject it — it
    returned success and created a type=user permission. Enforcement therefore
    has to happen before the grant, by resolving the address in the Directory.
    """

    def _service(self):
        service = FakeDrive()
        service.drives_get_result = {"id": "d1", "name": "Drive"}
        service.permissions_create_result = {"id": "p1", "type": "group"}
        return service

    @pytest.mark.uses_directory
    @pytest.mark.asyncio
    async def test_personal_address_is_refused_before_any_grant(self):
        service = self._service()

        async def not_a_group(principal, **kwargs):
            raise UserInputError(f"'{principal}' is not a Google Group in this domain")

        with patch.object(
            shared_drive_tools, "assert_principal_is_group", side_effect=not_a_group
        ):
            with pytest.raises(UserInputError, match="not a Google Group"):
                await set_drive_permission(
                    service, USER, "d1", principal="katie@otbgroup.co.uk", role="reader"
                )

        # The crucial assertion: nothing was created. The old code reached
        # permissions.create and Drive happily made an individual grant.
        assert "permissions.create" not in service.call_names()

    @pytest.mark.uses_directory
    @pytest.mark.asyncio
    async def test_verification_is_skipped_for_explicit_individual_grants(self):
        """allow_individual=True is the documented, audited escape hatch, so it
        must not be blocked by the group check."""
        service = self._service()
        service.permissions_create_result = {"id": "p1", "type": "user"}
        checked = []

        async def spy(principal, **kwargs):
            checked.append(principal)
            return ""

        with patch.object(
            shared_drive_tools, "assert_principal_is_group", side_effect=spy
        ):
            await set_drive_permission(
                service,
                USER,
                "d1",
                principal="katie@otbgroup.co.uk",
                role="reader",
                allow_individual=True,
            )

        assert checked == [], "individual grants must not be group-verified"
        assert service.kwargs_for("permissions.create")[0]["body"]["type"] == "user"

    @pytest.mark.uses_directory
    @pytest.mark.asyncio
    async def test_group_grant_is_verified_before_creation(self):
        service = self._service()
        order = []

        async def spy(principal, **kwargs):
            order.append("verified")
            return ""

        real_permissions = service.permissions

        def tracking_permissions():
            handle = real_permissions()
            original = handle.create

            def create(**kwargs):
                order.append("created")
                return original(**kwargs)

            handle.create = create
            return handle

        service.permissions = tracking_permissions

        with patch.object(
            shared_drive_tools, "assert_principal_is_group", side_effect=spy
        ):
            await set_drive_permission(
                service, USER, "d1", principal="bir-hs@otbgroup.co.uk", role="writer"
            )

        assert order == ["verified", "created"]


@pytest.mark.uses_directory
class TestAssertPrincipalIsGroup:
    @pytest.mark.asyncio
    async def test_unreachable_directory_fails_closed(self):
        """An unverifiable grant is exactly the case that used to slip
        through, so the default must be refusal, not optimism."""

        async def unavailable(**kwargs):
            raise RuntimeError("admin service not enabled")

        with patch.object(
            shared_drive_tools, "_directory_service", side_effect=unavailable
        ):
            with pytest.raises(UserInputError, match="Cannot verify"):
                await shared_drive_tools.assert_principal_is_group(
                    "team@otbgroup.co.uk",
                    user_google_email=USER,
                    allow_unverified_group=False,
                )

    @pytest.mark.asyncio
    async def test_unreachable_directory_can_be_overridden_loudly(self):
        async def unavailable(**kwargs):
            raise RuntimeError("admin service not enabled")

        with patch.object(
            shared_drive_tools, "_directory_service", side_effect=unavailable
        ):
            note = await shared_drive_tools.assert_principal_is_group(
                "team@otbgroup.co.uk",
                user_google_email=USER,
                allow_unverified_group=True,
            )

        assert "Could not verify" in note

    @pytest.mark.asyncio
    async def test_resolvable_group_passes_quietly(self):
        directory = MagicMock()
        directory.groups.return_value.get.return_value = _request(
            {"id": "g1", "email": "bir-hs@otbgroup.co.uk"}
        )

        with patch.object(
            shared_drive_tools,
            "_directory_service",
            new=AsyncMock(return_value=directory),
        ):
            note = await shared_drive_tools.assert_principal_is_group(
                "bir-hs@otbgroup.co.uk",
                user_google_email=USER,
                allow_unverified_group=False,
            )

        assert note == ""

    @pytest.mark.asyncio
    async def test_lookup_failure_is_not_read_as_a_no(self):
        """A 403 (non-admin caller) or 5xx means the question went unanswered,
        not that the answer was 'not a group'. Default is still refusal, but
        the override must apply here too — otherwise the escape hatch cannot
        unblock the deployments it exists for."""
        resp = MagicMock()
        resp.status = 403
        forbidden = HttpError(resp, b'{"error": {"message": "not an admin"}}')
        directory = MagicMock()
        directory.groups.return_value.get.return_value = _request(forbidden)
        # A non-admin caller cannot read users either, so the disambiguation
        # lookup is just as unanswered as the group lookup.
        directory.users.return_value.get.return_value = _request(forbidden)

        with patch.object(
            shared_drive_tools,
            "_directory_service",
            new=AsyncMock(return_value=directory),
        ):
            with pytest.raises(UserInputError, match="Could not determine"):
                await shared_drive_tools.assert_principal_is_group(
                    "team@otbgroup.co.uk",
                    user_google_email=USER,
                    allow_unverified_group=False,
                )

            note = await shared_drive_tools.assert_principal_is_group(
                "team@otbgroup.co.uk",
                user_google_email=USER,
                allow_unverified_group=True,
            )
        assert "Could not verify" in note
        assert "403" in note

    @pytest.mark.asyncio
    async def test_403_for_a_user_address_is_a_definitive_refusal(self):
        """Found live: the Directory answers groups.get with 403, not 404, when
        the key belongs to a person — with credentials that resolve real groups
        fine. Reporting that as 'lookup failed, try allow_unverified_group'
        pointed the operator at the override in exactly the case where the
        override creates the individual grant we are preventing."""
        resp = MagicMock()
        resp.status = 403
        forbidden = HttpError(resp, b'{"error": {"message": "Not Authorized"}}')
        directory = MagicMock()
        directory.groups.return_value.get.return_value = _request(forbidden)
        # The same address DOES resolve as a user.
        directory.users.return_value.get.return_value = _request(
            {"id": "u1", "primaryEmail": "katie@otbgroup.co.uk"}
        )

        with patch.object(
            shared_drive_tools,
            "_directory_service",
            new=AsyncMock(return_value=directory),
        ):
            with pytest.raises(UserInputError, match="is a user account"):
                await shared_drive_tools.assert_principal_is_group(
                    "katie@otbgroup.co.uk",
                    user_google_email=USER,
                    allow_unverified_group=False,
                )

            # And the override must NOT rescue it: this is a definite answer.
            with pytest.raises(UserInputError, match="is a user account"):
                await shared_drive_tools.assert_principal_is_group(
                    "katie@otbgroup.co.uk",
                    user_google_email=USER,
                    allow_unverified_group=True,
                )

    @pytest.mark.asyncio
    async def test_403_that_is_not_a_user_stays_overridable(self):
        """A genuine authorisation failure is still 'unanswered', so the escape
        hatch must keep working for the deployments it exists for."""
        resp = MagicMock()
        resp.status = 403
        forbidden = HttpError(resp, b'{"error": {"message": "Not Authorized"}}')
        directory = MagicMock()
        directory.groups.return_value.get.return_value = _request(forbidden)
        directory.users.return_value.get.return_value = _request(forbidden)

        with patch.object(
            shared_drive_tools,
            "_directory_service",
            new=AsyncMock(return_value=directory),
        ):
            note = await shared_drive_tools.assert_principal_is_group(
                "team@otbgroup.co.uk",
                user_google_email=USER,
                allow_unverified_group=True,
            )
        assert "Could not verify" in note

    def test_helper_requests_the_scope_groups_get_actually_needs(self):
        """groups.get needs the group-read scope. Declaring only the
        group-member scope would authenticate fine and then 403 on the lookup,
        refusing every default group grant on a valid session."""
        import inspect

        source = inspect.getsource(shared_drive_tools)
        marker = source.split("async def _directory_service")[0]
        decorator = marker[marker.rindex("@require_google_service") :]
        assert "admin_directory_group_read" in decorator
        assert "admin_directory_group_member_read" in decorator

    @pytest.mark.asyncio
    async def test_address_the_directory_does_not_know_is_refused(self):
        directory = MagicMock()
        directory.groups.return_value.get.return_value = _request(_not_found())

        with patch.object(
            shared_drive_tools,
            "_directory_service",
            new=AsyncMock(return_value=directory),
        ):
            with pytest.raises(UserInputError, match="not a Google Group"):
                await shared_drive_tools.assert_principal_is_group(
                    "katie@otbgroup.co.uk",
                    user_google_email=USER,
                    allow_unverified_group=False,
                )


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

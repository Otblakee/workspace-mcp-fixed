"""update_drive_file move guard (adversarial review tier 3, item 3).

A move (remove_parents set) records the previous parents on the file as
appProperties mcp_prev_parents, mcp_moved_at and mcp_moved_by in the same
files.update call, the way soft_delete_drive_file does. A move whose
destination is in a different drive (My Drive counts as a drive of its own,
driveId None) is refused unless allow_cross_drive_move=True. A rename-only
call reads nothing beyond the file itself.

resolve_drive_item and resolve_folder_id are patched; the Drive service is a
MagicMock. Nothing touches the network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import gdrive.drive_tools as mod
from core.utils import UserInputError

USER = "oliver@otbgroup.co.uk"


def _unwrap(fn):
    fn = fn.fn if hasattr(fn, "fn") else fn
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


update_drive_file = _unwrap(mod.update_drive_file)


def _service(destination_drive_id=None, destination_missing=False) -> MagicMock:
    service = MagicMock()
    destination = {"id": "dest"}
    if destination_drive_id is not None:
        destination["driveId"] = destination_drive_id
    files = service.files.return_value
    files.get.return_value.execute.return_value = destination
    files.update.return_value.execute.return_value = {
        "id": "file1",
        "name": "Report.docx",
        "parents": ["dest"],
    }
    return service


@pytest.fixture
def patched(monkeypatch):
    def _install(*, parents, drive_id=None):
        current = {
            "id": "file1",
            "name": "Report.docx",
            "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "parents": parents,
            "appProperties": {"keep": "me"},
        }
        if drive_id is not None:
            current["driveId"] = drive_id
        monkeypatch.setattr(
            mod, "resolve_drive_item", AsyncMock(return_value=("file1", current))
        )
        monkeypatch.setattr(
            mod, "resolve_folder_id", AsyncMock(side_effect=lambda _s, fid: fid)
        )

    return _install


def _update_kwargs(service: MagicMock) -> dict:
    (call,) = service.files.return_value.update.call_args_list
    return call.kwargs


def _get_calls(service: MagicMock):
    return service.files.return_value.get.call_args_list


class TestPreviousParentsRecorded:
    @pytest.mark.asyncio
    async def test_same_drive_move_records_previous_parents(self, patched):
        patched(parents=["srcA", "srcB"], drive_id="drv1")
        service = _service(destination_drive_id="drv1")

        result = await update_drive_file(
            service,
            USER,
            file_id="file1",
            add_parents="dest",
            remove_parents="srcA,srcB",
        )

        assert "Successfully updated file" in result
        kwargs = _update_kwargs(service)
        assert kwargs["addParents"] == "dest"
        assert kwargs["removeParents"] == "srcA,srcB"
        assert kwargs["supportsAllDrives"] is True
        app_properties = kwargs["body"]["appProperties"]
        assert app_properties["mcp_prev_parents"] == "srcA,srcB"
        assert app_properties["mcp_moved_by"] == USER
        # UTC ISO timestamp with an explicit offset.
        assert app_properties["mcp_moved_at"].endswith("+00:00")
        assert "T" in app_properties["mcp_moved_at"]

    @pytest.mark.asyncio
    async def test_destination_drive_is_read_from_first_add_parent(self, patched):
        patched(parents=["src"], drive_id="drv1")
        service = _service(destination_drive_id="drv1")

        await update_drive_file(
            service, USER, "file1", add_parents="dest, other", remove_parents="src"
        )

        (get_call,) = _get_calls(service)
        assert get_call.kwargs == {
            "fileId": "dest",
            "fields": "id, driveId",
            "supportsAllDrives": True,
        }

    @pytest.mark.asyncio
    async def test_remove_only_records_parents_without_destination_lookup(
        self, patched
    ):
        patched(parents=["src"], drive_id="drv1")
        service = _service()

        await update_drive_file(service, USER, "file1", remove_parents="src")

        assert _get_calls(service) == []
        assert (
            _update_kwargs(service)["body"]["appProperties"]["mcp_prev_parents"]
            == "src"
        )


class TestCrossDriveMove:
    @pytest.mark.asyncio
    async def test_shared_drive_to_my_drive_refused(self, patched):
        patched(parents=["src"], drive_id="drv1")
        service = _service(destination_drive_id=None)

        with pytest.raises(UserInputError) as excinfo:
            await update_drive_file(
                service, USER, "file1", add_parents="dest", remove_parents="src"
            )

        message = str(excinfo.value)
        assert "Refused" in message
        assert "from drive drv1 to drive My Drive" in message
        assert "allow_cross_drive_move=True" in message
        assert "copy_drive_file" in message
        service.files.return_value.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_my_drive_to_shared_drive_refused(self, patched):
        patched(parents=["src"], drive_id=None)
        service = _service(destination_drive_id="drv2")

        with pytest.raises(UserInputError, match="from drive My Drive to drive drv2"):
            await update_drive_file(
                service, USER, "file1", add_parents="dest", remove_parents="src"
            )

        service.files.return_value.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_between_two_shared_drives_refused(self, patched):
        patched(parents=["src"], drive_id="drv1")
        service = _service(destination_drive_id="drv2")

        with pytest.raises(UserInputError, match="crosses a drive boundary"):
            await update_drive_file(
                service, USER, "file1", add_parents="dest", remove_parents="src"
            )

        service.files.return_value.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_cross_drive_allowed_with_flag(self, patched):
        patched(parents=["src"], drive_id="drv1")
        service = _service(destination_drive_id="drv2")

        result = await update_drive_file(
            service,
            USER,
            "file1",
            add_parents="dest",
            remove_parents="src",
            allow_cross_drive_move=True,
        )

        assert "Successfully updated file" in result
        kwargs = _update_kwargs(service)
        assert kwargs["addParents"] == "dest"
        assert kwargs["removeParents"] == "src"
        assert kwargs["body"]["appProperties"]["mcp_prev_parents"] == "src"

    @pytest.mark.asyncio
    async def test_same_drive_move_needs_no_flag(self, patched):
        patched(parents=["src"], drive_id="drv1")
        service = _service(destination_drive_id="drv1")

        await update_drive_file(
            service, USER, "file1", add_parents="dest", remove_parents="src"
        )

        assert len(service.files.return_value.update.call_args_list) == 1


class TestRenameOnly:
    @pytest.mark.asyncio
    async def test_rename_makes_no_extra_files_get_and_no_markers(self, patched):
        patched(parents=["src"], drive_id="drv1")
        service = _service()

        result = await update_drive_file(service, USER, "file1", name="Renamed.docx")

        assert "Successfully updated file" in result
        assert _get_calls(service) == []
        kwargs = _update_kwargs(service)
        assert kwargs["body"] == {"name": "Renamed.docx"}
        assert "addParents" not in kwargs
        assert "removeParents" not in kwargs

    def test_signature_exposes_allow_cross_drive_move_default_false(self):
        import inspect

        params = inspect.signature(update_drive_file).parameters
        assert params["allow_cross_drive_move"].default is False

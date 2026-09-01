"""Third hardening round: restore_drive_file refuses another user's
soft-delete, and the attachment store sweeps orphaned files after restart."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _unwrap(fn):
    fn = fn.fn if hasattr(fn, "fn") else fn
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


class TestRestoreOnlyByDeleter:
    def _setup(self, monkeypatch, deleted_by):
        from gdrive import drive_tools

        monkeypatch.setattr(drive_tools, "_get_holding_folder_id", lambda: "HOLD")
        monkeypatch.setattr(
            drive_tools, "resolve_folder_id", AsyncMock(return_value="HOLD")
        )
        current = {
            "name": "Payroll.xlsx",
            "parents": ["HOLD"],
            "appProperties": {
                "mcp_softdeleted": "true",
                "mcp_orig_parents": "ORIG",
                "mcp_deleted_by": deleted_by,
            },
        }
        monkeypatch.setattr(
            drive_tools, "resolve_drive_item", AsyncMock(return_value=("F1", current))
        )
        service = MagicMock()
        service.files.return_value.update.return_value.execute = MagicMock(
            return_value={}
        )
        return drive_tools, service

    @pytest.mark.asyncio
    async def test_other_users_soft_delete_is_refused(self, monkeypatch):
        drive_tools, service = self._setup(monkeypatch, "finance@otbgroup.co.uk")
        with pytest.raises(Exception, match="soft-deleted by finance@otbgroup.co.uk"):
            await _unwrap(drive_tools.restore_drive_file)(
                service, "ops@otbgroup.co.uk", file_id="F1"
            )
        service.files.return_value.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_own_soft_delete_restores(self, monkeypatch):
        drive_tools, service = self._setup(monkeypatch, "Finance@OTBGroup.co.uk")
        out = await _unwrap(drive_tools.restore_drive_file)(
            service, "finance@otbgroup.co.uk", file_id="F1"
        )
        assert "Restored" in out
        kwargs = service.files.return_value.update.call_args.kwargs
        assert kwargs["addParents"] == "ORIG" and kwargs["removeParents"] == "HOLD"

    @pytest.mark.asyncio
    async def test_legacy_marker_without_deleter_still_restores(self, monkeypatch):
        drive_tools, service = self._setup(monkeypatch, "")
        out = await _unwrap(drive_tools.restore_drive_file)(
            service, "anyone@otbgroup.co.uk", file_id="F1"
        )
        assert "Restored" in out


class TestOrphanedAttachmentSweep:
    def test_untracked_old_files_are_removed_and_fresh_ones_kept(
        self, tmp_path, monkeypatch
    ):
        from core import attachment_storage as mod

        monkeypatch.setattr(mod, "STORAGE_DIR", tmp_path)
        store = mod.AttachmentStorage(expiration_seconds=3600)

        old = tmp_path / "leftover_deadbeef.pdf"
        old.write_bytes(b"x")
        stale = time.time() - 7200
        os.utime(old, (stale, stale))

        fresh = tmp_path / "recent_cafef00d.pdf"
        fresh.write_bytes(b"y")

        tracked = tmp_path / "tracked_12345678.pdf"
        tracked.write_bytes(b"z")
        os.utime(tracked, (stale, stale))
        store.register_existing_file("id-1", str(tracked), filename="tracked.pdf")

        sub = tmp_path / "subdir"
        sub.mkdir()

        removed = store.cleanup_expired()
        assert removed == 1
        assert not old.exists()
        assert fresh.exists()
        assert tracked.exists()  # tracked entries follow their own expiry
        assert sub.exists()

    def test_missing_dir_is_fine(self, tmp_path, monkeypatch):
        from core import attachment_storage as mod

        monkeypatch.setattr(mod, "STORAGE_DIR", tmp_path / "nope")
        assert mod.AttachmentStorage().cleanup_expired() == 0

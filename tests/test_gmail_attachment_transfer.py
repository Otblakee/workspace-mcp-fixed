"""Tests for stateless Gmail attachment delivery.

Covers the /attachments/{file_id} route regression (Starlette passes the
Request object, not path params, to custom-route endpoints), the inline
base64 path, the over-cap Drive transfer path, and transfer-folder
resolution/caching. Unit-scoped: Gmail/Drive API calls are mocked.
"""

from __future__ import annotations

import base64
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


BEGIN_MARKER = "--- BEGIN BASE64 CONTENT ---"
END_MARKER = "--- END BASE64 CONTENT ---"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gmail_service(payload: bytes, filename: str, mime_type: str) -> MagicMock:
    """Gmail service mock: attachments().get returns base64url payload, and
    messages().get returns metadata so filename/MIME resolution works."""
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.attachments.return_value.get.return_value.execute = MagicMock(
        return_value={
            "size": len(payload),
            "data": base64.urlsafe_b64encode(payload).decode("ascii"),
        }
    )
    messages.get.return_value.execute = MagicMock(
        return_value={
            "payload": {
                "parts": [
                    {
                        "filename": filename,
                        "mimeType": mime_type,
                        "body": {"attachmentId": "att-1", "size": len(payload)},
                    }
                ]
            }
        }
    )
    return service


def _force_http_mode(monkeypatch):
    import auth.oauth_config as oauth_config
    import core.config as core_config

    monkeypatch.setattr(core_config, "get_transport_mode", lambda: "streamable-http")
    monkeypatch.setattr(oauth_config, "is_stateless_mode", lambda: False)


# ---------------------------------------------------------------------------
# Route regression: serve_attachment must read file_id from path_params
# ---------------------------------------------------------------------------


class TestServeAttachmentRoute:
    def test_serves_stored_bytes_and_404_carries_instance_hint(
        self, tmp_path, monkeypatch
    ):
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from core import attachment_storage as st
        from core.server import serve_attachment

        monkeypatch.setattr(st, "STORAGE_DIR", tmp_path)
        monkeypatch.setattr(st, "_attachment_storage", None)
        storage = st.get_attachment_storage()

        payload = bytes(range(256)) * 8  # 2 KB of non-text bytes
        saved = storage.save_attachment(
            base64_data=base64.urlsafe_b64encode(payload).decode("ascii"),
            filename="blob.bin",
            mime_type="application/octet-stream",
        )

        app = Starlette(
            routes=[
                Route(
                    "/attachments/{file_id}", serve_attachment, methods=["GET"]
                )
            ]
        )
        client = TestClient(app)

        # With the old `serve_attachment(file_id: str)` signature, Starlette
        # passes the Request object into file_id, the metadata lookup misses,
        # and this returns 404 — the assertion below is the regression guard.
        resp = client.get(f"/attachments/{saved.file_id}")
        assert resp.status_code == 200
        assert resp.content == payload

        resp404 = client.get(f"/attachments/{uuid.uuid4()}")
        assert resp404.status_code == 404
        body = resp404.json()
        assert "instance" in body
        assert "hint" in body
        assert "get_gmail_attachment_content" in body["hint"]


# ---------------------------------------------------------------------------
# Inline path: small attachments come back as base64 between markers
# ---------------------------------------------------------------------------


class TestInlineDelivery:
    @pytest.mark.asyncio
    async def test_small_attachment_returned_inline(self, monkeypatch):
        from gmail import gmail_tools

        impl = _unwrap(gmail_tools.get_gmail_attachment_content)
        _force_http_mode(monkeypatch)
        monkeypatch.delenv("ATTACHMENT_INLINE_MAX_BYTES", raising=False)

        payload = os.urandom(22 * 1024)  # ~22 KB, well under the 512 KiB cap
        gmail_service = _make_gmail_service(payload, "photo.png", "image/png")
        drive_service = MagicMock()

        result = await impl(
            gmail_service=gmail_service,
            drive_service=drive_service,
            message_id="msg-1",
            attachment_id="att-1",
            user_google_email="u@example.com",
        )

        assert BEGIN_MARKER in result
        assert END_MARKER in result
        assert "photo.png" in result
        assert "image/png" in result
        assert f"{len(payload)} bytes" in result

        encoded = result.split(BEGIN_MARKER, 1)[1].split(END_MARKER, 1)[0].strip()
        assert base64.b64decode(encoded) == payload

        # No Drive upload for the inline path.
        drive_service.files.assert_not_called()


# ---------------------------------------------------------------------------
# Over-cap path: attachment is transferred to Drive
# ---------------------------------------------------------------------------


class TestDriveTransferDelivery:
    @pytest.mark.asyncio
    async def test_over_cap_attachment_uploaded_to_drive(self, monkeypatch):
        from core import attachment_transfer
        from gmail import gmail_tools

        impl = _unwrap(gmail_tools.get_gmail_attachment_content)
        _force_http_mode(monkeypatch)
        monkeypatch.setenv("ATTACHMENT_INLINE_MAX_BYTES", "1024")  # 1 KiB cap
        monkeypatch.delenv("ATTACHMENT_TRANSFER_FOLDER", raising=False)
        monkeypatch.setattr(attachment_transfer, "_folder_id_cache", {})

        payload = os.urandom(4096)  # 4 KiB, over the 1 KiB cap
        gmail_service = _make_gmail_service(payload, "report.pdf", "application/pdf")

        drive_service = MagicMock()
        files_api = drive_service.files.return_value
        # Both transfer-folder segments already exist.
        files_api.list.return_value.execute = MagicMock(
            return_value={"files": [{"id": "folder-leaf"}]}
        )
        files_api.create.return_value.execute = MagicMock(
            return_value={
                "id": "drive-file-1",
                "name": "20260611-120000_report.pdf",
                "webViewLink": "https://drive.google.com/file/d/drive-file-1/view",
                "size": str(len(payload)),
            }
        )

        result = await impl(
            gmail_service=gmail_service,
            drive_service=drive_service,
            message_id="msg-1",
            attachment_id="att-1",
            user_google_email="u@example.com",
        )

        assert "drive-file-1" in result
        assert "https://drive.google.com/file/d/drive-file-1/view" in result
        assert BEGIN_MARKER not in result
        assert END_MARKER not in result

        # The uploaded media bytes must be byte-identical to the original.
        files_api.create.assert_called_once()
        create_kwargs = files_api.create.call_args.kwargs
        media = create_kwargs["media_body"]
        assert media.size() == len(payload)
        assert media.getbytes(0, media.size()) == payload
        assert create_kwargs["body"]["parents"] == ["folder-leaf"]


# ---------------------------------------------------------------------------
# Transfer-folder resolution: find-or-create + cache
# ---------------------------------------------------------------------------


class TestResolveTransferFolder:
    @pytest.mark.asyncio
    async def test_creates_missing_segments_and_caches(self, monkeypatch):
        from core import attachment_transfer as xfer

        monkeypatch.setattr(xfer, "_folder_id_cache", {})

        drive_service = MagicMock()
        files_api = drive_service.files.return_value
        # Empty Drive: every lookup misses, so each segment gets created.
        files_api.list.return_value.execute = MagicMock(return_value={"files": []})
        created_ids = iter(["folder-a", "folder-b"])
        files_api.create.return_value.execute = MagicMock(
            side_effect=lambda: {"id": next(created_ids)}
        )

        folder_id = await xfer.resolve_transfer_folder(
            drive_service, "Claude Inbox/_attachment-transfer"
        )
        assert folder_id == "folder-b"
        assert files_api.create.call_count == 2

        first_body = files_api.create.call_args_list[0].kwargs["body"]
        second_body = files_api.create.call_args_list[1].kwargs["body"]
        assert first_body["name"] == "Claude Inbox"
        assert first_body["parents"] == ["root"]
        assert second_body["name"] == "_attachment-transfer"
        assert second_body["parents"] == ["folder-a"]

        # Second call hits the module cache: no extra list (or create) calls.
        list_calls_before = files_api.list.call_count
        folder_id_again = await xfer.resolve_transfer_folder(
            drive_service, "Claude Inbox/_attachment-transfer"
        )
        assert folder_id_again == "folder-b"
        assert files_api.list.call_count == list_calls_before
        assert files_api.create.call_count == 2

    @pytest.mark.asyncio
    async def test_query_escapes_single_quotes(self, monkeypatch):
        from core import attachment_transfer as xfer

        monkeypatch.setattr(xfer, "_folder_id_cache", {})

        drive_service = MagicMock()
        files_api = drive_service.files.return_value
        files_api.list.return_value.execute = MagicMock(
            return_value={"files": [{"id": "folder-q"}]}
        )

        await xfer.resolve_transfer_folder(drive_service, "Oli's Inbox")
        query = files_api.list.call_args.kwargs["q"]
        assert "Oli\\'s Inbox" in query

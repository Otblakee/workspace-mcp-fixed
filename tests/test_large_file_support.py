"""Tests for the large-file Drive tools and the related audit fixes.

Three new MCP tools (`create_drive_upload_session`, `confirm_drive_upload`,
`download_drive_file`) plus three audit-log hardening tweaks. Tests are
unit-scoped: Drive API calls and httpx are mocked. Live integration tests
require real OAuth credentials and are out of scope here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service_with_token(token: str = "ya29.fake-token") -> MagicMock:
    """A MagicMock service whose underlying credentials look fresh."""
    service = MagicMock()
    creds = MagicMock()
    creds.token = token
    creds.expired = False
    creds.refresh_token = "refresh-token"
    service._http.credentials = creds
    return service


def _httpx_response(status_code: int, headers: dict | None = None, text: str = "") -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers=headers or {},
        content=text.encode() if text else b"",
        request=httpx.Request("PATCH", "https://example.invalid/x"),
    )


# ---------------------------------------------------------------------------
# Tool 1: create_drive_upload_session
# ---------------------------------------------------------------------------


class TestCreateDriveUploadSession:
    def test_signature(self):
        from gdrive.drive_tools import create_drive_upload_session

        impl = _unwrap(create_drive_upload_session)
        params = list(impl.__wrapped__.__code__.co_varnames[: impl.__wrapped__.__code__.co_argcount]) if False else None
        # Just look at the unwrapped signature
        import inspect

        sig = inspect.signature(impl)
        assert "filename" in sig.parameters
        assert "mime_type" in sig.parameters
        assert "parent_folder_id" in sig.parameters
        assert "file_size" in sig.parameters
        assert sig.parameters["parent_folder_id"].default == "root"
        assert sig.parameters["file_size"].default is None

    @pytest.mark.asyncio
    async def test_happy_path_returns_uri_and_file_id(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.create_drive_upload_session)

        service = _make_service_with_token()
        service.files.return_value.create.return_value.execute = MagicMock(
            return_value={"id": "drive-file-abc", "name": "big.bin", "parents": ["folder-1"]}
        )

        async def fake_resolve(svc, fid):
            return "folder-1"

        monkeypatch.setattr(drive_tools, "resolve_folder_id", fake_resolve)

        ok_response = _httpx_response(
            200,
            headers={"Location": "https://www.googleapis.com/upload/drive/v3/files?upload_id=ABC"},
        )

        async def fake_patch(self, url, headers=None, content=None):
            assert "uploadType=resumable" in url
            assert headers["X-Upload-Content-Type"] == "application/octet-stream"
            assert headers["X-Upload-Content-Length"] == "10485760"
            assert headers["Authorization"] == "Bearer ya29.fake-token"
            return ok_response

        monkeypatch.setattr(httpx.AsyncClient, "patch", fake_patch)

        result = await impl(
            service=service,
            user_google_email="u@example.com",
            filename="big.bin",
            mime_type="application/octet-stream",
            parent_folder_id="folder-1",
            file_size=10 * 1024 * 1024,
        )

        assert "file_id: drive-file-abc" in result
        assert "upload_uri: https://www.googleapis.com/upload/drive/v3/files?upload_id=ABC" in result
        assert "expires_at:" in result
        # Caller-facing instructions reach for confirm_drive_upload, not raw bytes here.
        assert "confirm_drive_upload" in result
        # Bytes-never-pass disclaimer must reach the caller.
        assert "directly from your client to Google" in result
        # files().create() was called once with parents wired through resolve_folder_id.
        service.files.return_value.create.assert_called_once()
        create_kwargs = service.files.return_value.create.call_args.kwargs
        assert create_kwargs["body"]["parents"] == ["folder-1"]
        assert create_kwargs["supportsAllDrives"] is True
        # The placeholder was NOT deleted — happy path keeps it for the
        # subsequent PUT.
        service.files.return_value.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_filename_rejects_before_api_call(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.create_drive_upload_session)
        service = _make_service_with_token()

        async def boom(*a, **kw):  # pragma: no cover
            raise AssertionError("resolve_folder_id should not be called")

        monkeypatch.setattr(drive_tools, "resolve_folder_id", boom)

        with pytest.raises(Exception, match="filename is required"):
            await impl(
                service=service,
                user_google_email="u@example.com",
                filename="",
                mime_type="application/pdf",
            )
        service.files.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_folder_propagates_resolver_error(self, monkeypatch):
        """resolve_folder_id raises on bad folder IDs; the tool must surface
        that without trying to create a placeholder file."""
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.create_drive_upload_session)
        service = _make_service_with_token()

        async def bad_resolver(svc, fid):
            raise Exception("Folder not found: nope")

        monkeypatch.setattr(drive_tools, "resolve_folder_id", bad_resolver)

        with pytest.raises(Exception, match="Folder not found"):
            await impl(
                service=service,
                user_google_email="u@example.com",
                filename="x.bin",
                mime_type="application/octet-stream",
                parent_folder_id="not-a-real-folder",
            )
        service.files.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_init_failure_rolls_back_placeholder(self, monkeypatch):
        """If Google rejects the resumable session, the empty file we
        pre-created must be deleted so it doesn't litter Drive."""
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.create_drive_upload_session)

        service = _make_service_with_token()
        service.files.return_value.create.return_value.execute = MagicMock(
            return_value={"id": "drive-file-xyz"}
        )
        service.files.return_value.delete.return_value.execute = MagicMock(return_value={})

        async def fake_resolve(svc, fid):
            return "folder-1"

        monkeypatch.setattr(drive_tools, "resolve_folder_id", fake_resolve)

        bad_response = _httpx_response(403, text="quotaExceeded")

        async def fake_patch(self, url, headers=None, content=None):
            return bad_response

        monkeypatch.setattr(httpx.AsyncClient, "patch", fake_patch)

        with pytest.raises(Exception, match="HTTP 403"):
            await impl(
                service=service,
                user_google_email="u@example.com",
                filename="big.bin",
                mime_type="application/octet-stream",
                parent_folder_id="folder-1",
            )

        # Cleanup deleted the placeholder.
        service.files.return_value.delete.assert_called_once()
        delete_kwargs = service.files.return_value.delete.call_args.kwargs
        assert delete_kwargs["fileId"] == "drive-file-xyz"
        assert delete_kwargs["supportsAllDrives"] is True

    @pytest.mark.asyncio
    async def test_missing_location_header_rolls_back_placeholder(self, monkeypatch):
        """200 OK without a Location header is undefined state — Google
        didn't actually open a session. The placeholder must be deleted
        so flaky proxies don't accumulate orphans on retry (Codex P2)."""
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.create_drive_upload_session)

        service = _make_service_with_token()
        service.files.return_value.create.return_value.execute = MagicMock(
            return_value={"id": "drive-file-orphan"}
        )
        service.files.return_value.delete.return_value.execute = MagicMock(return_value={})

        async def fake_resolve(svc, fid):
            return "folder-1"

        monkeypatch.setattr(drive_tools, "resolve_folder_id", fake_resolve)

        # 200 but NO Location header.
        no_loc_response = _httpx_response(200, headers={})

        async def fake_patch(self, url, headers=None, content=None):
            return no_loc_response

        monkeypatch.setattr(httpx.AsyncClient, "patch", fake_patch)

        with pytest.raises(Exception, match="no Location header"):
            await impl(
                service=service,
                user_google_email="u@example.com",
                filename="big.bin",
                mime_type="application/octet-stream",
                parent_folder_id="folder-1",
            )

        service.files.return_value.delete.assert_called_once()
        delete_kwargs = service.files.return_value.delete.call_args.kwargs
        assert delete_kwargs["fileId"] == "drive-file-orphan"
        assert delete_kwargs["supportsAllDrives"] is True


# ---------------------------------------------------------------------------
# Tool 2: confirm_drive_upload
# ---------------------------------------------------------------------------


class TestConfirmDriveUpload:
    def test_signature(self):
        import inspect
        from gdrive.drive_tools import confirm_drive_upload

        sig = inspect.signature(_unwrap(confirm_drive_upload))
        assert "file_id" in sig.parameters
        assert "upload_uri" in sig.parameters

    @pytest.mark.asyncio
    async def test_complete_returns_metadata(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.confirm_drive_upload)

        service = _make_service_with_token()
        service.files.return_value.get.return_value.execute = MagicMock(
            return_value={
                "id": "drive-file-abc",
                "name": "big.bin",
                "mimeType": "application/octet-stream",
                "size": "10485760",
                "webViewLink": "https://drive.google.com/file/d/drive-file-abc/view",
            }
        )

        async def fake_put(self, url, headers=None):
            assert headers["Content-Range"] == "bytes */*"
            return _httpx_response(200)

        monkeypatch.setattr(httpx.AsyncClient, "put", fake_put)

        result = await impl(
            service=service,
            user_google_email="u@example.com",
            file_id="drive-file-abc",
            upload_uri="https://www.googleapis.com/upload/drive/v3/files?upload_id=ABC",
        )

        assert "Upload status: complete" in result
        assert "file_id: drive-file-abc" in result
        assert "size: 10485760 bytes" in result
        assert "webViewLink: https://drive.google.com/file/d/drive-file-abc/view" in result

    @pytest.mark.asyncio
    async def test_incomplete_parses_range_header(self, monkeypatch):
        """308 with `Range: bytes=0-499` means 500 bytes received."""
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.confirm_drive_upload)
        service = _make_service_with_token()

        async def fake_put(self, url, headers=None):
            return _httpx_response(308, headers={"Range": "bytes=0-499"})

        monkeypatch.setattr(httpx.AsyncClient, "put", fake_put)

        result = await impl(
            service=service,
            user_google_email="u@example.com",
            file_id="drive-file-abc",
            upload_uri="https://www.googleapis.com/upload/drive/v3/files?upload_id=ABC",
        )
        assert "Upload status: incomplete" in result
        assert "bytes_received: 500" in result
        # files().get() must NOT have been called for an incomplete upload.
        service.files.return_value.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_incomplete_zero_bytes_when_no_range_header(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.confirm_drive_upload)
        service = _make_service_with_token()

        async def fake_put(self, url, headers=None):
            return _httpx_response(308)

        monkeypatch.setattr(httpx.AsyncClient, "put", fake_put)

        result = await impl(
            service=service,
            user_google_email="u@example.com",
            file_id="drive-file-abc",
            upload_uri="https://www.googleapis.com/upload/drive/v3/files?upload_id=ABC",
        )
        assert "Upload status: incomplete" in result
        assert "bytes_received: 0" in result

    @pytest.mark.asyncio
    async def test_expired_session_returns_failed(self, monkeypatch):
        """Per Drive resumable spec, 404 / 410 mean the session expired or
        was abandoned. The tool must report a clean ``failed`` status, not
        a raw HTTP error."""
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.confirm_drive_upload)
        service = _make_service_with_token()

        async def fake_put(self, url, headers=None):
            return _httpx_response(404, text="Not Found")

        monkeypatch.setattr(httpx.AsyncClient, "put", fake_put)

        result = await impl(
            service=service,
            user_google_email="u@example.com",
            file_id="drive-file-abc",
            upload_uri="https://www.googleapis.com/upload/drive/v3/files?upload_id=EXPIRED",
        )
        assert "Upload status: failed" in result
        assert "expired or not found" in result
        assert "HTTP 404" in result

    @pytest.mark.asyncio
    async def test_missing_inputs_rejected(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.confirm_drive_upload)
        service = _make_service_with_token()

        with pytest.raises(Exception, match="file_id is required"):
            await impl(
                service=service,
                user_google_email="u@example.com",
                file_id="",
                upload_uri="https://www.googleapis.com/upload/drive/v3/files?upload_id=ABC",
            )
        with pytest.raises(Exception, match="upload_uri is required"):
            await impl(
                service=service,
                user_google_email="u@example.com",
                file_id="drive-file-abc",
                upload_uri="",
            )


# ---------------------------------------------------------------------------
# upload_uri host validation (Codex P1: SSRF / token exfil)
# ---------------------------------------------------------------------------


class TestConfirmDriveUploadHostValidation:
    """``confirm_drive_upload`` attaches the user's Drive OAuth token to a
    URL the caller controls. Without strict host validation, a malicious
    caller could either steal the token (point it at attacker.example) or
    weaponise the server for authenticated SSRF (point it at the cloud
    metadata service)."""

    def test_validator_accepts_googleapis(self):
        from gdrive.drive_tools import _validate_google_upload_uri

        # Should not raise.
        _validate_google_upload_uri(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=ABC"
        )
        _validate_google_upload_uri(
            "https://storage.googleapis.com/upload/drive/v3/files?upload_id=ABC"
        )

    @pytest.mark.parametrize(
        "uri,reason",
        [
            ("http://www.googleapis.com/upload?upload_id=ABC", "must use https"),
            ("https://attacker.example/steal?upload_id=ABC", "not a permitted"),
            ("https://169.254.169.254/latest/meta-data/", "not a permitted"),
            ("https://localhost:8080/", "not a permitted"),
            ("https://127.0.0.1/", "not a permitted"),
            (
                "https://user:pass@www.googleapis.com/upload?upload_id=ABC",
                "must not contain userinfo",
            ),
            # Subdomain trickery: evil.googleapis.com.attacker.example
            (
                "https://www.googleapis.com.attacker.example/upload?upload_id=ABC",
                "not a permitted",
            ),
            # Empty / nonsense hosts
            ("https:///nohost", "not a permitted"),
        ],
    )
    def test_validator_rejects_dangerous_uris(self, uri, reason):
        from gdrive.drive_tools import _validate_google_upload_uri

        with pytest.raises(Exception, match=reason):
            _validate_google_upload_uri(uri)

    @pytest.mark.asyncio
    async def test_confirm_rejects_non_google_host_before_sending_token(
        self, monkeypatch
    ):
        """The PUT must never fire when the host isn't whitelisted —
        otherwise the caller's Drive token reaches an attacker."""
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.confirm_drive_upload)
        service = _make_service_with_token()

        async def fake_put(self, url, headers=None):  # pragma: no cover
            raise AssertionError(
                "PUT must not be called for a non-Google upload_uri"
            )

        monkeypatch.setattr(httpx.AsyncClient, "put", fake_put)

        with pytest.raises(Exception, match="not a permitted"):
            await impl(
                service=service,
                user_google_email="u@example.com",
                file_id="drive-file-abc",
                upload_uri="https://attacker.example/leak",
            )

    @pytest.mark.asyncio
    async def test_confirm_rejects_metadata_service_uri(self, monkeypatch):
        """SSRF guard: cloud metadata addresses must be unreachable from
        this tool even though they're technically routable from the host."""
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.confirm_drive_upload)
        service = _make_service_with_token()

        async def fake_put(self, url, headers=None):  # pragma: no cover
            raise AssertionError("PUT must not fire for 169.254.169.254")

        monkeypatch.setattr(httpx.AsyncClient, "put", fake_put)

        with pytest.raises(Exception, match="not a permitted"):
            await impl(
                service=service,
                user_google_email="u@example.com",
                file_id="drive-file-abc",
                upload_uri="https://169.254.169.254/latest/meta-data/iam/info",
            )


# ---------------------------------------------------------------------------
# Tool 3: download_drive_file
# ---------------------------------------------------------------------------


class _StubDownloader:
    """Minimal MediaIoBaseDownload stand-in: writes payload in chunks to fh."""

    def __init__(self, fh, request, chunksize: int):
        self._fh = fh
        self._payload = getattr(request, "_payload", b"hello world")
        self._cursor = 0
        self._chunksize = chunksize

    def next_chunk(self):
        if self._cursor >= len(self._payload):
            return None, True
        end = min(self._cursor + self._chunksize, len(self._payload))
        self._fh.write(self._payload[self._cursor : end])
        self._cursor = end
        return None, self._cursor >= len(self._payload)


class TestDownloadDriveFile:
    def test_signature(self):
        import inspect
        from gdrive.drive_tools import download_drive_file

        sig = inspect.signature(_unwrap(download_drive_file))
        assert "file_id" in sig.parameters
        assert "destination_filename" in sig.parameters
        assert sig.parameters["destination_filename"].default is None

    @pytest.mark.asyncio
    async def test_streams_to_disk_and_registers(self, monkeypatch, tmp_path):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.download_drive_file)

        service = _make_service_with_token()
        request_obj = MagicMock()
        request_obj._payload = b"A" * 5000  # 5 KB
        service.files.return_value.get_media.return_value = request_obj

        async def fake_resolve(svc, fid, extra_fields=None):
            return fid, {
                "name": "big.bin",
                "mimeType": "application/octet-stream",
                "size": "5000",
            }

        monkeypatch.setattr(drive_tools, "resolve_drive_item", fake_resolve)
        monkeypatch.setattr(drive_tools, "MediaIoBaseDownload", _StubDownloader)

        # Redirect attachment storage into tmp_path.
        from core import attachment_storage

        monkeypatch.setattr(attachment_storage, "STORAGE_DIR", tmp_path)

        # Force stdio mode so we get a path-form result.
        monkeypatch.setattr(drive_tools, "get_transport_mode", lambda: "stdio")

        result = await impl(
            service=service,
            user_google_email="u@example.com",
            file_id="drive-file-abc",
        )

        assert "Download complete." in result
        assert "size: 5000 bytes" in result
        assert "path: " in result

        # Verify the file actually landed on disk and contains the expected bytes.
        path_line = next(line for line in result.splitlines() if line.startswith("path: "))
        path = Path(path_line[len("path: "):])
        assert path.exists()
        assert path.read_bytes() == b"A" * 5000

        # supportsAllDrives must be set on get_media.
        service.files.return_value.get_media.assert_called_once_with(
            fileId="drive-file-abc", supportsAllDrives=True
        )

    @pytest.mark.asyncio
    async def test_missing_file_id_rejects(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.download_drive_file)

        async def boom(*a, **kw):  # pragma: no cover
            raise AssertionError("resolve_drive_item should not be called")

        monkeypatch.setattr(drive_tools, "resolve_drive_item", boom)

        with pytest.raises(Exception, match="file_id is required"):
            await impl(
                service=_make_service_with_token(),
                user_google_email="u@example.com",
                file_id="",
            )

    @pytest.mark.asyncio
    async def test_nonexistent_file_propagates_resolver_error(self, monkeypatch):
        """If Drive can't resolve the file, the resolver raises — the tool
        must surface that and not leave a half-written file behind."""
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.download_drive_file)

        async def not_found(svc, fid, extra_fields=None):
            raise Exception(f"File not found: {fid}")

        monkeypatch.setattr(drive_tools, "resolve_drive_item", not_found)

        with pytest.raises(Exception, match="File not found"):
            await impl(
                service=_make_service_with_token(),
                user_google_email="u@example.com",
                file_id="missing-file",
            )

    @pytest.mark.asyncio
    async def test_chunk_size_keeps_memory_low(self, monkeypatch, tmp_path):
        """Verify download streams in our configured chunk size, not as one
        giant read. Concretely: payload of 9 chunks must take 9 next_chunk
        iterations."""
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.download_drive_file)

        service = _make_service_with_token()
        request_obj = MagicMock()
        # 9 * chunksize - 1 bytes, so we get 9 iterations to drain.
        request_obj._payload = b"X" * (drive_tools._DOWNLOAD_STREAM_CHUNK * 9 - 1)
        service.files.return_value.get_media.return_value = request_obj

        async def fake_resolve(svc, fid, extra_fields=None):
            return fid, {"name": "huge.bin", "mimeType": "application/octet-stream"}

        monkeypatch.setattr(drive_tools, "resolve_drive_item", fake_resolve)

        chunk_calls = {"n": 0}

        class CountingDownloader(_StubDownloader):
            def next_chunk(self_):
                chunk_calls["n"] += 1
                return super().next_chunk()

        monkeypatch.setattr(drive_tools, "MediaIoBaseDownload", CountingDownloader)
        from core import attachment_storage

        monkeypatch.setattr(attachment_storage, "STORAGE_DIR", tmp_path)
        monkeypatch.setattr(drive_tools, "get_transport_mode", lambda: "stdio")

        await impl(
            service=service,
            user_google_email="u@example.com",
            file_id="huge",
        )

        assert chunk_calls["n"] == 9, f"expected 9 chunked reads, got {chunk_calls['n']}"


# ---------------------------------------------------------------------------
# Audit fixes (Step 4)
# ---------------------------------------------------------------------------


class TestAuditFixes:
    def test_sensitive_includes_upload_uri(self):
        from core.audit import SENSITIVE

        # Pre-existing entries the task says must be present.
        assert "base64_content" in SENSITIVE
        assert "fileUrl" in SENSITIVE
        assert "attachments" in SENSITIVE
        # New entry for the resumable upload session URI.
        assert "upload_uri" in SENSITIVE

    def test_redact_hides_upload_uri(self):
        from core.audit import _redact

        out = _redact({
            "file_id": "drive-file-abc",
            "upload_uri": "https://www.googleapis.com/upload/drive/v3/files?upload_id=SECRET",
        })
        assert "drive-file-abc" in out
        assert "SECRET" not in out
        assert "<redacted:" in out

    def test_resource_id_skips_none_value(self):
        """``str(None)`` would log the literal "None" — useless noise.
        Both the kwargs path and the result-dict path must skip None."""
        from core.audit import _resource_id

        # kwargs has the key but value is None.
        assert _resource_id(None, {"file_id": None}) == ""
        # kwargs has no key; result has the key but value is None.
        assert _resource_id({"id": None}, {}) == ""
        # And empty string is treated the same way.
        assert _resource_id(None, {"file_id": ""}) == ""

    def test_resource_id_returns_real_value_when_present(self):
        from core.audit import _resource_id

        assert _resource_id(None, {"file_id": "abc"}) == "abc"
        assert _resource_id({"id": "xyz"}, {}) == "xyz"

    def test_origin_error_type_unwraps_handle_http_errors_chain(self):
        """``handle_http_errors`` re-raises as ``Exception(message) from
        cause``. Without unwrapping, the audit row says ``Exception:`` for
        every Drive failure, which is useless. We want the cause's class
        name (e.g. ``HttpError``)."""
        from core.audit import _origin_error_type

        class HttpError(Exception):
            pass

        try:
            try:
                raise HttpError("backend boom")
            except HttpError as inner:
                raise Exception("API error in foo: backend boom") from inner
        except Exception as outer:
            assert _origin_error_type(outer) == "HttpError"

    def test_origin_error_type_keeps_subclass_when_outer_is_typed(self):
        """If the outer is already a useful subclass, don't peel — the
        outer name is what we want."""
        from core.audit import _origin_error_type

        class TransientNetworkError(Exception):
            pass

        try:
            raise TransientNetworkError("flaky")
        except TransientNetworkError as e:
            assert _origin_error_type(e) == "TransientNetworkError"


# ---------------------------------------------------------------------------
# AttachmentStorage extension (Step 1 / Tool 3 supporting code)
# ---------------------------------------------------------------------------


class TestAttachmentStorageExtension:
    def test_reserve_path_returns_absolute_path_inside_storage_dir(self, tmp_path, monkeypatch):
        from core import attachment_storage as st

        monkeypatch.setattr(st, "STORAGE_DIR", tmp_path)
        storage = st.AttachmentStorage()
        file_id, path = storage.reserve_path("hello.bin")
        assert os.path.isabs(path)
        assert str(tmp_path) in path
        assert file_id  # non-empty UUID

    def test_register_existing_file_makes_metadata_lookup_work(self, tmp_path, monkeypatch):
        from core import attachment_storage as st

        monkeypatch.setattr(st, "STORAGE_DIR", tmp_path)
        storage = st.AttachmentStorage()
        file_id, path = storage.reserve_path("hi.txt")
        Path(path).write_bytes(b"hello")

        saved = storage.register_existing_file(
            file_id=file_id,
            file_path=path,
            filename="hi.txt",
            mime_type="text/plain",
        )
        assert saved.file_id == file_id
        assert saved.path == path

        meta = storage.get_attachment_metadata(file_id)
        assert meta is not None
        assert meta["filename"] == "hi.txt"
        assert meta["size"] == 5
        assert meta["mime_type"] == "text/plain"

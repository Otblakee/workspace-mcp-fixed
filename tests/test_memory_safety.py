"""Tests for memory-safety and event-loop-blocking fixes.

Covers:
  1. download_chat_attachment streams to disk in chunks with no base64
     round-trip (byte identity), and refreshes stale tokens first.
  2. Size caps on the whole-file-buffering Drive paths
     (get_drive_file_download_url, get_drive_file_content,
     import_to_google_doc's file_url branch).
  3. Blocking DNS resolution moved off the event loop.
  4. save_attachment off-loop calls still produce correct metadata, and
     the attachment metadata table is lock-guarded.
  5. Plain-text Gmail bodies truncated like HTML ones.
"""

from __future__ import annotations

import asyncio
import base64
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

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


def _make_service(token: str = "ya29.fake-token", expired: bool = False) -> MagicMock:
    service = MagicMock()
    creds = MagicMock()
    creds.token = token
    creds.expired = expired
    creds.refresh_token = "refresh-token"
    service._http.credentials = creds
    return service


def _make_chat_message(filename="image.png", content_type="image/png"):
    return {
        "name": "spaces/S/messages/M",
        "text": "msg",
        "attachment": [
            {
                "name": "spaces/S/messages/M/attachments/A",
                "contentName": filename,
                "contentType": content_type,
                "source": "UPLOADED_CONTENT",
                "attachmentDataRef": {"resourceName": "spaces/S/attachments/A"},
            }
        ],
    }


def _make_stream_client(payload: bytes, status_code: int = 200, yield_size: int = 7):
    """Mock httpx.AsyncClient whose .stream(...) is an async context manager
    yielding `payload` in multiple small chunks. Returns (client, chunk_log)."""
    chunk_log: list[int] = []

    resp = Mock()
    resp.status_code = status_code

    async def aiter_bytes(chunk_size=None):
        for i in range(0, len(payload), yield_size):
            chunk = payload[i : i + yield_size]
            chunk_log.append(len(chunk))
            yield chunk

    resp.aiter_bytes = aiter_bytes
    resp.aread = AsyncMock(return_value=b"error body")

    stream_cm = AsyncMock()
    stream_cm.__aenter__ = AsyncMock(return_value=resp)
    stream_cm.__aexit__ = AsyncMock(return_value=False)

    client = AsyncMock()
    client.stream = Mock(return_value=stream_cm)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client, chunk_log


class _StubDownloader:
    """MediaIoBaseDownload stand-in writing request._payload to fh in chunks."""

    last_chunksize = None

    def __init__(self, fh, request, chunksize=None):
        type(self).last_chunksize = chunksize
        self._fh = fh
        self._payload = getattr(request, "_payload", b"stub payload")
        self._cursor = 0
        self._chunksize = chunksize or len(self._payload) or 1

    def next_chunk(self):
        end = min(self._cursor + self._chunksize, len(self._payload))
        self._fh.write(self._payload[self._cursor : end])
        self._cursor = end
        return None, self._cursor >= len(self._payload)


# ---------------------------------------------------------------------------
# Fix 1 + 2: download_chat_attachment streaming + token refresh
# ---------------------------------------------------------------------------


class TestChatAttachmentStreaming:
    @pytest.mark.asyncio
    async def test_streams_to_disk_in_chunks_without_base64(
        self, monkeypatch, tmp_path
    ):
        """Bytes must land on disk identical to the source, written in
        multiple chunks, with no base64 round-trip via save_attachment."""
        from core import attachment_storage as st
        from gchat.chat_tools import download_chat_attachment

        monkeypatch.setattr(st, "STORAGE_DIR", tmp_path)
        storage = st.AttachmentStorage()
        # Streaming path must never go through the base64 entry point.
        storage.save_attachment = Mock(
            side_effect=AssertionError("save_attachment must not be called")
        )

        payload = bytes(range(256)) * 40  # 10240 bytes of binary data
        service = _make_service(token="fresh-token")
        service.spaces().messages().get().execute.return_value = _make_chat_message()

        client, chunk_log = _make_stream_client(payload, yield_size=1000)

        with (
            patch("gchat.chat_tools.httpx.AsyncClient", return_value=client),
            patch("auth.oauth_config.is_stateless_mode", return_value=False),
            patch("core.config.get_transport_mode", return_value="stdio"),
            patch(
                "core.attachment_storage.get_attachment_storage",
                return_value=storage,
            ),
        ):
            result = await _unwrap(download_chat_attachment)(
                service=service,
                user_google_email="u@example.com",
                message_id="spaces/S/messages/M",
            )

        assert "Saved to:" in result
        saved_path = result.split("Saved to: ", 1)[1].splitlines()[0].strip()
        assert Path(saved_path).read_bytes() == payload  # byte identity
        assert len(chunk_log) > 1  # genuinely chunked, not one big read

        # Metadata registered with the true size.
        (file_id,) = list(storage._metadata.keys())
        meta = storage.get_attachment_metadata(file_id)
        assert meta["size"] == len(payload)
        assert meta["mime_type"] == "image/png"

    @pytest.mark.asyncio
    async def test_refreshes_expired_token_before_download(
        self, monkeypatch, tmp_path
    ):
        """Cached services can hold expired tokens; the tool must refresh
        before hitting the media endpoint."""
        from core import attachment_storage as st
        from gchat.chat_tools import download_chat_attachment

        monkeypatch.setattr(st, "STORAGE_DIR", tmp_path)
        storage = st.AttachmentStorage()

        service = _make_service(token="stale-token", expired=True)
        creds = service._http.credentials

        def fake_refresh(request):
            creds.token = "refreshed-token"
            creds.expired = False

        creds.refresh = Mock(side_effect=fake_refresh)
        service.spaces().messages().get().execute.return_value = _make_chat_message()

        client, _ = _make_stream_client(b"data")

        with (
            patch("gchat.chat_tools.httpx.AsyncClient", return_value=client),
            patch("auth.oauth_config.is_stateless_mode", return_value=False),
            patch("core.config.get_transport_mode", return_value="stdio"),
            patch(
                "core.attachment_storage.get_attachment_storage",
                return_value=storage,
            ),
        ):
            result = await _unwrap(download_chat_attachment)(
                service=service,
                user_google_email="u@example.com",
                message_id="spaces/S/messages/M",
            )

        assert "Saved to:" in result
        creds.refresh.assert_called_once()
        headers = client.stream.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer refreshed-token"

    @pytest.mark.asyncio
    async def test_fresh_token_not_refreshed(self, monkeypatch, tmp_path):
        from core import attachment_storage as st
        from gchat.chat_tools import download_chat_attachment

        monkeypatch.setattr(st, "STORAGE_DIR", tmp_path)
        storage = st.AttachmentStorage()

        service = _make_service(token="good-token", expired=False)
        service._http.credentials.refresh = Mock()
        service.spaces().messages().get().execute.return_value = _make_chat_message()

        client, _ = _make_stream_client(b"data")

        with (
            patch("gchat.chat_tools.httpx.AsyncClient", return_value=client),
            patch("auth.oauth_config.is_stateless_mode", return_value=False),
            patch("core.config.get_transport_mode", return_value="stdio"),
            patch(
                "core.attachment_storage.get_attachment_storage",
                return_value=storage,
            ),
        ):
            await _unwrap(download_chat_attachment)(
                service=service,
                user_google_email="u@example.com",
                message_id="spaces/S/messages/M",
            )

        service._http.credentials.refresh.assert_not_called()
        headers = client.stream.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer good-token"


# ---------------------------------------------------------------------------
# Fix 3a: get_drive_file_download_url size cap
# ---------------------------------------------------------------------------


class TestInlineDownloadCap:
    @pytest.mark.asyncio
    async def test_over_cap_refused_with_helpful_message(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.get_drive_file_download_url)
        service = _make_service()
        over = drive_tools.INLINE_DOWNLOAD_MAX_BYTES + 1

        async def fake_resolve(svc, fid, extra_fields=None):
            assert "size" in (extra_fields or "")
            return fid, {
                "name": "huge.bin",
                "mimeType": "application/octet-stream",
                "size": str(over),
            }

        monkeypatch.setattr(drive_tools, "resolve_drive_item", fake_resolve)

        result = await impl(
            service=service,
            user_google_email="u@example.com",
            file_id="huge-file",
        )

        assert "download_drive_file" in result
        assert "WORKSPACE_INLINE_DOWNLOAD_MAX_BYTES" in result
        # The refusal happened before any bytes were requested.
        service.files.return_value.get_media.assert_not_called()
        service.files.return_value.export_media.assert_not_called()

    @pytest.mark.asyncio
    async def test_under_cap_downloads_normally(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.get_drive_file_download_url)
        service = _make_service()
        request_obj = MagicMock()
        request_obj._payload = b"B" * 4096
        service.files.return_value.get_media.return_value = request_obj

        async def fake_resolve(svc, fid, extra_fields=None):
            return fid, {
                "name": "small.bin",
                "mimeType": "application/octet-stream",
                "size": "4096",
            }

        monkeypatch.setattr(drive_tools, "resolve_drive_item", fake_resolve)
        monkeypatch.setattr(drive_tools, "MediaIoBaseDownload", _StubDownloader)
        monkeypatch.setattr(drive_tools, "is_stateless_mode", lambda: False)
        monkeypatch.setattr(drive_tools, "get_transport_mode", lambda: "stdio")

        saved = MagicMock()
        saved.path = "/tmp/small.bin"
        saved.file_id = "fid"
        storage = MagicMock()
        storage.save_attachment.return_value = saved
        monkeypatch.setattr(drive_tools, "get_attachment_storage", lambda: storage)

        result = await impl(
            service=service,
            user_google_email="u@example.com",
            file_id="small-file",
        )

        assert "File downloaded successfully!" in result
        assert "Size: 4.0 KB (4096 bytes)" in result
        storage.save_attachment.assert_called_once()

    @pytest.mark.asyncio
    async def test_native_google_file_without_size_passes(self, monkeypatch):
        """Native Google files report no size; the cap must not block them."""
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.get_drive_file_download_url)
        service = _make_service()
        request_obj = MagicMock()
        request_obj._payload = b"%PDF fake"
        service.files.return_value.export_media.return_value = request_obj

        async def fake_resolve(svc, fid, extra_fields=None):
            return fid, {
                "name": "My Doc",
                "mimeType": "application/vnd.google-apps.document",
            }

        monkeypatch.setattr(drive_tools, "resolve_drive_item", fake_resolve)
        monkeypatch.setattr(drive_tools, "MediaIoBaseDownload", _StubDownloader)
        monkeypatch.setattr(drive_tools, "is_stateless_mode", lambda: False)
        monkeypatch.setattr(drive_tools, "get_transport_mode", lambda: "stdio")

        saved = MagicMock()
        saved.path = "/tmp/My Doc.pdf"
        saved.file_id = "fid"
        storage = MagicMock()
        storage.save_attachment.return_value = saved
        monkeypatch.setattr(drive_tools, "get_attachment_storage", lambda: storage)

        result = await impl(
            service=service,
            user_google_email="u@example.com",
            file_id="doc-file",
        )

        assert "File downloaded successfully!" in result


# ---------------------------------------------------------------------------
# Fix 3b: get_drive_file_content size cap + explicit chunksize
# ---------------------------------------------------------------------------


class TestContentCap:
    @pytest.mark.asyncio
    async def test_over_cap_refused(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.get_drive_file_content)
        service = _make_service()
        over = drive_tools.CONTENT_MAX_BYTES + 1

        async def fake_resolve(svc, fid, extra_fields=None):
            assert "size" in (extra_fields or "")
            return fid, {
                "name": "giant.iso",
                "mimeType": "application/octet-stream",
                "size": str(over),
            }

        monkeypatch.setattr(drive_tools, "resolve_drive_item", fake_resolve)

        result = await impl(
            service=service, user_google_email="u@example.com", file_id="giant"
        )

        assert "download_drive_file" in result
        assert "WORKSPACE_CONTENT_MAX_BYTES" in result
        service.files.return_value.get_media.assert_not_called()
        service.files.return_value.export_media.assert_not_called()

    @pytest.mark.asyncio
    async def test_under_cap_returns_content_with_explicit_chunksize(
        self, monkeypatch
    ):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.get_drive_file_content)
        service = _make_service()
        request_obj = MagicMock()
        request_obj._payload = b"hello, drive text"
        service.files.return_value.get_media.return_value = request_obj

        async def fake_resolve(svc, fid, extra_fields=None):
            return fid, {
                "name": "notes.txt",
                "mimeType": "text/plain",
                "size": "17",
                "webViewLink": "https://drive/x",
            }

        monkeypatch.setattr(drive_tools, "resolve_drive_item", fake_resolve)
        _StubDownloader.last_chunksize = None
        monkeypatch.setattr(drive_tools, "MediaIoBaseDownload", _StubDownloader)

        result = await impl(
            service=service, user_google_email="u@example.com", file_id="notes"
        )

        assert "hello, drive text" in result
        # Explicit, bounded chunksize (default would be 100 MB).
        assert _StubDownloader.last_chunksize == drive_tools._DOWNLOAD_STREAM_CHUNK


# ---------------------------------------------------------------------------
# Fix 3c: import_to_google_doc file_url branch streams with a byte cap
# ---------------------------------------------------------------------------


def _fake_ssrf_stream(payload: bytes, status_code: int = 200):
    @asynccontextmanager
    async def fake(url):
        resp = Mock()
        resp.status_code = status_code

        async def aiter_bytes(chunk_size=None):
            step = 10
            for i in range(0, len(payload), step):
                yield payload[i : i + step]

        resp.aiter_bytes = aiter_bytes
        yield resp

    return fake


class TestImportToGoogleDocUrlCap:
    @pytest.mark.asyncio
    async def test_over_cap_rejected(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.import_to_google_doc)
        service = _make_service()

        async def fake_folder(svc, fid):
            return "root"

        monkeypatch.setattr(drive_tools, "resolve_folder_id", fake_folder)
        monkeypatch.setattr(drive_tools, "MAX_IMPORT_URL_BYTES", 50)
        monkeypatch.setattr(
            drive_tools, "_ssrf_safe_stream", _fake_ssrf_stream(b"Z" * 200)
        )

        with pytest.raises(Exception, match="byte limit"):
            await impl(
                service=service,
                user_google_email="u@example.com",
                file_name="big.md",
                file_url="https://example.com/big.md",
            )
        service.files.return_value.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_under_cap_streams_and_uploads(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.import_to_google_doc)
        service = _make_service()
        service.files.return_value.create.return_value.execute = MagicMock(
            return_value={
                "id": "doc-1",
                "name": "doc",
                "webViewLink": "https://docs/x",
                "mimeType": drive_tools.GOOGLE_DOCS_MIME_TYPE,
            }
        )

        async def fake_folder(svc, fid):
            return "root"

        monkeypatch.setattr(drive_tools, "resolve_folder_id", fake_folder)
        monkeypatch.setattr(
            drive_tools, "_ssrf_safe_stream", _fake_ssrf_stream(b"# Title\n\nBody")
        )

        result = await impl(
            service=service,
            user_google_email="u@example.com",
            file_name="doc.md",
            file_url="https://example.com/doc.md",
        )

        assert "Successfully imported" in result
        service.files.return_value.create.assert_called_once()


# ---------------------------------------------------------------------------
# Fix 4: DNS resolution off the event loop, SSRF semantics intact
# ---------------------------------------------------------------------------


class TestDnsOffEventLoop:
    @pytest.mark.asyncio
    async def test_pinned_fetch_resolves_off_loop(self, monkeypatch):
        from gdrive import drive_tools

        loop_thread = threading.current_thread()
        seen = {}

        def fake_validate(url):
            seen["thread"] = threading.current_thread()
            return ["93.184.216.34"]

        monkeypatch.setattr(drive_tools, "_validate_url_not_internal", fake_validate)

        async def fake_send(self, request, **kwargs):
            return httpx.Response(200, request=request, content=b"ok")

        monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)

        resp = await drive_tools._fetch_url_with_pinned_ip("https://example.com/file")
        assert resp.status_code == 200
        assert seen["thread"] is not loop_thread

    @pytest.mark.asyncio
    async def test_ssrf_stream_resolves_off_loop_and_still_blocks_private(
        self, monkeypatch
    ):
        from gdrive import drive_tools

        loop_thread = threading.current_thread()
        seen = {}

        def deny_private(url):
            seen["thread"] = threading.current_thread()
            raise ValueError(
                "URLs pointing to private/internal networks are not allowed"
            )

        monkeypatch.setattr(drive_tools, "_validate_url_not_internal", deny_private)

        with pytest.raises(ValueError, match="private/internal"):
            async with drive_tools._ssrf_safe_stream("https://internal.example/x"):
                pass  # pragma: no cover
        assert seen["thread"] is not loop_thread


# ---------------------------------------------------------------------------
# Fix 5: save_attachment off-loop + lock-guarded metadata
# ---------------------------------------------------------------------------


class TestAttachmentStorageThreading:
    @pytest.mark.asyncio
    async def test_save_attachment_via_to_thread_produces_correct_metadata(
        self, monkeypatch, tmp_path
    ):
        from core import attachment_storage as st

        monkeypatch.setattr(st, "STORAGE_DIR", tmp_path)
        storage = st.AttachmentStorage()

        data = b"\x00\x01binary payload\xff" * 32
        b64 = base64.urlsafe_b64encode(data).decode("utf-8")

        result = await asyncio.to_thread(
            storage.save_attachment,
            base64_data=b64,
            filename="blob.bin",
            mime_type="application/octet-stream",
        )

        assert Path(result.path).read_bytes() == data
        meta = storage.get_attachment_metadata(result.file_id)
        assert meta is not None
        assert meta["size"] == len(data)
        assert meta["filename"] == "blob.bin"
        assert meta["mime_type"] == "application/octet-stream"

    def test_metadata_guarded_by_lock_under_concurrent_writers(
        self, monkeypatch, tmp_path
    ):
        from core import attachment_storage as st

        monkeypatch.setattr(st, "STORAGE_DIR", tmp_path)
        storage = st.AttachmentStorage()
        assert isinstance(storage._lock, type(threading.RLock()))

        errors = []

        def writer(n):
            try:
                for i in range(25):
                    fid, path = storage.reserve_path(f"f{n}-{i}.bin")
                    Path(path).write_bytes(b"x")
                    storage.register_existing_file(
                        file_id=fid, file_path=path, filename=f"f{n}-{i}.bin"
                    )
                    storage.get_attachment_metadata(fid)
                    storage.cleanup_expired()
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(storage._metadata) == 100


# ---------------------------------------------------------------------------
# Fix 6: plain-text Gmail bodies truncated
# ---------------------------------------------------------------------------


class TestPlainTextTruncation:
    def test_long_plain_text_truncated_with_marker(self):
        from gmail.gmail_tools import HTML_BODY_TRUNCATE_LIMIT, _format_body_content

        long_text = "x" * (HTML_BODY_TRUNCATE_LIMIT + 5000)
        out = _format_body_content(long_text, "")

        assert len(out) < len(long_text)
        assert out.startswith("x" * 100)
        assert "[Content truncated...]" in out
        assert len(out) <= HTML_BODY_TRUNCATE_LIMIT + len("\n\n[Content truncated...]")

    def test_short_plain_text_unchanged(self):
        from gmail.gmail_tools import _format_body_content

        assert _format_body_content("hello world", "") == "hello world"

    def test_html_truncation_still_applies(self):
        from gmail.gmail_tools import HTML_BODY_TRUNCATE_LIMIT, _format_body_content

        html = "<p>" + "y" * (HTML_BODY_TRUNCATE_LIMIT + 5000) + "</p>"
        out = _format_body_content("", html)
        assert "[Content truncated...]" in out
        assert len(out) <= HTML_BODY_TRUNCATE_LIMIT + len("\n\n[Content truncated...]")

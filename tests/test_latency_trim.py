"""Per-call latency trims (v1.18.0).

Five fixed costs sat on every tool call before the Google API was even
reached, measured on the live Render service on 2026-10-01:

1. the OAuth proxy re-verified the upstream Google token with two Google
   calls on every POST /mcp (150 to 290 ms);
2. a full ``gc.collect()`` on the event loop after every call (about 40 ms);
3. a ``files.get`` to resolve the folder even when it was ``root``;
4. a resumable (two-request) upload for every payload, however small;
5. a fresh SSL context and certifi read for every Google connection.

These tests pin each trim.
"""

from __future__ import annotations

import base64
import io
import re
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

IDENTITY = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
]


def _access_token(token: str, expires_in: int = 3600):
    from fastmcp.server.auth.auth import AccessToken

    return AccessToken(
        token=token,
        client_id="sub-1",
        scopes=IDENTITY,
        expires_at=int(time.time()) + expires_in,
        subject="sub-1",
        claims={"email": "oli@example.test"},
    )


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.delenv("OAUTH_VERIFY_CACHE_TTL_S", raising=False)
    from auth.google_provider import WorkspaceGoogleProvider

    return WorkspaceGoogleProvider(
        client_id="test-client-id.apps.googleusercontent.com",
        client_secret="test-secret",
        base_url="https://mcp.example.test",
        redirect_path="/oauth2callback",
        required_scopes=IDENTITY,
        valid_scopes=IDENTITY,
    )


# ---------------------------------------------------------------------------
# 1. Upstream token verification is cached
# ---------------------------------------------------------------------------


class TestVerifierCache:
    def test_provider_installs_the_cached_verifier(self, provider):
        from auth.google_provider import CachedGoogleTokenVerifier

        verifier = provider.token_verifier
        assert isinstance(verifier, CachedGoogleTokenVerifier)
        assert verifier.cache.enabled
        # The gate's scopes survive the swap (normalised form).
        assert set(verifier.required_scopes or []) == set(IDENTITY)

    @pytest.mark.asyncio
    async def test_second_verification_of_same_token_makes_no_google_call(
        self, provider, monkeypatch
    ):
        from fastmcp.server.auth.providers import google as google_mod

        calls = []

        async def fake_verify(self, token):
            calls.append(token)
            return _access_token(token)

        monkeypatch.setattr(google_mod.GoogleTokenVerifier, "verify_token", fake_verify)
        verifier = provider.token_verifier

        first = await verifier.verify_token("ya29.abc")
        second = await verifier.verify_token("ya29.abc")
        third = await verifier.verify_token("ya29.other")

        assert calls == ["ya29.abc", "ya29.other"]
        assert first is not None and second is not None and third is not None
        assert second.claims["email"] == "oli@example.test"
        # A copy, never the cached object itself.
        assert second is not first

    @pytest.mark.asyncio
    async def test_failed_verification_is_not_cached(self, provider, monkeypatch):
        from fastmcp.server.auth.providers import google as google_mod

        calls = []

        async def fake_verify(self, token):
            calls.append(token)
            return None

        monkeypatch.setattr(google_mod.GoogleTokenVerifier, "verify_token", fake_verify)
        verifier = provider.token_verifier
        assert await verifier.verify_token("bad") is None
        assert await verifier.verify_token("bad") is None
        assert calls == ["bad", "bad"]

    @pytest.mark.asyncio
    async def test_entry_never_outlives_the_token(self, provider, monkeypatch):
        from fastmcp.server.auth.providers import google as google_mod

        calls = []

        async def fake_verify(self, token):
            calls.append(token)
            # Already expired upstream: the cache must not keep it.
            return _access_token(token, expires_in=-1)

        monkeypatch.setattr(google_mod.GoogleTokenVerifier, "verify_token", fake_verify)
        verifier = provider.token_verifier
        await verifier.verify_token("t")
        await verifier.verify_token("t")
        assert calls == ["t", "t"]

    @pytest.mark.asyncio
    async def test_shared_http_client_is_used_and_restored(self, provider, monkeypatch):
        from fastmcp.server.auth.providers import google as google_mod

        seen = []

        async def fake_verify(self, token):
            seen.append(self._http_client)
            return _access_token(token)

        monkeypatch.setattr(google_mod.GoogleTokenVerifier, "verify_token", fake_verify)
        verifier = provider.token_verifier
        assert verifier._http_client is None
        await verifier.verify_token("a")
        await verifier.verify_token("b")
        assert seen[0] is not None and seen[0] is seen[1]
        # Restored after the call so the base class never closes the pool.
        assert verifier._http_client is None

    def test_ttl_env_zero_disables_the_cache(self, monkeypatch):
        monkeypatch.setenv("OAUTH_VERIFY_CACHE_TTL_S", "0")
        from auth.google_provider import WorkspaceGoogleProvider

        p = WorkspaceGoogleProvider(
            client_id="x.apps.googleusercontent.com",
            client_secret="s",
            base_url="https://mcp.example.test",
            required_scopes=IDENTITY,
        )
        assert not p.token_verifier.cache.enabled

    @pytest.mark.parametrize(
        "raw, expected",
        [("", 300), ("120", 120), ("abc", 300), ("-5", 300), (" 60 ", 60)],
    )
    def test_ttl_env_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv("OAUTH_VERIFY_CACHE_TTL_S", raw)
        from auth.google_provider import verify_cache_ttl_seconds

        assert verify_cache_ttl_seconds() == expected

    def test_default_ttl_is_five_minutes(self, monkeypatch):
        monkeypatch.delenv("OAUTH_VERIFY_CACHE_TTL_S", raising=False)
        from auth.google_provider import (
            DEFAULT_VERIFY_CACHE_TTL_SECONDS,
            verify_cache_ttl_seconds,
        )

        assert verify_cache_ttl_seconds() == DEFAULT_VERIFY_CACHE_TTL_SECONDS == 300


# ---------------------------------------------------------------------------
# 2. No full-heap gc.collect() on the request path
# ---------------------------------------------------------------------------


class TestGcGeneration:
    @pytest.mark.parametrize("path", ["auth/service_decorator.py", "core/audit.py"])
    def test_no_bare_full_collect(self, path):
        src = (REPO_ROOT / path).read_text(encoding="utf-8")
        assert not re.search(r"\bgc\.collect\(\s*\)", src), (
            f"{path}: use gc.collect(1); a full sweep blocks the event loop "
            "for ~40 ms on every tool call"
        )
        assert "gc.collect(1)" in src


# ---------------------------------------------------------------------------
# 3. "root" needs no files.get
# ---------------------------------------------------------------------------


class TestRootFolder:
    @pytest.mark.asyncio
    async def test_root_short_circuits(self):
        from gdrive.drive_helpers import resolve_folder_id

        service = Mock()
        assert await resolve_folder_id(service, "root") == "root"
        service.files.assert_not_called()

    @pytest.mark.asyncio
    async def test_other_ids_still_resolve(self):
        from gdrive.drive_helpers import FOLDER_MIME_TYPE, resolve_folder_id

        service = Mock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "abc",
            "mimeType": FOLDER_MIME_TYPE,
        }
        assert await resolve_folder_id(service, "abc") == "abc"
        service.files.return_value.get.assert_called_once()


# ---------------------------------------------------------------------------
# 4. Multipart for small uploads, resumable above 5 MB
# ---------------------------------------------------------------------------


class TestMediaUpload:
    def test_small_payload_is_one_request(self):
        from gdrive.drive_helpers import build_media_upload

        media = build_media_upload(io.BytesIO(b"x" * 10), "text/plain", 10)
        assert media.resumable() is False

    def test_boundary_is_inclusive(self):
        from gdrive.drive_helpers import SIMPLE_UPLOAD_MAX_BYTES, build_media_upload

        media = build_media_upload(
            io.BytesIO(b""), "text/plain", SIMPLE_UPLOAD_MAX_BYTES
        )
        assert media.resumable() is False

    def test_large_payload_is_resumable_with_chunksize(self):
        from gdrive.drive_helpers import SIMPLE_UPLOAD_MAX_BYTES, build_media_upload

        media = build_media_upload(
            io.BytesIO(b""),
            "text/plain",
            SIMPLE_UPLOAD_MAX_BYTES + 1,
            chunksize=1024 * 1024,
        )
        assert media.resumable() is True
        assert media.chunksize() == 1024 * 1024

    def test_unknown_size_is_resumable(self):
        from gdrive.drive_helpers import build_media_upload

        assert (
            build_media_upload(io.BytesIO(b""), "text/plain", None).resumable() is True
        )

    @pytest.mark.asyncio
    async def test_create_drive_file_small_base64_uploads_in_one_request(
        self, monkeypatch
    ):
        import gdrive.drive_tools as drive_tools
        from tests.test_mcp_fixes import _unwrap

        monkeypatch.setattr(
            drive_tools, "resolve_folder_id", AsyncMock(return_value="root")
        )
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            req = Mock()
            req.execute.return_value = {"id": "f1", "name": "a.bin", "webViewLink": "L"}
            return req

        service = Mock()
        service.files.return_value.create.side_effect = fake_create
        payload = base64.b64encode(b"hello world" * 100).decode()
        out = await _unwrap(drive_tools.create_drive_file)(
            service=service,
            user_google_email="oli@example.test",
            file_name="a.bin",
            folder_id="root",
            base64_content=payload,
            mime_type="application/octet-stream",
        )
        assert "Successfully created" in out
        assert captured["media_body"].resumable() is False
        assert captured["supportsAllDrives"] is True

    @pytest.mark.asyncio
    async def test_create_drive_file_text_content_uploads_in_one_request(
        self, monkeypatch
    ):
        import gdrive.drive_tools as drive_tools
        from tests.test_mcp_fixes import _unwrap

        monkeypatch.setattr(
            drive_tools, "resolve_folder_id", AsyncMock(return_value="root")
        )
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            req = Mock()
            req.execute.return_value = {"id": "f1", "name": "a.txt", "webViewLink": "L"}
            return req

        service = Mock()
        service.files.return_value.create.side_effect = fake_create
        await _unwrap(drive_tools.create_drive_file)(
            service=service,
            user_google_email="oli@example.test",
            file_name="a.txt",
            folder_id="root",
            content="hello",
            mime_type="text/plain",
        )
        assert captured["media_body"].resumable() is False


class TestBase64Decode:
    def test_decodes_in_chunks_and_counts_bytes(self):
        from gdrive.drive_tools import _decode_base64_into

        raw = bytes(range(256)) * 40
        sink = io.BytesIO()
        assert _decode_base64_into(base64.b64encode(raw).decode(), sink) == len(raw)
        assert sink.getvalue() == raw

    def test_invalid_input_raises(self):
        from gdrive.drive_tools import _decode_base64_into

        with pytest.raises((ValueError, TypeError)):
            _decode_base64_into("not base64!!", io.BytesIO())

    def test_stream_size_preserves_position(self):
        from gdrive.drive_tools import _stream_size

        s = io.BytesIO(b"abcdef")
        s.seek(2)
        assert _stream_size(s) == 6
        assert s.tell() == 2
        assert _stream_size(object()) is None


# ---------------------------------------------------------------------------
# 5. One SSL context per argument set
# ---------------------------------------------------------------------------


class TestSslContextCache:
    def test_installed_and_cached(self):
        import httplib2

        from core.ssl_context_cache import (
            install_httplib2_ssl_context_cache,
            is_installed,
        )

        assert install_httplib2_ssl_context_cache() is True
        assert install_httplib2_ssl_context_cache() is True  # idempotent
        assert is_installed()
        builder = httplib2._build_ssl_context
        builder.cache_clear()
        a = builder(False, httplib2.CA_CERTS)
        b = builder(False, httplib2.CA_CERTS)
        assert a is b
        assert builder.cache_info().hits >= 1
        c = builder(True, httplib2.CA_CERTS)
        assert c is not a

    def test_server_import_installs_it(self):
        import core.server  # noqa: F401
        from core.ssl_context_cache import is_installed

        assert is_installed()


# ---------------------------------------------------------------------------
# Logging: the verifier's calls no longer flood the Render log
# ---------------------------------------------------------------------------


class TestHttpx2LoggerSilenced:
    @pytest.mark.parametrize("path", ["main.py", "fastmcp_server.py"])
    def test_httpx2_and_httpcore2_silenced(self, path):
        src = (REPO_ROOT / path).read_text(encoding="utf-8")
        assert 'logging.getLogger("httpx2").setLevel(logging.WARNING)' in src
        assert 'logging.getLogger("httpcore2").setLevel(logging.WARNING)' in src

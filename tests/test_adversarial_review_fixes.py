"""Tests for the fixes from the adversarial review.

1. ``core.attachment_storage`` sweeps the storage directory (files left by a
   previous process) and enforces a total-size cap.
2. The OAuth proxy ``DiskStore`` is built with a ``max_size`` so diskcache
   evicts instead of growing without bound.
3. Error text written to audit rows is scrubbed of URL paths and query
   strings, Google access tokens and JWTs.
4. The start-up banner prints only "set" / "not set" for the client secret.
5. ``validate_file_path`` blocks ``/etc/secrets`` and the OAuth proxy disk
   directory.
6. A domain-policy rejection of a FastMCP-validated token is terminal in the
   auth middleware; a request with no token still reaches session binding.
7. ``_ensure_audit_started`` resets its flag on failure so the next call
   retries.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# 1. Attachment storage: directory sweep and size cap
# ---------------------------------------------------------------------------


def _old_file(directory: Path, name: str, age_s: int, size: int = 10) -> Path:
    p = directory / name
    p.write_bytes(b"x" * size)
    stamp = time.time() - age_s
    os.utime(p, (stamp, stamp))
    return p


class TestAttachmentDirectorySweep:
    @pytest.fixture
    def storage_dir(self, tmp_path, monkeypatch):
        from core import attachment_storage as st

        monkeypatch.setattr(st, "STORAGE_DIR", tmp_path)
        monkeypatch.delenv("WORKSPACE_ATTACHMENT_MAX_BYTES", raising=False)
        return tmp_path

    def test_stale_files_removed_on_init(self, storage_dir, caplog):
        from core import attachment_storage as st

        stale = _old_file(storage_dir, "left-behind.pdf", age_s=7200)
        fresh = _old_file(storage_dir, "recent.pdf", age_s=10)

        with caplog.at_level(logging.INFO, logger="core.attachment_storage"):
            st.AttachmentStorage(expiration_seconds=3600)

        assert not stale.exists()
        assert fresh.exists()
        assert any("removed 1 stale file" in r.getMessage() for r in caplog.records)

    def test_stale_files_removed_on_cleanup_expired(self, storage_dir):
        from core import attachment_storage as st

        storage = st.AttachmentStorage(expiration_seconds=3600)
        # Written after construction, so the init sweep never saw it.
        stale = _old_file(storage_dir, "old.bin", age_s=7200)
        fresh = _old_file(storage_dir, "new.bin", age_s=5)

        storage.cleanup_expired()

        assert not stale.exists()
        assert fresh.exists()

    def test_registered_unexpired_file_with_old_mtime_survives(self, storage_dir):
        """A file still tracked in _metadata and not expired is kept even if
        its mtime is old (e.g. a download that preserved a source mtime)."""
        from core import attachment_storage as st

        storage = st.AttachmentStorage(expiration_seconds=3600)
        file_id, path = storage.reserve_path("report.pdf")
        p = Path(path)
        p.write_bytes(b"data")
        stamp = time.time() - 7200
        os.utime(p, (stamp, stamp))
        storage.register_existing_file(file_id, path, filename="report.pdf")

        storage.cleanup_expired()

        assert p.exists()
        assert storage.get_attachment_path(file_id) == p

    def test_fresh_registered_file_survives(self, storage_dir):
        from core import attachment_storage as st

        storage = st.AttachmentStorage(expiration_seconds=3600)
        saved = storage.save_attachment("aGVsbG8=", filename="hello.txt")

        storage.cleanup_expired()

        assert Path(saved.path).exists()
        assert storage.get_attachment_path(saved.file_id) is not None

    def test_symlinks_and_directories_untouched(self, storage_dir):
        from core import attachment_storage as st

        outside = storage_dir.parent / "outside.txt"
        outside.write_text("keep me")
        stamp = time.time() - 7200
        os.utime(outside, (stamp, stamp))
        link = storage_dir / "link.txt"
        link.symlink_to(outside)
        os.utime(link, (stamp, stamp), follow_symlinks=False)
        sub = storage_dir / "subdir"
        sub.mkdir()
        os.utime(sub, (stamp, stamp))

        st.AttachmentStorage(expiration_seconds=3600)

        assert outside.exists()
        assert link.is_symlink()
        assert sub.is_dir()

    def test_size_cap_evicts_oldest_first(self, storage_dir, monkeypatch, caplog):
        from core import attachment_storage as st

        monkeypatch.setenv("WORKSPACE_ATTACHMENT_MAX_BYTES", "250")
        storage = st.AttachmentStorage(expiration_seconds=3600)
        oldest = _old_file(storage_dir, "a.bin", age_s=300, size=100)
        middle = _old_file(storage_dir, "b.bin", age_s=200, size=100)
        newest = _old_file(storage_dir, "c.bin", age_s=100, size=100)

        with caplog.at_level(logging.WARNING, logger="core.attachment_storage"):
            removed = storage.sweep_directory()

        # 300 bytes over a 250 cap: only the oldest goes.
        assert not oldest.exists()
        assert middle.exists()
        assert newest.exists()
        assert removed == 1
        assert any("exceeded 250 bytes" in r.getMessage() for r in caplog.records)

    def test_size_cap_default_is_512_mib(self, monkeypatch):
        from core import attachment_storage as st

        monkeypatch.delenv("WORKSPACE_ATTACHMENT_MAX_BYTES", raising=False)
        assert st._max_storage_bytes() == 512 * 1024 * 1024
        monkeypatch.setenv("WORKSPACE_ATTACHMENT_MAX_BYTES", "not-a-number")
        assert st._max_storage_bytes() == 512 * 1024 * 1024
        monkeypatch.setenv("WORKSPACE_ATTACHMENT_MAX_BYTES", "1024")
        assert st._max_storage_bytes() == 1024

    def test_missing_directory_is_not_created_by_sweep(self, tmp_path, monkeypatch):
        from core import attachment_storage as st

        missing = tmp_path / "never-made"
        monkeypatch.setattr(st, "STORAGE_DIR", missing)
        storage = st.AttachmentStorage()
        assert storage.sweep_directory() == 0
        assert not missing.exists()


# ---------------------------------------------------------------------------
# 2. OAuth proxy DiskStore gets a max_size
# ---------------------------------------------------------------------------


class TestOAuthProxyDiskStoreCap:
    def test_default_and_env_override(self, monkeypatch):
        import core.server as core_server

        monkeypatch.delenv("WORKSPACE_MCP_OAUTH_PROXY_DISK_MAX_BYTES", raising=False)
        assert core_server.get_oauth_proxy_disk_max_bytes() == 256 * 1024 * 1024
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_DISK_MAX_BYTES", "1048576")
        assert core_server.get_oauth_proxy_disk_max_bytes() == 1048576
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_DISK_MAX_BYTES", "junk")
        assert core_server.get_oauth_proxy_disk_max_bytes() == 256 * 1024 * 1024
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_DISK_MAX_BYTES", "0")
        assert core_server.get_oauth_proxy_disk_max_bytes() == 256 * 1024 * 1024

    def test_disk_store_constructed_with_max_size(self, tmp_path, monkeypatch):
        import core.server as core_server
        import key_value.aio.stores.disk as disk_mod

        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "disk")
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY", str(tmp_path))
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_DISK_MAX_BYTES", "4096")
        monkeypatch.delenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST", raising=False)
        monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", raising=False)

        config = MagicMock()
        config.is_oauth21_enabled.return_value = True
        config.is_configured.return_value = True
        config.is_external_oauth21_provider.return_value = False
        config.client_id = "client-id"
        config.client_secret = "a-long-enough-client-secret-value"
        config.redirect_path = "/oauth2callback"
        config.get_oauth_base_url.return_value = "http://localhost:8000"

        fake_disk_store = MagicMock(name="DiskStore")
        fake_provider_cls = MagicMock(name="GoogleProvider")
        fake_provider_cls.return_value.get_well_known_routes.return_value = []

        saved_auth = core_server.server.auth
        saved_provider = core_server._auth_provider
        try:
            with (
                patch.object(
                    core_server, "get_transport_mode", lambda: "streamable-http"
                ),
                patch("auth.oauth_config.get_oauth_config", return_value=config),
                patch.object(disk_mod, "DiskStore", fake_disk_store),
                patch.object(core_server, "GoogleProvider", fake_provider_cls),
                patch.object(core_server, "set_auth_provider", MagicMock()),
            ):
                core_server.configure_server_for_http()
        finally:
            core_server.server.auth = saved_auth
            core_server._auth_provider = saved_provider

        fake_disk_store.assert_called_once()
        kwargs = fake_disk_store.call_args.kwargs
        assert kwargs["directory"] == str(tmp_path)
        assert kwargs["max_size"] == 4096
        # The store must be what the provider was given (wrapped in Fernet).
        assert fake_provider_cls.called

    def test_disk_store_accepts_max_size_keyword(self):
        """Pin the real constructor signature so a library bump that renames
        the parameter fails here rather than at boot."""
        import inspect

        from key_value.aio.stores.disk import DiskStore

        assert "max_size" in inspect.signature(DiskStore.__init__).parameters


# ---------------------------------------------------------------------------
# 3. Audit error text scrubbing
# ---------------------------------------------------------------------------


class TestAuditErrorScrub:
    def test_presigned_url_reduced_to_scheme_and_host(self):
        from core.audit import _scrub_error_text

        text = (
            "HttpError 403 fetching https://bucket.s3.eu-west-2.amazonaws.com/"
            "path/to/file.pdf?X-Amz-Signature=abcdef0123&X-Amz-Credential=AKIA"
        )
        out = _scrub_error_text(text)
        assert out == "HttpError 403 fetching https://bucket.s3.eu-west-2.amazonaws.com"
        assert "Signature" not in out

    def test_http_url_also_scrubbed(self):
        from core.audit import _scrub_error_text

        assert (
            _scrub_error_text("failed: http://example.com/a/b?c=d done")
            == "failed: http://example.com done"
        )

    def test_bearer_token_masked(self):
        from core.audit import _scrub_error_text

        out = _scrub_error_text("invalid token ya29.a0AfH6SMBx-Yz_12.34 rejected")
        assert out == "invalid token <token> rejected"

    def test_jwt_masked(self):
        from core.audit import _scrub_error_text

        jwt = (
            "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        out = _scrub_error_text(f"jwt {jwt} bad signature")
        assert out == "jwt <token> bad signature"

    def test_short_dotted_strings_left_alone(self):
        from core.audit import _scrub_error_text

        assert _scrub_error_text("file report.v1.pdf not found") == (
            "file report.v1.pdf not found"
        )

    def test_truncated_to_300(self):
        from core.audit import _scrub_error_text

        assert len(_scrub_error_text("z" * 1000)) == 300

    def test_none_and_non_str(self):
        from core.audit import _scrub_error_text

        assert _scrub_error_text(None) == ""
        assert _scrub_error_text(42) == "42"

    def test_inspect_result_scrubs_string_result(self):
        from core.audit import _inspect_result_for_error

        is_err, detail = _inspect_result_for_error(
            "Error: could not fetch https://x.example/secret?sig=123"
        )
        assert is_err
        assert detail == "Error: could not fetch https://x.example"

    def test_inspect_result_scrubs_dict_and_replies(self):
        from core.audit import _inspect_result_for_error

        _, detail = _inspect_result_for_error(
            {"error": {"message": "token ya29.abc-def rejected"}}
        )
        assert detail == "result.error: token <token> rejected"
        _, detail = _inspect_result_for_error(
            {"replies": [{}, {"error": "see https://h.example/p?q=1"}]}
        )
        assert detail == "replies[1].error: see https://h.example"

    @pytest.mark.asyncio
    async def test_exception_message_scrubbed_in_audit_row(self, monkeypatch):
        from core import audit

        monkeypatch.setattr(audit, "ENABLED", True)
        fresh = audit.AuditLogger()
        monkeypatch.setattr(audit, "_inst", fresh)

        @audit.audit_log("create_drive_file")
        async def failing(fileUrl: str = ""):
            raise RuntimeError(
                f"Download failed for {fileUrl} with token ya29.secret-token-value"
            )

        with pytest.raises(RuntimeError):
            await failing(fileUrl="https://s3.example.com/k?X-Amz-Signature=deadbeef")

        row = fresh.q.get_nowait()
        assert row["status"] == "error"
        assert "X-Amz-Signature" not in row["error"]
        assert "deadbeef" not in row["error"]
        assert "secret-token-value" not in row["error"]
        assert "https://s3.example.com" in row["error"]
        assert "<token>" in row["error"]


# ---------------------------------------------------------------------------
# 4. Start-up banner never prints part of the client secret
# ---------------------------------------------------------------------------


class TestBannerClientSecret:
    def test_source_prints_only_set_or_not_set(self):
        src = (Path(__file__).resolve().parent.parent / "main.py").read_text()
        assert "client_secret[:4]" not in src
        assert "client_secret[-4:]" not in src
        assert '"set" if os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()' in src
        assert '"not set"' in src


# ---------------------------------------------------------------------------
# 5. validate_file_path: /etc/secrets and the OAuth proxy disk directory
# ---------------------------------------------------------------------------


class TestValidateFilePathNewBlocks:
    def test_etc_secrets_blocked(self, monkeypatch):
        from core.utils import validate_file_path

        real_exists = Path.exists

        def fake_exists(self):
            if str(self).startswith("/etc/secrets"):
                return True
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", fake_exists)
        with pytest.raises(ValueError, match="restricted system location"):
            validate_file_path("/etc/secrets/signature-sa.json")
        with pytest.raises(ValueError, match="restricted system location"):
            validate_file_path("/etc/secrets")

    def test_oauth_proxy_disk_directory_blocked(self, tmp_path, monkeypatch):
        from core.utils import validate_file_path

        proxy_dir = tmp_path / "oauth-proxy"
        proxy_dir.mkdir()
        cache_file = proxy_dir / "cache.db"
        cache_file.write_bytes(b"sqlite")
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY", str(proxy_dir))

        with pytest.raises(ValueError, match="credential store"):
            validate_file_path(str(cache_file))

    def test_sibling_of_proxy_directory_still_allowed(self, tmp_path, monkeypatch):
        from core.utils import validate_file_path

        proxy_dir = tmp_path / "oauth-proxy"
        proxy_dir.mkdir()
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY", str(proxy_dir))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ordinary = tmp_path / "oauth-proxy-notes.txt"
        ordinary.write_text("fine")

        assert validate_file_path(str(ordinary)) == ordinary.resolve()


# ---------------------------------------------------------------------------
# 6. Middleware: domain-policy rejection is terminal
# ---------------------------------------------------------------------------


def _middleware_context(session_id: str = "sess-1"):
    ctx = MagicMock()
    fastmcp_context = MagicMock()
    fastmcp_context.session_id = session_id
    fastmcp_context.set_state = AsyncMock()
    fastmcp_context.get_state = AsyncMock(return_value=None)
    ctx.fastmcp_context = fastmcp_context
    return ctx


class TestDomainPolicyRejectionTerminal:
    @pytest.fixture
    def bound_store(self):
        store = MagicMock()
        store.get_user_by_mcp_session.return_value = "bound@otbgroup.co.uk"
        store.has_session.return_value = False
        store.get_single_user_email.return_value = None
        return store

    @pytest.mark.asyncio
    async def test_rejected_token_does_not_fall_through(
        self, monkeypatch, bound_store, caplog
    ):
        from auth.auth_info_middleware import AuthInfoMiddleware

        monkeypatch.setenv("OAUTH_ALLOWED_EMAIL_DOMAINS", "otbgroup.co.uk")
        token = MagicMock()
        token.email = "attacker@evil.example"
        token.claims = {"email": "attacker@evil.example", "hd": "evil.example"}

        ctx = _middleware_context()
        headers_probe = MagicMock(return_value={})
        with (
            patch("auth.auth_info_middleware.get_access_token", return_value=token),
            patch("auth.auth_info_middleware.get_http_headers", headers_probe),
            patch(
                "auth.oauth21_session_store.get_oauth21_session_store",
                return_value=bound_store,
            ),
            patch("core.config.get_transport_mode", return_value="streamable-http"),
            caplog.at_level(logging.WARNING, logger="auth.auth_info_middleware"),
        ):
            await AuthInfoMiddleware()._process_request_for_auth(ctx)

        set_keys = [c.args[0] for c in ctx.fastmcp_context.set_state.await_args_list]
        assert "authenticated_user_email" not in set_keys
        assert "authenticated_via" not in set_keys
        assert not headers_probe.called
        assert not bound_store.get_user_by_mcp_session.called
        assert any(
            "Rejecting FastMCP-validated token" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_no_token_still_reaches_session_binding(
        self, monkeypatch, bound_store
    ):
        from auth.auth_info_middleware import AuthInfoMiddleware

        monkeypatch.setenv("OAUTH_ALLOWED_EMAIL_DOMAINS", "otbgroup.co.uk")
        ctx = _middleware_context()
        with (
            patch("auth.auth_info_middleware.get_access_token", return_value=None),
            patch("auth.auth_info_middleware.get_http_headers", return_value={}),
            patch(
                "auth.oauth21_session_store.get_oauth21_session_store",
                return_value=bound_store,
            ),
            patch("core.config.get_transport_mode", return_value="streamable-http"),
        ):
            await AuthInfoMiddleware()._process_request_for_auth(ctx)

        calls = {
            c.args[0]: c.args[1] for c in ctx.fastmcp_context.set_state.await_args_list
        }
        assert calls["authenticated_user_email"] == "bound@otbgroup.co.uk"
        assert calls["authenticated_via"] == "mcp_session_binding"

    @pytest.mark.asyncio
    async def test_accepted_token_sets_identity(self, monkeypatch, bound_store):
        from auth.auth_info_middleware import AuthInfoMiddleware

        monkeypatch.setenv("OAUTH_ALLOWED_EMAIL_DOMAINS", "otbgroup.co.uk")
        token = MagicMock()
        token.email = "oliver@otbgroup.co.uk"
        token.claims = {"email": "oliver@otbgroup.co.uk", "hd": "otbgroup.co.uk"}
        ctx = _middleware_context()
        with (
            patch("auth.auth_info_middleware.get_access_token", return_value=token),
            patch("auth.auth_info_middleware.get_http_headers", return_value={}),
            patch(
                "auth.oauth21_session_store.get_oauth21_session_store",
                return_value=bound_store,
            ),
        ):
            await AuthInfoMiddleware()._process_request_for_auth(ctx)

        calls = {
            c.args[0]: c.args[1] for c in ctx.fastmcp_context.set_state.await_args_list
        }
        assert calls["authenticated_user_email"] == "oliver@otbgroup.co.uk"
        assert calls["authenticated_via"] == "fastmcp_oauth"


# ---------------------------------------------------------------------------
# 7. _ensure_audit_started retries after a failed start
# ---------------------------------------------------------------------------


class TestEnsureAuditStartedRetry:
    @pytest.mark.asyncio
    async def test_flag_reset_on_failure_then_retry_succeeds(self, monkeypatch, caplog):
        import core.server as core_server

        attempts = []

        class _Logger:
            async def start(self):
                attempts.append(1)
                if len(attempts) == 1:
                    raise RuntimeError("sheets unreachable")

        fake = _Logger()
        monkeypatch.setattr(core_server, "audit_logger", lambda: fake)
        monkeypatch.setattr(core_server, "_audit_started", False)

        with caplog.at_level(logging.WARNING, logger="core.server"):
            await core_server._ensure_audit_started()
        assert core_server._audit_started is False
        assert any(
            r.levelno == logging.WARNING
            and "retry on the next tool call" in r.getMessage()
            for r in caplog.records
        )

        await core_server._ensure_audit_started()
        assert core_server._audit_started is True
        assert len(attempts) == 2

        # Idempotent once started.
        await core_server._ensure_audit_started()
        assert len(attempts) == 2
        monkeypatch.setattr(core_server, "_audit_started", False)

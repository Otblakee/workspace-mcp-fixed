"""Regression tests for the security hardening batch.

Covers: upload_uri host validation (token exfiltration), import_to_google_doc
remote-filesystem gate, MCP credential-store blocklist in validate_file_path,
OAuth 2.1 empty-scope denial, Gmail header-injection stripping, OAuth callback
HTML escaping, per-user transfer-folder cache, and the appscript opt-in gate.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _make_service_with_token(token: str = "ya29.fake-token") -> MagicMock:
    service = MagicMock()
    creds = MagicMock()
    creds.token = token
    creds.expired = False
    creds.refresh_token = "refresh-token"
    service._http.credentials = creds
    return service


# ---------------------------------------------------------------------------
# S1: confirm_drive_upload must refuse to send the OAuth token off-Google
# ---------------------------------------------------------------------------


class TestConfirmUploadUriValidation:
    @pytest.mark.asyncio
    async def test_non_google_host_rejected_without_any_request(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.confirm_drive_upload)
        service = _make_service_with_token()

        async def must_not_be_called(self, url, headers=None):  # pragma: no cover
            raise AssertionError("HTTP request must not be sent to a non-Google host")

        monkeypatch.setattr(httpx.AsyncClient, "put", must_not_be_called)

        for bad_uri in (
            "https://attacker.example.com/collect",
            "http://www.googleapis.com/upload",  # https required
            "https://evilgoogleapis.com/upload",  # suffix spoof
            "https://googleapis.com.evil.net/upload",
        ):
            with pytest.raises(Exception, match="googleapis.com"):
                await impl(
                    service=service,
                    user_google_email="u@example.com",
                    file_id="f1",
                    upload_uri=bad_uri,
                )

    @pytest.mark.asyncio
    async def test_google_issued_uri_still_works(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.confirm_drive_upload)
        service = _make_service_with_token()

        async def fake_put(self, url, headers=None):
            return httpx.Response(
                status_code=308,
                headers={"Range": "bytes=0-499"},
                request=httpx.Request("PUT", url),
            )

        monkeypatch.setattr(httpx.AsyncClient, "put", fake_put)

        result = await impl(
            service=service,
            user_google_email="u@example.com",
            file_id="f1",
            upload_uri="https://www.googleapis.com/upload/drive/v3/files?upload_id=ABC",
        )
        assert "Upload status: incomplete" in result


# ---------------------------------------------------------------------------
# S2: import_to_google_doc must not read the server filesystem in HTTP mode
# ---------------------------------------------------------------------------


class TestImportToGoogleDocRemoteGate:
    @pytest.mark.asyncio
    async def test_file_path_rejected_in_streamable_http(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.import_to_google_doc)
        service = MagicMock()
        monkeypatch.setattr(drive_tools, "get_transport_mode", lambda: "streamable-http")

        async def fake_resolve(svc, fid):
            return "root"

        monkeypatch.setattr(drive_tools, "resolve_folder_id", fake_resolve)

        with pytest.raises(Exception, match="not supported for remote MCP clients"):
            await impl(
                service=service,
                user_google_email="u@example.com",
                file_name="report",
                file_path="/home/user/.google_workspace_mcp/credentials/u.json",
            )
        service.files.assert_not_called()

    @pytest.mark.asyncio
    async def test_content_path_unaffected_in_streamable_http(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.import_to_google_doc)
        service = MagicMock()
        service.files.return_value.create.return_value.execute = MagicMock(
            return_value={
                "id": "doc-1",
                "name": "report",
                "webViewLink": "https://docs.google.com/document/d/doc-1",
            }
        )
        monkeypatch.setattr(drive_tools, "get_transport_mode", lambda: "streamable-http")

        async def fake_resolve(svc, fid):
            return "root"

        monkeypatch.setattr(drive_tools, "resolve_folder_id", fake_resolve)

        result = await impl(
            service=service,
            user_google_email="u@example.com",
            file_name="report",
            content="# Hello",
        )
        assert "doc-1" in result


# ---------------------------------------------------------------------------
# validate_file_path: MCP credential store is always blocked
# ---------------------------------------------------------------------------


class TestCredentialStoreBlocklist:
    def test_env_configured_credentials_dir_blocked(self, tmp_path, monkeypatch):
        from core.utils import validate_file_path

        cred_dir = tmp_path / "creds"
        cred_dir.mkdir()
        token_file = cred_dir / "oliver@otbgroup.co.uk.json"
        token_file.write_text('{"refresh_token": "secret"}')
        monkeypatch.setenv("WORKSPACE_MCP_CREDENTIALS_DIR", str(cred_dir))

        with pytest.raises(ValueError, match="credential store"):
            validate_file_path(str(token_file))

    def test_default_credentials_dir_blocked(self, tmp_path, monkeypatch):
        from core.utils import validate_file_path

        monkeypatch.delenv("WORKSPACE_MCP_CREDENTIALS_DIR", raising=False)
        monkeypatch.delenv("GOOGLE_MCP_CREDENTIALS_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        cred_dir = tmp_path / ".google_workspace_mcp" / "credentials"
        cred_dir.mkdir(parents=True)
        token_file = cred_dir / "user.json"
        token_file.write_text("{}")

        with pytest.raises(ValueError, match="credential store"):
            validate_file_path(str(token_file))

    def test_ordinary_file_still_allowed(self, tmp_path, monkeypatch):
        from core.utils import validate_file_path

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ordinary = tmp_path / "notes.txt"
        ordinary.write_text("hello")

        assert validate_file_path(str(ordinary)) == ordinary.resolve()


# ---------------------------------------------------------------------------
# S3: OAuth 2.1 credentials with no recorded scopes must be denied
# ---------------------------------------------------------------------------


class TestOAuth21ScopeGate:
    @pytest.mark.asyncio
    async def test_empty_scopes_denied_not_substituted(self, monkeypatch):
        from auth import service_decorator
        from auth.google_auth import GoogleAuthenticationError

        monkeypatch.setattr(service_decorator, "get_auth_provider", lambda: None)
        monkeypatch.setattr(service_decorator, "get_access_token", lambda: None)

        credentials = MagicMock()
        credentials.scopes = None

        store = MagicMock()
        store.get_credentials_with_validation.return_value = credentials
        monkeypatch.setattr(
            service_decorator, "get_oauth21_session_store", lambda: store
        )

        built = MagicMock()
        monkeypatch.setattr(
            service_decorator, "build", lambda *a, **kw: built
        )

        with pytest.raises(GoogleAuthenticationError, match="no recorded scopes"):
            await service_decorator.get_authenticated_google_service_oauth21(
                service_name="drive",
                version="v3",
                tool_name="test_tool",
                user_google_email="u@example.com",
                required_scopes=["https://www.googleapis.com/auth/drive.file"],
            )

    @pytest.mark.asyncio
    async def test_sufficient_scopes_still_pass(self, monkeypatch):
        from auth import service_decorator

        monkeypatch.setattr(service_decorator, "get_auth_provider", lambda: None)
        monkeypatch.setattr(service_decorator, "get_access_token", lambda: None)

        credentials = MagicMock()
        credentials.scopes = ["https://www.googleapis.com/auth/drive.file"]

        store = MagicMock()
        store.get_credentials_with_validation.return_value = credentials
        monkeypatch.setattr(
            service_decorator, "get_oauth21_session_store", lambda: store
        )

        built = MagicMock()
        monkeypatch.setattr(service_decorator, "build", lambda *a, **kw: built)

        service, email = await service_decorator.get_authenticated_google_service_oauth21(
            service_name="drive",
            version="v3",
            tool_name="test_tool",
            user_google_email="u@example.com",
            required_scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        assert service is built
        assert email == "u@example.com"


# ---------------------------------------------------------------------------
# S4: Gmail header injection
# ---------------------------------------------------------------------------


class TestGmailHeaderInjection:
    def _roundtrip(self, **kwargs):
        import email as email_lib

        from gmail.gmail_tools import _prepare_gmail_message

        raw, _thread = _prepare_gmail_message(**kwargs)
        return email_lib.message_from_bytes(base64.urlsafe_b64decode(raw))

    def test_subject_crlf_cannot_smuggle_bcc(self):
        msg = self._roundtrip(
            subject="Quarterly report\r\nBcc: attacker@evil.com",
            body="See attached.",
            to="legit@example.com",
        )
        assert msg.get_all("Bcc") is None
        # The payload text survives, inert, inside the Subject value.
        assert "attacker@evil.com" in msg["Subject"]

    def test_recipient_fields_stripped(self):
        msg = self._roundtrip(
            subject="Hi",
            body="b",
            to="a@example.com\r\nBcc: hidden@evil.com",
            cc="c@example.com\nReply-To: mitm@evil.com",
        )
        assert msg.get_all("Bcc") is None
        assert msg.get_all("Reply-To") is None

    def test_threading_headers_stripped(self):
        msg = self._roundtrip(
            subject="Hi",
            body="b",
            to="a@example.com",
            in_reply_to="<x@mail>\r\nBcc: hidden@evil.com",
            references="<x@mail>\nX-Evil: 1",
        )
        assert msg.get_all("Bcc") is None
        assert msg.get_all("X-Evil") is None


# ---------------------------------------------------------------------------
# S5: OAuth callback pages must escape reflected input
# ---------------------------------------------------------------------------


class TestOAuthCallbackEscaping:
    PAYLOAD = '<script>alert("xss")</script>'

    def test_error_response_escapes(self):
        from auth.oauth_responses import create_error_response

        body = create_error_response(self.PAYLOAD).body.decode()
        assert self.PAYLOAD not in body
        assert "&lt;script&gt;" in body

    def test_server_error_response_escapes(self):
        from auth.oauth_responses import create_server_error_response

        body = create_server_error_response(self.PAYLOAD).body.decode()
        assert self.PAYLOAD not in body
        assert "&lt;script&gt;" in body

    def test_success_response_escapes_user_id(self):
        from auth.oauth_responses import create_success_response

        body = create_success_response('<img src=x onerror=alert(1)>').body.decode()
        assert "<img src=x" not in body
        assert "&lt;img" in body


# ---------------------------------------------------------------------------
# S7: appscript must be opt-in, never part of the default tool load
# ---------------------------------------------------------------------------


class TestAppscriptOptIn:
    def test_opt_in_set_contains_appscript_and_gadmin(self):
        import main

        assert "appscript" in main.OPT_IN_TOOLS
        assert "gadmin" in main.OPT_IN_TOOLS

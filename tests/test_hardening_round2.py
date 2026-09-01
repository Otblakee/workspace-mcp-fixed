"""Second hardening round (multi-user review): debug-log file controls,
audit error-column scrubbing, and the remote-client gate on Gmail
``attachments[].path``."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestFileLogging:
    def _fresh_logger(self, name):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        return lg

    def test_env_opt_out_disables_file_log(self, monkeypatch):
        from core.log_formatter import FILE_LOGGING_ENV, configure_file_logging

        monkeypatch.delenv("WORKSPACE_MCP_STATELESS_MODE", raising=False)
        monkeypatch.setenv(FILE_LOGGING_ENV, "false")
        lg = self._fresh_logger("test.filelog.off")
        assert configure_file_logging("test.filelog.off") is False
        assert lg.handlers == []

    def test_default_file_log_rotates(self, monkeypatch, tmp_path):
        from core import log_formatter

        monkeypatch.delenv("WORKSPACE_MCP_STATELESS_MODE", raising=False)
        monkeypatch.delenv(log_formatter.FILE_LOGGING_ENV, raising=False)
        # Point the log at a temp dir by faking the module location.
        monkeypatch.setattr(
            log_formatter, "__file__", str(tmp_path / "core" / "log_formatter.py")
        )
        (tmp_path / "core").mkdir()
        lg = self._fresh_logger("test.filelog.on")
        try:
            configured = log_formatter.configure_file_logging("test.filelog.on")
            assert configured is True
            handlers = [h for h in lg.handlers if isinstance(h, RotatingFileHandler)]
            assert len(handlers) == 1
            assert handlers[0].maxBytes == log_formatter.FILE_LOG_MAX_BYTES
            assert handlers[0].backupCount == log_formatter.FILE_LOG_BACKUP_COUNT
            assert Path(handlers[0].baseFilename).name == "mcp_server_debug.log"
        finally:
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)

    def test_render_blueprint_disables_file_log(self):
        import yaml

        blueprint = yaml.safe_load((REPO_ROOT / "render.yaml").read_text())
        env = {e["key"]: e.get("value") for e in blueprint["services"][0]["envVars"]}
        assert env.get("WORKSPACE_MCP_FILE_LOGGING") == "false"


class TestAuditErrorScrub:
    def test_query_string_removed_from_urls(self):
        from core.audit import _scrub_error_text

        text = (
            "<HttpError 400 when requesting "
            "https://www.googleapis.com/drive/v3/files?q=name+contains+%27payroll%27&fields=x "
            'returned "Invalid Value". Details: "[...]">'
        )
        out = _scrub_error_text(text)
        assert "payroll" not in out
        assert "q=" not in out
        assert "https://www.googleapis.com/drive/v3/files?<redacted-query>" in out
        assert 'returned "Invalid Value"' in out

    def test_text_without_urls_unchanged(self):
        from core.audit import _scrub_error_text

        assert _scrub_error_text("plain failure, no url") == "plain failure, no url"
        assert _scrub_error_text("") == ""

    @pytest.mark.asyncio
    async def test_audit_row_error_column_is_scrubbed(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from core import audit

        fake = MagicMock()
        monkeypatch.setattr(audit, "logger", lambda: fake)
        monkeypatch.setattr(audit, "ENABLED", True)
        monkeypatch.setattr(audit, "_resolve_user_email", AsyncMock(return_value="u@x"))

        @audit.audit_log()
        async def boom(**kwargs):
            raise Exception(
                "API error in search: <HttpError 403 when requesting "
                "https://gmail.googleapis.com/gmail/v1/users/me/messages?q=from%3Aboss%20secret "
                'returned "forbidden">'
            )

        with pytest.raises(Exception):
            await boom(query="from:boss secret")
        row = fake.submit.call_args.args[0]
        assert "secret" not in row["error"]
        assert "<redacted-query>" in row["error"]
        assert "from:boss" not in row["params_summary"]  # query is SENSITIVE


class TestGmailPathAttachmentGate:
    def test_path_rejected_over_streamable_http(self, monkeypatch, tmp_path):
        import core.config as cfg
        from gmail.gmail_tools import _prepare_gmail_message

        f = tmp_path / "note.txt"
        f.write_text("hello")
        monkeypatch.setattr(cfg, "get_transport_mode", lambda: "streamable-http")
        with pytest.raises(Exception, match="not supported for remote MCP clients"):
            _prepare_gmail_message(
                subject="s",
                body="b",
                to="a@example.com",
                attachments=[{"path": str(f)}],
            )

    def test_path_allowed_over_stdio(self, monkeypatch, tmp_path):
        import base64
        import email as email_lib

        import core.config as cfg
        from gmail.gmail_tools import _prepare_gmail_message

        f = tmp_path / "note.txt"
        f.write_text("hello")
        monkeypatch.setattr(cfg, "get_transport_mode", lambda: "stdio")
        monkeypatch.setenv("ALLOWED_FILE_DIRS", str(tmp_path))
        raw, _ = _prepare_gmail_message(
            subject="s", body="b", to="a@example.com", attachments=[{"path": str(f)}]
        )
        msg = email_lib.message_from_bytes(base64.urlsafe_b64decode(raw))
        names = [part.get_filename() for part in msg.walk() if part.get_filename()]
        assert names == ["note.txt"]

    def test_base64_content_still_works_over_http(self, monkeypatch):
        import base64
        import email as email_lib

        import core.config as cfg
        from gmail.gmail_tools import _prepare_gmail_message

        monkeypatch.setattr(cfg, "get_transport_mode", lambda: "streamable-http")
        raw, _ = _prepare_gmail_message(
            subject="s",
            body="b",
            to="a@example.com",
            attachments=[
                {
                    "filename": "r.txt",
                    "content": base64.b64encode(b"hi").decode(),
                    "mime_type": "text/plain",
                }
            ],
        )
        msg = email_lib.message_from_bytes(base64.urlsafe_b64decode(raw))
        names = [part.get_filename() for part in msg.walk() if part.get_filename()]
        assert names == ["r.txt"]

"""Audit writer via a dedicated service account (multi-user mode).

With AUDIT_SA_JSON_FILE / AUDIT_SA_JSON_B64 set, every row is appended by
one service-account client, users need no access to the audit Sheet, and
rows attributed to DEFAULT_USER are written rather than dropped. Without it,
the per-user path is unchanged (covered by tests/test_multi_user_security.py).
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import audit  # noqa: E402
from core.service_account import (  # noqa: E402
    ServiceAccountConfigError,
    load_service_account_info,
    service_account_email,
)

SA_KEY = {"type": "service_account", "client_email": "audit-writer@x.iam"}


class TestSharedLoader:
    def test_none_when_unset(self):
        assert load_service_account_info("F", "B", {}) is None

    def test_file_wins_over_b64(self, tmp_path):
        f = tmp_path / "sa.json"
        f.write_text(json.dumps(SA_KEY))
        env = {
            "F": str(f),
            "B": base64.b64encode(
                b'{"type":"service_account","client_email":"other"}'
            ).decode(),
        }
        assert (
            load_service_account_info("F", "B", env)["client_email"]
            == "audit-writer@x.iam"
        )

    def test_b64(self):
        env = {"B": base64.b64encode(json.dumps(SA_KEY).encode()).decode()}
        assert (
            service_account_email(load_service_account_info("F", "B", env))
            == "audit-writer@x.iam"
        )

    @pytest.mark.parametrize(
        "env",
        [
            {"F": "/nonexistent/sa.json"},
            {"B": "%%%not-base64%%%"},
            {"B": base64.b64encode(b"{nope").decode()},
            {"B": base64.b64encode(b'{"type": "authorized_user"}').decode()},
            {"B": base64.b64encode(b"[]").decode()},
        ],
    )
    def test_invalid_raises(self, env):
        with pytest.raises(ServiceAccountConfigError):
            load_service_account_info("F", "B", env)


def _fake_sheets():
    sheets = MagicMock()
    sheets.spreadsheets.return_value.values.return_value.append.return_value.execute = (
        MagicMock(return_value={})
    )
    return sheets


class TestServiceAccountFlush:
    @pytest.mark.asyncio
    async def test_one_client_writes_every_row_including_default_user(
        self, monkeypatch
    ):
        monkeypatch.setattr(audit, "AUDIT_SHEET_ID", "sheet-test")
        monkeypatch.setenv(
            audit.AUDIT_SA_JSON_B64_ENV,
            base64.b64encode(json.dumps(SA_KEY).encode()).decode(),
        )
        monkeypatch.delenv(audit.AUDIT_SA_JSON_FILE_ENV, raising=False)

        sheets = _fake_sheets()
        built = []

        def fake_build(name, version, credentials=None, **kw):
            built.append((name, version, credentials))
            return sheets

        from google.oauth2 import service_account

        monkeypatch.setattr(
            service_account.Credentials,
            "from_service_account_info",
            lambda info, scopes=None: ("sa-creds", info["client_email"], tuple(scopes)),
        )
        monkeypatch.setattr(audit, "build", fake_build)
        per_user_calls = []
        monkeypatch.setattr(
            audit.AuditLogger,
            "_build_sheets_for_user",
            lambda self, email: per_user_calls.append(email),
        )
        monkeypatch.setattr(audit.AuditLogger, "_ensure_tab", lambda self, s, tab: None)

        logger = audit.AuditLogger()
        batch = [
            {"user": "alice@otb.co.uk", "tool": "x"},
            {"user": audit.DEFAULT_USER, "tool": "y"},
            {"user": "", "tool": "z"},
        ]
        unwritten = await logger._flush(batch)

        assert unwritten == []
        assert per_user_calls == []
        assert len(built) == 1
        assert built[0][2] == ("sa-creds", "audit-writer@x.iam", (audit.SHEETS_SCOPE,))
        body = sheets.spreadsheets.return_value.values.return_value.append.call_args.kwargs[
            "body"
        ]
        users = [row[audit.HEADERS.index("user")] for row in body["values"]]
        assert users == ["alice@otb.co.uk", audit.DEFAULT_USER, audit.DEFAULT_USER]
        sheets.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_append_failure_returns_whole_batch_unwritten(self, monkeypatch):
        monkeypatch.setattr(audit, "AUDIT_SHEET_ID", "sheet-test")
        sheets = _fake_sheets()
        sheets.spreadsheets.return_value.values.return_value.append.return_value.execute.side_effect = RuntimeError(
            "quota"
        )
        logger = audit.AuditLogger()
        monkeypatch.setattr(logger, "_build_sheets_for_service_account", lambda: sheets)
        monkeypatch.setattr(audit.AuditLogger, "_ensure_tab", lambda self, s, tab: None)
        batch = [
            {"user": "a@otb.co.uk", "tool": "x"},
            {"user": "b@otb.co.uk", "tool": "y"},
        ]
        assert await logger._flush(batch) == batch

    @pytest.mark.asyncio
    async def test_invalid_key_falls_back_to_per_user(self, monkeypatch, caplog):
        monkeypatch.setattr(audit, "AUDIT_SHEET_ID", "sheet-test")
        monkeypatch.setenv(
            audit.AUDIT_SA_JSON_B64_ENV, base64.b64encode(b'{"type":"user"}').decode()
        )
        monkeypatch.delenv(audit.AUDIT_SA_JSON_FILE_ENV, raising=False)
        seen = []

        def fake_per_user(self, email):
            seen.append(email)
            return _fake_sheets()

        monkeypatch.setattr(audit.AuditLogger, "_build_sheets_for_user", fake_per_user)
        monkeypatch.setattr(audit.AuditLogger, "_ensure_tab", lambda self, s, tab: None)
        logger = audit.AuditLogger()
        with caplog.at_level("ERROR", logger="core.audit"):
            unwritten = await logger._flush([{"user": "a@otb.co.uk", "tool": "x"}])
        assert unwritten == []
        assert seen == ["a@otb.co.uk"]
        assert any("service-account key invalid" in r.message for r in caplog.records)

    def test_unconfigured_means_none(self, monkeypatch):
        monkeypatch.delenv(audit.AUDIT_SA_JSON_B64_ENV, raising=False)
        monkeypatch.delenv(audit.AUDIT_SA_JSON_FILE_ENV, raising=False)
        assert audit.AuditLogger()._build_sheets_for_service_account() is None

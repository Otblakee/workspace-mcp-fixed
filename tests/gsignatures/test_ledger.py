"""Unit tests for the signature ledger (Google Sheet) helpers.

Covers:

* ``ledger_sheet_id`` fails with guidance when the env var is unset;
* ``ensure_tab`` creates the tab and header only when the tab is missing,
  fills the header into an existing empty tab, and refuses a tab whose
  header does not match;
* ``append_ledger_rows`` writes values in ``LEDGER_HEADER`` order with
  ``valueInputOption=RAW`` and refuses unknown or missing keys;
* ``read_ledger_latest`` keeps the latest row per (user, send-as) pair,
  tolerates short rows and an empty sheet, and refuses a foreign header;
* ``write_audit_report`` ensures the tab, clears it, then writes header +
  rows.

The fake exposes the googleapiclient shape
(``service.spreadsheets().values().append(...).execute()``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gsignatures import ledger  # noqa: E402

SHEET_ID = "sheet-123"


def _request(result):
    request = MagicMock()
    if isinstance(result, Exception):
        request.execute.side_effect = result
    else:
        request.execute.return_value = result
    return request


def _tab_of(a1_range: str) -> str:
    """Return the tab name from an A1 range such as ``'Ledger'!A:L``."""
    name = a1_range.split("!")[0]
    if name.startswith("'") and name.endswith("'"):
        name = name[1:-1].replace("''", "'")
    return name


class FakeSheets:
    """Sheets double: ``tabs`` maps a tab title to its rows."""

    def __init__(self, tabs=None):
        self.calls = []
        self.tabs = {k: [list(r) for r in v] for k, v in (tabs or {}).items()}

    def spreadsheets(self):
        parent = self

        class _Values:
            def get(self, **kwargs):
                parent.calls.append(("values.get", kwargs))
                tab = _tab_of(kwargs["range"])
                rows = parent.tabs.get(tab, [])
                body = {"range": kwargs["range"]}
                if rows:
                    body["values"] = [list(r) for r in rows]
                return _request(body)

            def append(self, **kwargs):
                parent.calls.append(("values.append", kwargs))
                tab = _tab_of(kwargs["range"])
                parent.tabs.setdefault(tab, []).extend(kwargs["body"]["values"])
                return _request(
                    {"updates": {"updatedRows": len(kwargs["body"]["values"])}}
                )

            def update(self, **kwargs):
                parent.calls.append(("values.update", kwargs))
                tab = _tab_of(kwargs["range"])
                values = kwargs["body"]["values"]
                existing = parent.tabs.setdefault(tab, [])
                for i, row in enumerate(values):
                    if i < len(existing):
                        existing[i] = list(row)
                    else:
                        existing.append(list(row))
                return _request({"updatedRows": len(values)})

            def clear(self, **kwargs):
                parent.calls.append(("values.clear", kwargs))
                parent.tabs[_tab_of(kwargs["range"])] = []
                return _request({})

        class _Spreadsheets:
            def get(self, **kwargs):
                parent.calls.append(("spreadsheets.get", kwargs))
                return _request(
                    {
                        "sheets": [
                            {"properties": {"title": title}} for title in parent.tabs
                        ]
                    }
                )

            def batchUpdate(self, **kwargs):
                parent.calls.append(("spreadsheets.batchUpdate", kwargs))
                for req in kwargs["body"]["requests"]:
                    title = req["addSheet"]["properties"]["title"]
                    parent.tabs.setdefault(title, [])
                return _request(
                    {"replies": [{"addSheet": {"properties": {"sheetId": 7}}}]}
                )

            def values(self):
                return _Values()

        return _Spreadsheets()

    def names(self):
        return [c[0] for c in self.calls]


def _row(**overrides):
    base = {
        "applied_at": "2026-09-25T10:00:00Z",
        "actor": "oliver@otbgroup.co.uk",
        "user_email": "alice@otbgroup.co.uk",
        "send_as_email": "alice@otbgroup.co.uk",
        "entity": "OTB",
        "template_version": "1.0.0",
        "statutory_version": "1.0.0",
        "rendered_hash": "r1",
        "readback_hash": "b1",
        "previous_hash": "",
        "previous_signature_html": "<div>old</div>",
        "run_id": "run-1",
    }
    base.update(overrides)
    return base


class TestConstants:
    def test_headers_are_the_documented_ones(self):
        assert ledger.LEDGER_TAB == "Ledger"
        assert ledger.LEDGER_HEADER == [
            "applied_at",
            "actor",
            "user_email",
            "send_as_email",
            "entity",
            "template_version",
            "statutory_version",
            "rendered_hash",
            "readback_hash",
            "previous_hash",
            "previous_signature_html",
            "run_id",
        ]
        assert ledger.AUDIT_HEADER == [
            "audited_at",
            "user_email",
            "send_as_email",
            "entity",
            "expected_template_version",
            "expected_statutory_version",
            "ledger_template_version",
            "ledger_statutory_version",
            "status",
            "reason",
            "current_hash",
        ]


class TestLedgerSheetId:
    def test_unset_raises_with_guidance(self, monkeypatch):
        monkeypatch.delenv("SIGNATURE_LEDGER_SHEET_ID", raising=False)
        with pytest.raises(ledger.LedgerError) as excinfo:
            ledger.ledger_sheet_id()
        assert "SIGNATURE_LEDGER_SHEET_ID" in str(excinfo.value)

    def test_blank_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("SIGNATURE_LEDGER_SHEET_ID", "  ")
        with pytest.raises(ledger.LedgerError):
            ledger.ledger_sheet_id()

    def test_set_is_stripped(self, monkeypatch):
        monkeypatch.setenv("SIGNATURE_LEDGER_SHEET_ID", " abc123 ")
        assert ledger.ledger_sheet_id() == "abc123"


class TestEnsureTab:
    @pytest.mark.asyncio
    async def test_creates_tab_and_header_when_missing(self):
        sheets = FakeSheets({"Other": [["x"]]})
        await ledger.ensure_tab(sheets, SHEET_ID, "Ledger", ledger.LEDGER_HEADER)
        assert sheets.names() == [
            "spreadsheets.get",
            "spreadsheets.batchUpdate",
            "values.update",
        ]
        add = sheets.calls[1][1]
        assert add["spreadsheetId"] == SHEET_ID
        assert add["body"] == {
            "requests": [{"addSheet": {"properties": {"title": "Ledger"}}}]
        }
        update = sheets.calls[2][1]
        assert update["valueInputOption"] == "RAW"
        assert _tab_of(update["range"]) == "Ledger"
        assert update["body"] == {"values": [ledger.LEDGER_HEADER]}
        assert sheets.tabs["Ledger"] == [ledger.LEDGER_HEADER]

    @pytest.mark.asyncio
    async def test_existing_tab_with_matching_header_is_left_alone(self):
        sheets = FakeSheets({"Ledger": [ledger.LEDGER_HEADER, _row_values()]})
        await ledger.ensure_tab(sheets, SHEET_ID, "Ledger", ledger.LEDGER_HEADER)
        assert "spreadsheets.batchUpdate" not in sheets.names()
        assert "values.update" not in sheets.names()
        assert sheets.tabs["Ledger"] == [ledger.LEDGER_HEADER, _row_values()]

    @pytest.mark.asyncio
    async def test_existing_empty_tab_gets_the_header(self):
        sheets = FakeSheets({"Ledger": []})
        await ledger.ensure_tab(sheets, SHEET_ID, "Ledger", ledger.LEDGER_HEADER)
        assert "spreadsheets.batchUpdate" not in sheets.names()
        assert sheets.tabs["Ledger"] == [ledger.LEDGER_HEADER]

    @pytest.mark.asyncio
    async def test_existing_tab_with_foreign_header_is_refused(self):
        sheets = FakeSheets({"Ledger": [["something", "else"]]})
        with pytest.raises(ledger.LedgerError):
            await ledger.ensure_tab(sheets, SHEET_ID, "Ledger", ledger.LEDGER_HEADER)
        assert "values.update" not in sheets.names()

    @pytest.mark.asyncio
    async def test_blank_tab_or_header_is_refused_before_any_call(self):
        sheets = FakeSheets()
        with pytest.raises(ledger.LedgerError):
            await ledger.ensure_tab(sheets, SHEET_ID, "", ledger.LEDGER_HEADER)
        with pytest.raises(ledger.LedgerError):
            await ledger.ensure_tab(sheets, SHEET_ID, "Ledger", [])
        with pytest.raises(ledger.LedgerError):
            await ledger.ensure_tab(sheets, "", "Ledger", ledger.LEDGER_HEADER)
        assert sheets.calls == []


def _row_values(**overrides):
    row = _row(**overrides)
    return [row[key] for key in ledger.LEDGER_HEADER]


class TestAppendLedgerRows:
    @pytest.mark.asyncio
    async def test_writes_rows_in_header_order_with_raw(self):
        sheets = FakeSheets({"Ledger": [ledger.LEDGER_HEADER]})
        # Keys deliberately out of header order, one missing (previous_hash).
        row = {
            "run_id": "run-9",
            "readback_hash": "b9",
            "rendered_hash": "r9",
            "send_as_email": "alice@jit-logistics.com",
            "user_email": "alice@otbgroup.co.uk",
            "applied_at": "2026-09-25T11:00:00Z",
            "actor": "oliver@otbgroup.co.uk",
            "entity": "JIT",
            "template_version": "1.0.0",
            "statutory_version": "1.0.0",
            "previous_signature_html": None,
        }
        written = await ledger.append_ledger_rows(sheets, SHEET_ID, [row])
        assert written == 1
        assert sheets.names() == ["values.append"]
        params = sheets.calls[0][1]
        assert params["spreadsheetId"] == SHEET_ID
        assert _tab_of(params["range"]) == "Ledger"
        assert params["valueInputOption"] == "RAW"
        assert params["body"] == {
            "values": [
                [
                    "2026-09-25T11:00:00Z",
                    "oliver@otbgroup.co.uk",
                    "alice@otbgroup.co.uk",
                    "alice@jit-logistics.com",
                    "JIT",
                    "1.0.0",
                    "1.0.0",
                    "r9",
                    "b9",
                    "",
                    "",
                    "run-9",
                ]
            ]
        }

    @pytest.mark.asyncio
    async def test_empty_list_writes_nothing(self):
        sheets = FakeSheets()
        assert await ledger.append_ledger_rows(sheets, SHEET_ID, []) == 0
        assert sheets.calls == []

    @pytest.mark.asyncio
    async def test_unknown_key_is_refused(self):
        sheets = FakeSheets()
        with pytest.raises(ledger.LedgerError) as excinfo:
            await ledger.append_ledger_rows(sheets, SHEET_ID, [_row(surprise="x")])
        assert "surprise" in str(excinfo.value)
        assert sheets.calls == []

    @pytest.mark.asyncio
    async def test_missing_identity_fields_are_refused(self):
        sheets = FakeSheets()
        with pytest.raises(ledger.LedgerError) as excinfo:
            await ledger.append_ledger_rows(sheets, SHEET_ID, [_row(user_email="")])
        assert "user_email" in str(excinfo.value)
        with pytest.raises(ledger.LedgerError):
            await ledger.append_ledger_rows(sheets, SHEET_ID, [_row(applied_at=None)])
        assert sheets.calls == []

    @pytest.mark.asyncio
    async def test_multiple_rows_written_in_one_call(self):
        sheets = FakeSheets()
        rows = [
            _row(run_id="a"),
            _row(run_id="b", send_as_email="alice@jit-logistics.com"),
        ]
        assert await ledger.append_ledger_rows(sheets, SHEET_ID, rows) == 2
        assert len(sheets.calls) == 1
        assert len(sheets.calls[0][1]["body"]["values"]) == 2


class TestReadLedgerLatest:
    @pytest.mark.asyncio
    async def test_empty_sheet_returns_empty_dict(self):
        sheets = FakeSheets({"Ledger": []})
        assert await ledger.read_ledger_latest(sheets, SHEET_ID) == {}
        params = sheets.calls[0][1]
        assert params["spreadsheetId"] == SHEET_ID
        assert _tab_of(params["range"]) == "Ledger"
        assert params["range"].endswith("!A:L")

    @pytest.mark.asyncio
    async def test_header_only_returns_empty_dict(self):
        sheets = FakeSheets({"Ledger": [ledger.LEDGER_HEADER]})
        assert await ledger.read_ledger_latest(sheets, SHEET_ID) == {}

    @pytest.mark.asyncio
    async def test_latest_per_key_and_short_rows(self):
        sheets = FakeSheets(
            {
                "Ledger": [
                    ledger.LEDGER_HEADER,
                    _row_values(applied_at="2026-09-25T10:00:00Z", readback_hash="old"),
                    _row_values(applied_at="2026-09-26T09:00:00Z", readback_hash="new"),
                    # Older row listed after the newer one must not win.
                    _row_values(
                        applied_at="2026-09-24T09:00:00Z", readback_hash="older"
                    ),
                    # Short row: only the first five cells present.
                    [
                        "2026-09-25T12:00:00Z",
                        "oliver@otbgroup.co.uk",
                        "Bob@OTBGroup.co.uk",
                        "bob@otbgroup.co.uk",
                        "OTB",
                    ],
                    # Blank row, as Sheets returns after a manual deletion.
                    [],
                ]
            }
        )
        latest = await ledger.read_ledger_latest(sheets, SHEET_ID)
        assert set(latest) == {
            ("alice@otbgroup.co.uk", "alice@otbgroup.co.uk"),
            ("bob@otbgroup.co.uk", "bob@otbgroup.co.uk"),
        }
        alice = latest[("alice@otbgroup.co.uk", "alice@otbgroup.co.uk")]
        assert alice["readback_hash"] == "new"
        assert alice["applied_at"] == "2026-09-26T09:00:00Z"
        assert set(alice) == set(ledger.LEDGER_HEADER)
        bob = latest[("bob@otbgroup.co.uk", "bob@otbgroup.co.uk")]
        assert bob["readback_hash"] == ""
        assert bob["run_id"] == ""
        assert bob["entity"] == "OTB"

    @pytest.mark.asyncio
    async def test_foreign_header_is_refused(self):
        sheets = FakeSheets({"Ledger": [["when", "who"], ["x", "y"]]})
        with pytest.raises(ledger.LedgerError):
            await ledger.read_ledger_latest(sheets, SHEET_ID)

    @pytest.mark.asyncio
    async def test_malformed_row_warning_never_logs_personal_data(self, caplog):
        """A row without user/send-as is skipped; the log names only its
        position, applied_at and run_id, never previous_signature_html
        (a person's name, title and mobile) nor the actor."""
        secret_html = "<div>Priya Patel, Head of Ops, 07700 900999</div>"
        sheets = FakeSheets(
            {
                "Ledger": [
                    ledger.LEDGER_HEADER,
                    _row_values(),
                    _row_values(
                        applied_at="2026-09-27T09:00:00Z",
                        user_email="",
                        send_as_email="",
                        previous_signature_html=secret_html,
                        run_id="run-broken",
                    ),
                ]
            }
        )
        with caplog.at_level("WARNING", logger="gsignatures.ledger"):
            latest = await ledger.read_ledger_latest(sheets, SHEET_ID)
        assert len(latest) == 1
        text = caplog.text
        assert "row 3" in text
        assert "2026-09-27T09:00:00Z" in text and "run-broken" in text
        assert "Priya" not in text and "07700" not in text
        assert "oliver@otbgroup.co.uk" not in text
        assert "<div>" not in text


class TestReadLedgerRows:
    @pytest.mark.asyncio
    async def test_every_attributable_row_in_sheet_order(self):
        sheets = FakeSheets(
            {
                "Ledger": [
                    ledger.LEDGER_HEADER,
                    _row_values(run_id="run-1"),
                    [],
                    _row_values(run_id="run-2", applied_at="2026-09-26T09:00:00Z"),
                    ["2026-09-25T12:00:00Z", "oliver@otbgroup.co.uk", "", ""],
                ]
            }
        )
        rows = await ledger.read_ledger_rows(sheets, SHEET_ID)
        assert [r["run_id"] for r in rows] == ["run-1", "run-2"]
        assert all(set(r) == set(ledger.LEDGER_HEADER) for r in rows)
        assert ledger.ledger_key(rows[0]) == (
            "alice@otbgroup.co.uk",
            "alice@otbgroup.co.uk",
        )

    @pytest.mark.asyncio
    async def test_empty_and_header_only(self):
        assert await ledger.read_ledger_rows(FakeSheets({"Ledger": []}), SHEET_ID) == []
        sheets = FakeSheets({"Ledger": [ledger.LEDGER_HEADER]})
        assert await ledger.read_ledger_rows(sheets, SHEET_ID) == []


class TestLedgerTabExists:
    @pytest.mark.asyncio
    async def test_reports_presence_without_reading_values(self):
        sheets = FakeSheets({"Other": [["x"]]})
        assert await ledger.ledger_tab_exists(sheets, SHEET_ID) is False
        assert sheets.names() == ["spreadsheets.get"]
        sheets = FakeSheets({"Ledger": []})
        assert await ledger.ledger_tab_exists(sheets, SHEET_ID) is True

    @pytest.mark.asyncio
    async def test_blank_sheet_id_is_refused(self):
        with pytest.raises(ledger.LedgerError):
            await ledger.ledger_tab_exists(FakeSheets(), "")


class TestAssertTabWritable:
    @pytest.mark.asyncio
    async def test_rewrites_the_header_unchanged(self):
        sheets = FakeSheets({"Ledger": [ledger.LEDGER_HEADER, _row_values()]})
        before = [list(r) for r in sheets.tabs["Ledger"]]
        await ledger.assert_tab_writable(
            sheets, SHEET_ID, ledger.LEDGER_TAB, ledger.LEDGER_HEADER
        )
        assert sheets.names() == ["values.update"]
        params = sheets.calls[0][1]
        assert _tab_of(params["range"]) == "Ledger"
        assert params["range"].endswith("!1:1")
        assert params["valueInputOption"] == "RAW"
        assert params["body"] == {"values": [ledger.LEDGER_HEADER]}
        assert sheets.tabs["Ledger"] == before

    @pytest.mark.asyncio
    async def test_read_only_share_is_refused(self):
        from googleapiclient.errors import HttpError

        from tests.gsignatures.fakes import FakeSheets as SharedFakeSheets
        from tests.gsignatures.fakes import http_error

        sheets = SharedFakeSheets({"Ledger": [ledger.LEDGER_HEADER]})
        sheets.fail_writes = http_error(403, "forbidden")
        with pytest.raises(HttpError):
            await ledger.assert_tab_writable(
                sheets, SHEET_ID, ledger.LEDGER_TAB, ledger.LEDGER_HEADER
            )

    @pytest.mark.asyncio
    async def test_blank_arguments_are_refused_before_any_call(self):
        sheets = FakeSheets({"Ledger": [ledger.LEDGER_HEADER]})
        with pytest.raises(ledger.LedgerError):
            await ledger.assert_tab_writable(sheets, "", "Ledger", ledger.LEDGER_HEADER)
        with pytest.raises(ledger.LedgerError):
            await ledger.assert_tab_writable(
                sheets, SHEET_ID, " ", ledger.LEDGER_HEADER
            )
        with pytest.raises(ledger.LedgerError):
            await ledger.assert_tab_writable(sheets, SHEET_ID, "Ledger", [])
        assert sheets.calls == []


class TestWriteAuditReport:
    @pytest.mark.asyncio
    async def test_clears_then_writes_header_and_rows(self):
        sheets = FakeSheets({"Audit 2026-09-25": [ledger.AUDIT_HEADER, ["stale"] * 11]})
        rows = [
            {
                "audited_at": "2026-09-25T12:00:00Z",
                "user_email": "alice@otbgroup.co.uk",
                "send_as_email": "alice@otbgroup.co.uk",
                "entity": "OTB",
                "expected_template_version": "1.0.0",
                "expected_statutory_version": "1.0.0",
                "ledger_template_version": "1.0.0",
                "ledger_statutory_version": "1.0.0",
                "status": "in_sync",
                "reason": "",
                "current_hash": "abc",
            },
            {
                "audited_at": "2026-09-25T12:00:00Z",
                "user_email": "bob@otbgroup.co.uk",
                "send_as_email": "bob@otbgroup.co.uk",
                "status": "never_applied",
                "reason": "no ledger row",
            },
        ]
        await ledger.write_audit_report(sheets, SHEET_ID, "Audit 2026-09-25", rows)
        names = sheets.names()
        assert names.index("values.clear") < names.index("values.update")
        assert "spreadsheets.batchUpdate" not in names
        clear = sheets.calls[names.index("values.clear")][1]
        assert _tab_of(clear["range"]) == "Audit 2026-09-25"
        update = sheets.calls[-1][1]
        assert update["valueInputOption"] == "RAW"
        assert _tab_of(update["range"]) == "Audit 2026-09-25"
        assert update["body"]["values"][0] == ledger.AUDIT_HEADER
        assert update["body"]["values"][1] == [
            "2026-09-25T12:00:00Z",
            "alice@otbgroup.co.uk",
            "alice@otbgroup.co.uk",
            "OTB",
            "1.0.0",
            "1.0.0",
            "1.0.0",
            "1.0.0",
            "in_sync",
            "",
            "abc",
        ]
        assert update["body"]["values"][2][8] == "never_applied"
        assert update["body"]["values"][2][3] == ""
        assert sheets.tabs["Audit 2026-09-25"] == update["body"]["values"]

    @pytest.mark.asyncio
    async def test_creates_missing_tab_first(self):
        sheets = FakeSheets()
        await ledger.write_audit_report(sheets, SHEET_ID, "Audit", [])
        names = sheets.names()
        assert names[0] == "spreadsheets.get"
        assert "spreadsheets.batchUpdate" in names
        assert names.index("spreadsheets.batchUpdate") < names.index("values.clear")
        assert sheets.tabs["Audit"] == [ledger.AUDIT_HEADER]

    @pytest.mark.asyncio
    async def test_unknown_key_is_refused_before_clearing(self):
        sheets = FakeSheets({"Audit": [ledger.AUDIT_HEADER, ["keep"] * 11]})
        with pytest.raises(ledger.LedgerError):
            await ledger.write_audit_report(
                sheets, SHEET_ID, "Audit", [{"status": "x", "bogus": 1}]
            )
        assert "values.clear" not in sheets.names()
        assert sheets.tabs["Audit"][1] == ["keep"] * 11

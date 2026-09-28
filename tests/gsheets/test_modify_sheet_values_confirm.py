"""modify_sheet_values whole-tab clear needs confirm (tier 3, item 7).

clear_values=True with a range_name that is a bare sheet name (no "!" and
no A1 cell reference) empties the whole tab, so it needs confirm=True. A
cell range clears without confirmation, and writes are unaffected.

The Sheets service is a MagicMock; nothing touches the network.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.utils import UserInputError
from gsheets import sheets_tools
from gsheets.sheets_tools import is_bare_sheet_name

USER = "oliver@otbgroup.co.uk"


def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


modify_sheet_values = _unwrap(sheets_tools.modify_sheet_values)


def _service() -> MagicMock:
    service = MagicMock()
    values = service.spreadsheets.return_value.values.return_value
    values.clear.return_value.execute.return_value = {"clearedRange": "Sheet1!A1:Z1000"}
    values.update.return_value.execute.return_value = {"updatedCells": 2}
    return service


def _clear_calls(service: MagicMock):
    return service.spreadsheets.return_value.values.return_value.clear.call_args_list


class TestBareSheetNameDetection:
    @pytest.mark.parametrize(
        "range_name", ["Sheet1", "Q3 Data", "'Q3 Data'", "AB", "Data2024", "", "  "]
    )
    def test_bare_names(self, range_name):
        assert is_bare_sheet_name(range_name) is True

    @pytest.mark.parametrize(
        "range_name",
        [
            "Sheet1!A1:B2",
            "Sheet1!A:A",
            "'Q3 Data'!B2",
            "A1:B2",
            "A1",
            "A:C",
            "1:5",
            "$A$1:$B$2",
            "AA10:AB",
        ],
    )
    def test_cell_references(self, range_name):
        assert is_bare_sheet_name(range_name) is False


class TestClearNeedsConfirm:
    @pytest.mark.asyncio
    async def test_bare_sheet_clear_without_confirm_refused(self):
        service = _service()

        with pytest.raises(UserInputError) as excinfo:
            await modify_sheet_values(
                service,
                USER,
                spreadsheet_id="ss1",
                range_name="Sheet1",
                clear_values=True,
            )

        message = str(excinfo.value)
        assert "Refused" in message
        assert "'Sheet1'" in message
        assert "whole tab" in message
        assert "confirm=True" in message
        assert "Sheet1!A2:D100" in message
        assert _clear_calls(service) == []

    @pytest.mark.asyncio
    async def test_bare_sheet_clear_with_confirm_proceeds(self):
        service = _service()

        result = await modify_sheet_values(
            service, USER, "ss1", "Sheet1", clear_values=True, confirm=True
        )

        assert "Successfully cleared range" in result
        (call,) = _clear_calls(service)
        assert call.kwargs == {"spreadsheetId": "ss1", "range": "Sheet1"}

    @pytest.mark.asyncio
    async def test_cell_range_clear_needs_no_confirm(self):
        service = _service()

        result = await modify_sheet_values(
            service, USER, "ss1", "Sheet1!A2:D100", clear_values=True
        )

        assert "Successfully cleared range" in result
        assert len(_clear_calls(service)) == 1

    @pytest.mark.asyncio
    async def test_write_to_bare_sheet_needs_no_confirm(self):
        service = _service()

        result = await modify_sheet_values(
            service, USER, "ss1", "Sheet1", values=[["a", "b"]]
        )

        assert "Successfully updated" in result
        assert _clear_calls(service) == []
        update = service.spreadsheets.return_value.values.return_value.update
        assert update.call_args.kwargs["range"] == "Sheet1"

"""list_spreadsheets must say when Drive paged the result.

``files.list`` returns at most ``pageSize`` files and a ``nextPageToken``
when more exist, but only if the token is asked for in the ``fields`` mask.
The tool asked for ``files(...)`` alone, so the token never arrived and a
truncated page read as complete. The default page size is unchanged; the
tool now requests the token and appends one line when it is present.

The Drive service is a MagicMock; nothing touches the network.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from gsheets import sheets_tools

USER = "oliver@otbgroup.co.uk"
MORE_LINE = "More results available: raise max_results."


def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


list_spreadsheets = _unwrap(sheets_tools.list_spreadsheets)


def _file(file_id: str) -> dict:
    return {
        "id": file_id,
        "name": f"Sheet {file_id}",
        "modifiedTime": "2026-09-26T09:00:00.000Z",
        "webViewLink": f"https://docs.google.com/spreadsheets/d/{file_id}",
    }


def _service(list_response: dict) -> MagicMock:
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = list_response
    return service


class TestListSpreadsheetsPaging:
    @pytest.mark.asyncio
    async def test_next_page_token_adds_the_more_results_line(self):
        service = _service(
            {"files": [_file("s1"), _file("s2")], "nextPageToken": "tok-2"}
        )

        result = await list_spreadsheets(
            service=service, user_google_email=USER, max_results=2
        )

        assert "Successfully listed 2 spreadsheets" in result
        assert result.rstrip().endswith(MORE_LINE)
        assert result.count(MORE_LINE) == 1
        assert service.files.return_value.list.call_args.kwargs["pageSize"] == 2

    @pytest.mark.asyncio
    async def test_no_token_means_no_line(self):
        service = _service({"files": [_file("s1")]})

        result = await list_spreadsheets(service=service, user_google_email=USER)

        assert "Successfully listed 1 spreadsheets" in result
        assert "More results available" not in result
        assert service.files.return_value.list.call_args.kwargs["pageSize"] == 25

    @pytest.mark.asyncio
    async def test_fields_mask_requests_the_token(self):
        """Without nextPageToken in fields Drive never returns it, and the
        line could never fire."""
        service = _service({"files": [_file("s1")]})

        await list_spreadsheets(service=service, user_google_email=USER)

        kwargs = service.files.return_value.list.call_args.kwargs
        fields = kwargs["fields"].replace(" ", "")
        assert fields.startswith("nextPageToken,")
        assert "files(id,name,modifiedTime,webViewLink)" in fields
        # The rest of the request is unchanged.
        assert kwargs["q"] == "mimeType='application/vnd.google-apps.spreadsheet'"
        assert kwargs["orderBy"] == "modifiedTime desc"
        assert kwargs["supportsAllDrives"] is True
        assert kwargs["includeItemsFromAllDrives"] is True

    @pytest.mark.asyncio
    async def test_empty_result_is_unchanged(self):
        service = _service({"files": [], "nextPageToken": "never-happens"})

        result = await list_spreadsheets(service=service, user_google_email=USER)

        assert result == f"No spreadsheets found for {USER}."

    def test_default_max_results_is_still_25(self):
        assert (
            inspect.signature(list_spreadsheets).parameters["max_results"].default == 25
        )

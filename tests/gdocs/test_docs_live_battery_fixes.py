"""Tests for the four Google Docs tool defects found by the live battery
against the Docs API on 2026-09-26.

1. modify_doc_text: text plus a formatting flag with no end_index inserts
   the text and formats the inserted range in one batchUpdate.
2. update_doc_headers_footers: a missing DEFAULT header or footer is created
   with createHeader / createFooter, the new segment ID is read from the
   response, and the content goes into that segment.
3. create_table_with_data and insert_doc_elements: an index at or past the
   body end index is clamped to end index minus 1, and the descriptions
   point callers at max_insertion_index.
4. insert_doc_image: a private Drive file is refused with a clear message
   before the Docs API is called, and the Docs API 400 for a URL Google
   cannot fetch returns the same message. No sharing call anywhere.
5. update_doc_headers_footers: the target header or footer is the one
   documentStyle names for the requested type (defaultHeaderId,
   firstPageHeaderId, evenPageHeaderId and the footer equivalents). Header
   and Footer objects carry no type and every id is an opaque kix string,
   so the old pattern match and first-available fallback picked the wrong
   section in any document with more than one.
6. modify_doc_text and update_paragraph_style: an end_index at or past the
   body end index is clamped to max_insertion_index, the same rule the
   insert tools apply, with the same note in the result.

All Google services are MagicMock doubles; nothing touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from gdocs import docs_tools
from gdocs.docs_structure import (
    analyze_document_complexity,
    get_body_end_index,
    max_insertion_index,
)
from gdocs.managers.header_footer_manager import HeaderFooterManager

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
USER = "oliver@otbgroup.co.uk"
DOC = "1abcDEFghiJKLmnoPQRstuVWXyz0123456789abcdef"


def _unwrap(fn):
    """Peel functools.wraps layers down to the original implementation."""
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


modify_doc_text = _unwrap(docs_tools.modify_doc_text)
update_doc_headers_footers = _unwrap(docs_tools.update_doc_headers_footers)
create_table_with_data = _unwrap(docs_tools.create_table_with_data)
insert_doc_elements = _unwrap(docs_tools.insert_doc_elements)
insert_doc_image = _unwrap(docs_tools.insert_doc_image)
inspect_doc_structure = _unwrap(docs_tools.inspect_doc_structure)
update_paragraph_style = _unwrap(docs_tools.update_paragraph_style)


def _doc_with_end_index(end_index: int) -> dict:
    """A minimal document whose body ends at end_index (trailing newline)."""
    return {
        "title": "Battery",
        "body": {
            "content": [
                {"endIndex": 1, "sectionBreak": {}},
                {
                    "startIndex": 1,
                    "endIndex": end_index,
                    "paragraph": {
                        "elements": [
                            {
                                "startIndex": 1,
                                "endIndex": end_index,
                                "textRun": {"content": "x" * (end_index - 2) + "\n"},
                            }
                        ]
                    },
                },
            ]
        },
    }


def _batch_bodies(service) -> list[list[dict]]:
    """Every batchUpdate request list sent through the service double."""
    return [
        call.kwargs["body"]["requests"]
        for call in service.documents.return_value.batchUpdate.call_args_list
    ]


def _http_error(status: int, message: str) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = message
    content = json.dumps(
        {"error": {"code": status, "message": message, "status": "INVALID_ARGUMENT"}}
    ).encode()
    return HttpError(resp, content)


# ---------------------------------------------------------------------------
# Defect 1: insert and format in one call
# ---------------------------------------------------------------------------


class TestModifyDocTextInsertAndFormat:
    @pytest.mark.asyncio
    async def test_text_plus_bold_without_end_index_inserts_then_styles(self):
        service = MagicMock()
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        result = await modify_doc_text(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            start_index=5,
            text="Hello",
            bold=True,
        )

        assert not result.startswith("Error"), result
        assert "'end_index' is required" not in result
        (requests,) = _batch_bodies(service)
        assert len(requests) == 2
        assert requests[0] == {
            "insertText": {"location": {"index": 5}, "text": "Hello"}
        }
        style = requests[1]["updateTextStyle"]
        assert style["range"] == {"startIndex": 5, "endIndex": 10}
        assert style["textStyle"]["bold"] is True
        assert "bold" in style["fields"]
        assert "Inserted text at index 5" in result
        assert "Applied formatting (bold=True) to range 5-10" in result

    @pytest.mark.asyncio
    async def test_insert_and_format_at_index_zero_shifts_to_one(self):
        service = MagicMock()
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        result = await modify_doc_text(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            start_index=0,
            text="Title",
            italic=True,
            font_size=18,
        )

        assert not result.startswith("Error"), result
        (requests,) = _batch_bodies(service)
        assert requests[0]["insertText"]["location"]["index"] == 1
        assert requests[1]["updateTextStyle"]["range"] == {
            "startIndex": 1,
            "endIndex": 6,
        }

    @pytest.mark.asyncio
    async def test_formatting_only_without_end_index_still_errors(self):
        service = MagicMock()

        result = await modify_doc_text(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            start_index=5,
            bold=True,
        )

        assert result.startswith("Error")
        assert "end_index" in result
        assert not service.documents.return_value.batchUpdate.called

    @pytest.mark.asyncio
    async def test_neither_text_nor_formatting_errors(self):
        service = MagicMock()

        result = await modify_doc_text(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            start_index=5,
        )

        assert result.startswith("Error")
        assert not service.documents.return_value.batchUpdate.called

    @pytest.mark.asyncio
    async def test_replace_with_end_index_and_formatting_unchanged(self):
        service = MagicMock()
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        result = await modify_doc_text(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            start_index=5,
            end_index=9,
            text="New",
            underline=True,
        )

        assert not result.startswith("Error"), result
        (requests,) = _batch_bodies(service)
        assert "deleteContentRange" in requests[0]
        assert requests[0]["deleteContentRange"]["range"] == {
            "startIndex": 5,
            "endIndex": 9,
        }
        assert requests[1]["insertText"] == {
            "location": {"index": 5},
            "text": "New",
        }
        assert requests[2]["updateTextStyle"]["range"] == {
            "startIndex": 5,
            "endIndex": 8,
        }

    def test_docstring_describes_insert_and_format(self):
        doc = docs_tools.modify_doc_text.__doc__ or ""
        assert "text plus formatting, no end_index" in doc
        assert "insertText first, then updateTextStyle" in doc


# ---------------------------------------------------------------------------
# Defect 2: create the header or footer when the document has none
# ---------------------------------------------------------------------------


class TestHeaderFooterCreateWhenMissing:
    @pytest.mark.asyncio
    async def test_missing_header_is_created_then_filled(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(10)
        )
        service.documents.return_value.batchUpdate.return_value.execute.side_effect = [
            {"replies": [{"createHeader": {"headerId": "kix.hdr001"}}]},
            {},
        ]

        manager = HeaderFooterManager(service)
        success, message = await manager.update_header_footer_content(
            DOC, "header", "Confidential", "DEFAULT"
        )

        assert success, message
        assert "Please create a header first" not in message
        assert "kix.hdr001" in message
        first, second = _batch_bodies(service)
        assert first == [{"createHeader": {"type": "DEFAULT"}}]
        assert second == [
            {
                "insertText": {
                    "location": {"segmentId": "kix.hdr001", "index": 0},
                    "text": "Confidential",
                }
            }
        ]

    @pytest.mark.asyncio
    async def test_missing_footer_is_created_then_filled(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(10)
        )
        service.documents.return_value.batchUpdate.return_value.execute.side_effect = [
            {"replies": [{"createFooter": {"footerId": "kix.ftr002"}}]},
            {},
        ]

        manager = HeaderFooterManager(service)
        success, message = await manager.update_header_footer_content(
            DOC, "footer", "Page footer", "DEFAULT"
        )

        assert success, message
        first, second = _batch_bodies(service)
        assert first == [{"createFooter": {"type": "DEFAULT"}}]
        assert second[0]["insertText"]["location"] == {
            "segmentId": "kix.ftr002",
            "index": 0,
        }
        assert second[0]["insertText"]["text"] == "Page footer"

    @pytest.mark.asyncio
    async def test_existing_header_still_replaced_without_create(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = {
            "documentStyle": {"defaultHeaderId": "hdr.existing"},
            "headers": {
                "hdr.existing": {
                    "content": [
                        {
                            "startIndex": 0,
                            "endIndex": 6,
                            "paragraph": {"elements": []},
                        }
                    ]
                }
            },
        }
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        manager = HeaderFooterManager(service)
        success, _ = await manager.update_header_footer_content(
            DOC, "header", "Replaced", "DEFAULT"
        )

        assert success
        (requests,) = _batch_bodies(service)
        assert not any("createHeader" in r for r in requests)
        assert requests[-1]["insertText"]["location"]["segmentId"] == "hdr.existing"

    @pytest.mark.asyncio
    async def test_missing_first_page_header_is_refused_without_create(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(10)
        )

        manager = HeaderFooterManager(service)
        success, message = await manager.update_header_footer_content(
            DOC, "header", "First page", "FIRST_PAGE_ONLY"
        )

        assert not success
        assert "DEFAULT" in message and "FIRST_PAGE_ONLY" in message
        assert not service.documents.return_value.batchUpdate.called

    @pytest.mark.asyncio
    async def test_create_response_without_id_is_a_clean_failure(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(10)
        )
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {
            "replies": [{}]
        }

        manager = HeaderFooterManager(service)
        success, message = await manager.update_header_footer_content(
            DOC, "header", "x", "DEFAULT"
        )

        assert not success
        assert "headerId" in message
        # Only the create was attempted; nothing was inserted blind.
        assert len(_batch_bodies(service)) == 1

    @pytest.mark.asyncio
    async def test_tool_end_to_end_reports_creation(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(10)
        )
        service.documents.return_value.batchUpdate.return_value.execute.side_effect = [
            {"replies": [{"createFooter": {"footerId": "kix.ftr003"}}]},
            {},
        ]

        result = await update_doc_headers_footers(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            section_type="footer",
            content="OTB Group Limited",
        )

        assert not result.startswith("Error"), result
        assert "Created footer" in result
        assert "No footer found" not in result
        assert f"https://docs.google.com/document/d/{DOC}/edit" in result

    def test_docstring_says_it_creates(self):
        doc = docs_tools.update_doc_headers_footers.__doc__ or ""
        assert "createHeader" in doc and "createFooter" in doc
        assert "FIRST_PAGE_ONLY" in doc


# ---------------------------------------------------------------------------
# Defect 3: end index clamp and the documented workflow
# ---------------------------------------------------------------------------


class TestBodyEndIndexHelpers:
    def test_end_index_read_from_last_body_element(self):
        assert get_body_end_index(_doc_with_end_index(42)) == 42
        assert max_insertion_index(_doc_with_end_index(42)) == 41

    def test_unreadable_document_returns_none(self):
        assert get_body_end_index({}) is None
        assert get_body_end_index({"body": {"content": []}}) is None
        assert get_body_end_index({"body": {"content": [{"endIndex": "7"}]}}) is None
        assert max_insertion_index({}) is None

    def test_analysis_exposes_max_insertion_index(self):
        stats = analyze_document_complexity(_doc_with_end_index(30))
        assert stats["total_length"] == 30
        assert stats["max_insertion_index"] == 29


class _RecordingTableManager:
    """Stands in for TableOperationManager and records the index it was given."""

    calls: list[int] = []

    def __init__(self, service):
        self.service = service

    async def create_and_populate_table(
        self, document_id, table_data, index, bold_headers=True
    ):
        type(self).calls.append(index)
        return (
            True,
            "Successfully created table",
            {"rows": len(table_data), "columns": len(table_data[0])},
        )


class TestCreateTableWithDataClamp:
    @pytest.fixture(autouse=True)
    def _fake_manager(self, monkeypatch):
        _RecordingTableManager.calls = []
        monkeypatch.setattr(docs_tools, "TableOperationManager", _RecordingTableManager)

    @pytest.mark.asyncio
    async def test_index_equal_to_end_index_is_clamped_and_reported(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(25)
        )

        result = await create_table_with_data(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            table_data=[["a", "b"], ["1", "2"]],
            index=25,
        )

        assert result.startswith("SUCCESS"), result
        assert _RecordingTableManager.calls == [24]
        assert "Index: 24" in result
        assert "Requested index 25 is at or past the document end index 25" in result
        assert "used 24" in result

    @pytest.mark.asyncio
    async def test_index_past_end_index_is_clamped(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(25)
        )

        await create_table_with_data(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            table_data=[["a"]],
            index=999,
        )

        assert _RecordingTableManager.calls == [24]

    @pytest.mark.asyncio
    async def test_index_inside_body_is_untouched(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(25)
        )

        result = await create_table_with_data(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            table_data=[["a"]],
            index=10,
        )

        assert _RecordingTableManager.calls == [10]
        assert "at or past the document end index" not in result

    def test_descriptions_agree_on_the_index_to_use(self):
        table_doc = docs_tools.create_table_with_data.__doc__ or ""
        inspect_doc = docs_tools.inspect_doc_structure.__doc__ or ""
        assert "max_insertion_index" in table_doc
        assert "max_insertion_index" in inspect_doc
        assert "'total_length' value from inspect_doc_structure as your index" not in (
            table_doc
        )
        assert "total_length: Maximum safe index for insertion" not in inspect_doc
        assert "total_length - 1" in inspect_doc


class TestInsertDocElementsClamp:
    @pytest.mark.asyncio
    async def test_page_break_at_end_index_is_clamped(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(25)
        )
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        result = await insert_doc_elements(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            element_type="page_break",
            index=25,
        )

        (requests,) = _batch_bodies(service)
        assert requests == [{"insertPageBreak": {"location": {"index": 24}}}]
        assert "at index 24" in result
        assert "Requested index 25 is at or past the document end index 25" in result

    @pytest.mark.asyncio
    async def test_table_inside_body_is_untouched(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(25)
        )
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        result = await insert_doc_elements(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            element_type="table",
            index=7,
            rows=2,
            columns=3,
        )

        (requests,) = _batch_bodies(service)
        assert requests[0]["insertTable"]["location"]["index"] == 7
        assert "at or past the document end index" not in result


class TestInspectDocStructureOutput:
    @pytest.mark.asyncio
    async def test_basic_and_detailed_output_carry_max_insertion_index(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(25)
        )

        basic = await inspect_doc_structure(
            service=service, user_google_email=USER, document_id=DOC
        )
        detailed = await inspect_doc_structure(
            service=service, user_google_email=USER, document_id=DOC, detailed=True
        )

        for text in (basic, detailed):
            payload = json.loads(text.split("\n\n", 1)[1].rsplit("\n\nLink:", 1)[0])
            assert payload["total_length"] == 25
            assert payload["max_insertion_index"] == 24


# ---------------------------------------------------------------------------
# Defect 4: private Drive images are refused, never shared
# ---------------------------------------------------------------------------


def _drive_with_file(permissions):
    drive = MagicMock()
    drive.files.return_value.get.return_value.execute.return_value = {
        "id": "img123",
        "name": "logo.png",
        "mimeType": "image/png",
        "size": "1234",
        "permissions": permissions,
    }
    return drive


class TestInsertDocImagePublicCheck:
    @pytest.mark.asyncio
    async def test_private_drive_file_is_refused_before_docs_call(self):
        docs = MagicMock()
        drive = _drive_with_file(
            [
                {"type": "user", "role": "owner"},
                {"type": "domain", "role": "reader"},
            ]
        )

        result = await insert_doc_image(
            docs_service=docs,
            drive_service=drive,
            user_google_email=USER,
            document_id=DOC,
            image_source="img123",
            index=5,
        )

        assert result.startswith("Error")
        assert "publicly" in result.lower() or "public" in result.lower()
        assert "policy" in result
        assert "https" in result
        assert "img123" in result
        assert not docs.documents.return_value.batchUpdate.called
        # No sharing of any kind happened.
        assert not drive.permissions.called
        assert not drive.files.return_value.update.called

    @pytest.mark.asyncio
    async def test_drive_lookup_requests_permissions_with_all_drives(self):
        docs = MagicMock()
        drive = _drive_with_file([{"type": "user", "role": "owner"}])

        await insert_doc_image(
            docs_service=docs,
            drive_service=drive,
            user_google_email=USER,
            document_id=DOC,
            image_source="img123",
            index=5,
        )

        kwargs = drive.files.return_value.get.call_args.kwargs
        assert kwargs["fileId"] == "img123"
        assert kwargs["fields"] == "id,name,mimeType,size,permissions(type,role)"
        assert kwargs["supportsAllDrives"] is True

    @pytest.mark.asyncio
    async def test_public_drive_file_is_inserted_as_before(self):
        docs = MagicMock()
        docs.documents.return_value.batchUpdate.return_value.execute.return_value = {}
        drive = _drive_with_file(
            [
                {"type": "user", "role": "owner"},
                {"type": "anyone", "role": "reader"},
            ]
        )

        result = await insert_doc_image(
            docs_service=docs,
            drive_service=drive,
            user_google_email=USER,
            document_id=DOC,
            image_source="img123",
            index=5,
            width=200,
        )

        assert not result.startswith("Error"), result
        (requests,) = _batch_bodies(docs)
        image = requests[0]["insertInlineImage"]
        assert image["uri"] == "https://drive.google.com/uc?id=img123"
        assert image["location"] == {"index": 5}
        assert image["objectSize"]["width"] == {"magnitude": 200, "unit": "PT"}
        assert not drive.permissions.called

    @pytest.mark.asyncio
    async def test_non_image_drive_file_still_refused(self):
        docs = MagicMock()
        drive = MagicMock()
        drive.files.return_value.get.return_value.execute.return_value = {
            "id": "pdf1",
            "name": "brochure.pdf",
            "mimeType": "application/pdf",
            "permissions": [{"type": "anyone", "role": "reader"}],
        }

        result = await insert_doc_image(
            docs_service=docs,
            drive_service=drive,
            user_google_email=USER,
            document_id=DOC,
            image_source="pdf1",
            index=5,
        )

        assert "not an image" in result
        assert not docs.documents.return_value.batchUpdate.called

    @pytest.mark.asyncio
    async def test_url_rejected_by_google_returns_clear_message(self):
        docs = MagicMock()
        docs.documents.return_value.batchUpdate.return_value.execute.side_effect = (
            _http_error(
                400,
                "Invalid requests[0].insertInlineImage: There was a problem "
                "retrieving the image. The provided image should be publicly "
                "accessible, within size limit, and in supported formats.",
            )
        )
        drive = MagicMock()

        result = await insert_doc_image(
            docs_service=docs,
            drive_service=drive,
            user_google_email=USER,
            document_id=DOC,
            image_source="https://example.com/private.png",
            index=5,
        )

        assert result.startswith("Error")
        assert "HttpError" not in result
        assert "policy" in result
        assert "https://example.com/private.png" in result
        assert not drive.files.called
        assert not drive.permissions.called

    @pytest.mark.asyncio
    async def test_other_400_from_docs_is_still_raised(self):
        docs = MagicMock()
        docs.documents.return_value.batchUpdate.return_value.execute.side_effect = (
            _http_error(400, "Index 5 must be less than the end index of the segment")
        )

        with pytest.raises(HttpError):
            await insert_doc_image(
                docs_service=docs,
                drive_service=MagicMock(),
                user_google_email=USER,
                document_id=DOC,
                image_source="https://example.com/ok.png",
                index=5,
            )

    def test_docstring_says_public_up_front(self):
        doc = docs_tools.insert_doc_image.__doc__ or ""
        assert "publicly" in doc
        assert "Private Drive files are refused" in doc

    def test_docs_tools_never_shares_a_file(self):
        src = (REPO_ROOT / "gdocs" / "docs_tools.py").read_text()
        assert "permissions()" not in src
        assert "anyoneWithLink" not in src
        assert '.create(body={"role"' not in src


# ---------------------------------------------------------------------------
# Defect 5: pick the header or footer documentStyle names for the type
# ---------------------------------------------------------------------------


def _section(end_index: int) -> dict:
    return {
        "content": [
            {
                "startIndex": 0,
                "endIndex": end_index,
                "paragraph": {"elements": []},
            }
        ]
    }


def _doc_with_default_and_first_page_headers() -> dict:
    """A document with both a default and a first-page header.

    The default header is listed first in the map, so any "first available"
    fallback would pick it.
    """
    doc = _doc_with_end_index(10)
    doc["documentStyle"] = {
        "defaultHeaderId": "kix.default01",
        "firstPageHeaderId": "kix.first01",
        "defaultFooterId": "kix.dfoot01",
    }
    doc["headers"] = {
        "kix.default01": _section(8),
        "kix.first01": _section(5),
    }
    doc["footers"] = {"kix.dfoot01": _section(3)}
    return doc


class TestHeaderFooterTargetsDocumentStyleId:
    @pytest.mark.asyncio
    async def test_first_page_update_targets_the_first_page_id_only(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_default_and_first_page_headers()
        )
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        manager = HeaderFooterManager(service)
        success, message = await manager.update_header_footer_content(
            DOC, "header", "Cover page", "FIRST_PAGE_ONLY"
        )

        assert success, message
        (requests,) = _batch_bodies(service)
        segment_ids = set()
        for request in requests:
            if "deleteContentRange" in request:
                segment_ids.add(request["deleteContentRange"]["range"]["segmentId"])
            else:
                segment_ids.add(request["insertText"]["location"]["segmentId"])
        assert segment_ids == {"kix.first01"}
        assert "kix.default01" not in str(requests)
        # The delete range comes from the first-page section (endIndex 5),
        # not the default one (endIndex 8).
        assert requests[0]["deleteContentRange"]["range"]["endIndex"] == 4
        assert not any("createHeader" in r for r in requests)

    @pytest.mark.asyncio
    async def test_default_update_targets_the_default_id_even_when_listed_last(
        self,
    ):
        doc = _doc_with_default_and_first_page_headers()
        # Reverse the map order: the first-page header now comes first.
        doc["headers"] = dict(reversed(list(doc["headers"].items())))
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = doc
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        manager = HeaderFooterManager(service)
        success, message = await manager.update_header_footer_content(
            DOC, "header", "Every page", "DEFAULT"
        )

        assert success, message
        (requests,) = _batch_bodies(service)
        assert requests[-1]["insertText"]["location"]["segmentId"] == "kix.default01"
        assert "kix.first01" not in str(requests)

    @pytest.mark.asyncio
    async def test_even_page_footer_missing_is_refused_not_substituted(self):
        """A default footer exists but no even-page footer: no fallback to
        the default, and no create (the API cannot create EVEN_PAGE)."""
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_default_and_first_page_headers()
        )

        manager = HeaderFooterManager(service)
        success, message = await manager.update_header_footer_content(
            DOC, "footer", "Even", "EVEN_PAGE"
        )

        assert not success
        assert "EVEN_PAGE" in message and "DEFAULT" in message
        assert not service.documents.return_value.batchUpdate.called

    @pytest.mark.asyncio
    async def test_no_header_at_all_takes_the_create_path(self):
        """No documentStyle header id: DEFAULT is created, not looked up."""
        doc = _doc_with_end_index(10)
        doc["documentStyle"] = {"defaultFooterId": "kix.dfoot01"}
        doc["footers"] = {"kix.dfoot01": _section(3)}
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = doc
        service.documents.return_value.batchUpdate.return_value.execute.side_effect = [
            {"replies": [{"createHeader": {"headerId": "kix.hdrnew"}}]},
            {},
        ]

        manager = HeaderFooterManager(service)
        success, message = await manager.update_header_footer_content(
            DOC, "header", "Fresh", "DEFAULT"
        )

        assert success, message
        first, second = _batch_bodies(service)
        assert first == [{"createHeader": {"type": "DEFAULT"}}]
        assert second[0]["insertText"]["location"]["segmentId"] == "kix.hdrnew"
        # The footer id was never mistaken for a header.
        assert "kix.dfoot01" not in str(first) + str(second)

    @pytest.mark.asyncio
    async def test_id_named_by_document_style_but_absent_from_map_is_missing(
        self,
    ):
        doc = _doc_with_end_index(10)
        doc["documentStyle"] = {"firstPageHeaderId": "kix.ghost"}
        doc["headers"] = {"kix.other": _section(4)}
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = doc

        manager = HeaderFooterManager(service)
        success, message = await manager.update_header_footer_content(
            DOC, "header", "x", "FIRST_PAGE_ONLY"
        )

        assert not success
        assert "FIRST_PAGE_ONLY" in message
        assert not service.documents.return_value.batchUpdate.called

    def test_no_pattern_or_first_available_fallback_left_in_source(self):
        source = (
            REPO_ROOT / "gdocs" / "managers" / "header_footer_manager.py"
        ).read_text()
        assert "target_patterns" not in source
        assert "first available section as fallback" not in source
        for field in (
            "defaultHeaderId",
            "firstPageHeaderId",
            "evenPageHeaderId",
            "defaultFooterId",
            "firstPageFooterId",
            "evenPageFooterId",
        ):
            assert field in source, field


# ---------------------------------------------------------------------------
# Defect 6: end_index clamped to max_insertion_index
# ---------------------------------------------------------------------------


class TestEndIndexClamp:
    @pytest.mark.asyncio
    async def test_modify_doc_text_replace_clamps_end_index_by_one(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(42)
        )
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        result = await modify_doc_text(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            start_index=5,
            end_index=42,
            text="New",
        )

        assert not result.startswith("Error"), result
        (requests,) = _batch_bodies(service)
        delete_range = requests[0]["deleteContentRange"]["range"]
        assert delete_range == {"startIndex": 5, "endIndex": 41}
        assert requests[1]["insertText"]["location"]["index"] == 5
        assert "Requested end_index 42" in result
        assert "used 41, the largest valid insertion index" in result

    @pytest.mark.asyncio
    async def test_modify_doc_text_format_only_clamps_end_index(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(30)
        )
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        result = await modify_doc_text(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            start_index=1,
            end_index=31,
            bold=True,
        )

        assert not result.startswith("Error"), result
        (requests,) = _batch_bodies(service)
        assert requests[0]["updateTextStyle"]["range"] == {
            "startIndex": 1,
            "endIndex": 29,
        }
        assert "used 29" in result

    @pytest.mark.asyncio
    async def test_modify_doc_text_in_range_end_index_is_untouched(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(42)
        )
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        result = await modify_doc_text(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            start_index=5,
            end_index=20,
            text="New",
        )

        (requests,) = _batch_bodies(service)
        assert requests[0]["deleteContentRange"]["range"]["endIndex"] == 20
        assert "Requested end_index" not in result

    @pytest.mark.asyncio
    async def test_modify_doc_text_insert_without_end_index_does_not_read_doc(
        self,
    ):
        service = MagicMock()
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        await modify_doc_text(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            start_index=5,
            text="Hi",
        )

        service.documents.return_value.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_modify_doc_text_start_index_at_end_is_a_clean_error(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(10)
        )

        result = await modify_doc_text(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            start_index=9,
            end_index=10,
            bold=True,
        )

        assert result.startswith("Error")
        assert "max_insertion_index" in result
        assert not service.documents.return_value.batchUpdate.called

    @pytest.mark.asyncio
    async def test_update_paragraph_style_clamps_end_index_by_one(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(25)
        )
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        result = await update_paragraph_style(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            start_index=1,
            end_index=25,
            heading_level=1,
        )

        assert not result.startswith("Error"), result
        (requests,) = _batch_bodies(service)
        assert requests[0]["updateParagraphStyle"]["range"] == {
            "startIndex": 1,
            "endIndex": 24,
        }
        assert "range 1-24" in result
        assert "Requested end_index 25" in result
        assert "used 24, the largest valid insertion index" in result

    @pytest.mark.asyncio
    async def test_update_paragraph_style_list_range_is_clamped_too(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(25)
        )
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        result = await update_paragraph_style(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            start_index=1,
            end_index=30,
            list_type="UNORDERED",
        )

        assert not result.startswith("Error"), result
        (requests,) = _batch_bodies(service)
        bullets = [r for r in requests if "createParagraphBullets" in r]
        assert bullets, requests
        assert bullets[0]["createParagraphBullets"]["range"]["endIndex"] == 24

    @pytest.mark.asyncio
    async def test_update_paragraph_style_keeps_start_index_validation(self):
        service = MagicMock()

        result = await update_paragraph_style(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            start_index=0,
            end_index=5,
            heading_level=1,
        )

        assert result == "Error: start_index must be >= 1"
        service.documents.return_value.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_paragraph_style_in_range_end_index_is_untouched(self):
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = (
            _doc_with_end_index(25)
        )
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        result = await update_paragraph_style(
            service=service,
            user_google_email=USER,
            document_id=DOC,
            start_index=1,
            end_index=10,
            alignment="CENTER",
        )

        (requests,) = _batch_bodies(service)
        assert requests[0]["updateParagraphStyle"]["range"]["endIndex"] == 10
        assert "Requested end_index" not in result

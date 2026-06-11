"""Tests for the broken-service-tool fixes (fix/broken-service-tools).

Nine verified bugs across contacts, calendar, docs, chat, gmail and drive.
Unit-scoped: Google API services are MagicMocks, tools are exercised through
the `_unwrap` pattern used across this test suite.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


# ---------------------------------------------------------------------------
# Fix 1: batch_update_contacts — per-field-set updateMask grouping
# ---------------------------------------------------------------------------


class TestBatchUpdateContactsMaskGrouping:
    @pytest.mark.asyncio
    async def test_distinct_field_sets_issue_separate_batch_calls(self):
        from gcontacts.contacts_tools import batch_update_contacts

        impl = _unwrap(batch_update_contacts)

        service = MagicMock()
        service.people.return_value.getBatchGet.return_value.execute.return_value = {
            "responses": [
                {"person": {"resourceName": "people/c1", "etag": "etag-1"}},
                {"person": {"resourceName": "people/c2", "etag": "etag-2"}},
            ]
        }
        service.people.return_value.batchUpdateContacts.return_value.execute.return_value = {
            "updateResult": {}
        }

        result = await impl(
            service=service,
            user_google_email="u@example.com",
            updates=[
                {"contact_id": "c1", "email": "a@example.com"},
                {"contact_id": "c2", "phone": "+44 1234 567890"},
            ],
        )

        batch_update = service.people.return_value.batchUpdateContacts
        assert batch_update.call_count == 2

        bodies = [call.kwargs["body"] for call in batch_update.call_args_list]
        masks = {body["updateMask"] for body in bodies}
        assert masks == {"emailAddresses", "phoneNumbers"}

        for body in bodies:
            contacts = body["contacts"]
            assert len(contacts) == 1
            person = contacts[0]["person"]
            if body["updateMask"] == "emailAddresses":
                assert person["resourceName"] == "people/c1"
                assert person["etag"] == "etag-1"
                assert "phoneNumbers" not in person
            else:
                assert person["resourceName"] == "people/c2"
                assert person["etag"] == "etag-2"
                assert "emailAddresses" not in person

        assert "Batch Update Results" in result

    @pytest.mark.asyncio
    async def test_same_field_set_stays_in_one_call(self):
        from gcontacts.contacts_tools import batch_update_contacts

        impl = _unwrap(batch_update_contacts)

        service = MagicMock()
        service.people.return_value.getBatchGet.return_value.execute.return_value = {
            "responses": [
                {"person": {"resourceName": "people/c1", "etag": "etag-1"}},
                {"person": {"resourceName": "people/c2", "etag": "etag-2"}},
            ]
        }
        service.people.return_value.batchUpdateContacts.return_value.execute.return_value = {
            "updateResult": {}
        }

        await impl(
            service=service,
            user_google_email="u@example.com",
            updates=[
                {"contact_id": "c1", "email": "a@example.com"},
                {"contact_id": "c2", "email": "b@example.com"},
            ],
        )

        batch_update = service.people.return_value.batchUpdateContacts
        assert batch_update.call_count == 1
        body = batch_update.call_args.kwargs["body"]
        assert body["updateMask"] == "emailAddresses"
        assert len(body["contacts"]) == 2


# ---------------------------------------------------------------------------
# Fix 2: modify_event — patch with only caller-provided fields
# ---------------------------------------------------------------------------


class TestModifyEventUsesPatch:
    @pytest.mark.asyncio
    async def test_rename_only_patches_summary_without_start_end(self):
        from gcalendar.calendar_tools import modify_event

        impl = _unwrap(modify_event)

        service = MagicMock()
        service.events.return_value.patch.return_value.execute.return_value = {
            "id": "evt1",
            "summary": "New name",
            "htmlLink": "https://calendar.google.com/event?eid=evt1",
        }

        result = await impl(
            service=service,
            user_google_email="u@example.com",
            event_id="evt1",
            summary="New name",
        )

        patch_call = service.events.return_value.patch
        patch_call.assert_called_once()
        kwargs = patch_call.call_args.kwargs
        assert kwargs["calendarId"] == "primary"
        assert kwargs["eventId"] == "evt1"
        assert kwargs["body"] == {"summary": "New name"}
        assert "start" not in kwargs["body"]
        assert "end" not in kwargs["body"]
        assert "recurrence" not in kwargs["body"]

        # Full-replace update() and the pre-update get() are gone.
        service.events.return_value.update.assert_not_called()
        service.events.return_value.get.assert_not_called()

        assert "Successfully modified event" in result

    @pytest.mark.asyncio
    async def test_reminders_branch_no_longer_issues_blocking_get(self):
        from gcalendar.calendar_tools import modify_event

        impl = _unwrap(modify_event)

        service = MagicMock()
        service.events.return_value.patch.return_value.execute.return_value = {
            "id": "evt1",
            "summary": "Standup",
            "htmlLink": "link",
        }

        await impl(
            service=service,
            user_google_email="u@example.com",
            event_id="evt1",
            reminders=[{"method": "popup", "minutes": 15}],
        )

        service.events.return_value.get.assert_not_called()
        body = service.events.return_value.patch.call_args.kwargs["body"]
        assert body["reminders"] == {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 15}],
        }
        # Nothing the caller didn't supply rides along in the patch body.
        assert set(body) == {"reminders"}


# ---------------------------------------------------------------------------
# Fix 3: header/footer content replacement targets the section segment
# ---------------------------------------------------------------------------


class TestHeaderFooterSegmentId:
    @pytest.mark.asyncio
    async def test_requests_carry_segment_id_of_header(self):
        from gdocs.managers.header_footer_manager import HeaderFooterManager

        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = {
            "headers": {
                "hdr.abc123": {
                    "type": "DEFAULT",
                    "content": [
                        {
                            "startIndex": 0,
                            "endIndex": 10,
                            "paragraph": {"elements": []},
                        }
                    ],
                }
            }
        }
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        manager = HeaderFooterManager(service)
        success, message = await manager.update_header_footer_content(
            "doc1", "header", "New header text", "DEFAULT"
        )

        assert success, message
        requests = service.documents.return_value.batchUpdate.call_args.kwargs["body"][
            "requests"
        ]
        delete_range = requests[0]["deleteContentRange"]["range"]
        assert delete_range["segmentId"] == "hdr.abc123"
        assert delete_range["startIndex"] == 0
        assert delete_range["endIndex"] == 9  # paragraph end marker preserved

        insert_location = requests[1]["insertText"]["location"]
        assert insert_location["segmentId"] == "hdr.abc123"
        assert insert_location["index"] == 0
        assert requests[1]["insertText"]["text"] == "New header text"

    @pytest.mark.asyncio
    async def test_requests_carry_segment_id_of_footer(self):
        from gdocs.managers.header_footer_manager import HeaderFooterManager

        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = {
            "footers": {
                "ftr.xyz789": {
                    "content": [
                        {
                            "startIndex": 0,
                            "endIndex": 1,
                            "paragraph": {"elements": []},
                        }
                    ],
                }
            }
        }
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        manager = HeaderFooterManager(service)
        success, message = await manager.update_header_footer_content(
            "doc1", "footer", "Page footer", "DEFAULT"
        )

        assert success, message
        requests = service.documents.return_value.batchUpdate.call_args.kwargs["body"][
            "requests"
        ]
        for request in requests:
            if "deleteContentRange" in request:
                assert request["deleteContentRange"]["range"]["segmentId"] == "ftr.xyz789"
            else:
                assert request["insertText"]["location"]["segmentId"] == "ftr.xyz789"


# ---------------------------------------------------------------------------
# Fix 4: insert_doc_elements list — no nested request arrays
# ---------------------------------------------------------------------------


class TestInsertDocElementsListRequests:
    @pytest.mark.asyncio
    async def test_list_insert_builds_flat_dict_requests(self):
        from gdocs.docs_tools import insert_doc_elements

        impl = _unwrap(insert_doc_elements)

        service = MagicMock()
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        result = await impl(
            service=service,
            user_google_email="u@example.com",
            document_id="doc1",
            element_type="list",
            index=5,
            list_type="UNORDERED",
            text="First item",
        )

        requests = service.documents.return_value.batchUpdate.call_args.kwargs["body"][
            "requests"
        ]
        assert requests, "expected batchUpdate requests"
        assert all(isinstance(request, dict) for request in requests)
        assert "insertText" in requests[0]
        assert any("createParagraphBullets" in request for request in requests)
        assert "Inserted unordered list" in result


# ---------------------------------------------------------------------------
# Fix 5: chat search_messages — client-side matching, no unsupported filter
# ---------------------------------------------------------------------------


class TestChatSearchMessages:
    @pytest.mark.asyncio
    @patch("gchat.chat_tools._resolve_sender", new_callable=AsyncMock)
    async def test_all_spaces_search_matches_client_side_without_filter(
        self, mock_resolve
    ):
        from gchat.chat_tools import search_messages

        mock_resolve.return_value = "Test User"

        def _msg(name, text):
            return {
                "name": name,
                "text": text,
                "createTime": "2026-01-01T00:00:00Z",
                "sender": {"name": "users/123", "displayName": "Test User"},
            }

        chat_service = MagicMock()
        chat_service.spaces.return_value.list.return_value.execute.return_value = {
            "spaces": [
                {"name": "spaces/A", "displayName": "Alpha"},
                {"name": "spaces/B", "displayName": "Beta"},
            ]
        }
        chat_service.spaces.return_value.messages.return_value.list.return_value.execute.side_effect = [
            {
                "messages": [
                    _msg("spaces/A/messages/1", "Quarterly REPORT is ready"),
                    _msg("spaces/A/messages/2", "lunch plans?"),
                ]
            },
            {"messages": [_msg("spaces/B/messages/3", "see the report attached")]},
        ]

        result = await _unwrap(search_messages)(
            chat_service=chat_service,
            people_service=MagicMock(),
            user_google_email="u@example.com",
            query="report",
        )

        # Case-insensitive substring match, non-matching message excluded.
        assert "Quarterly REPORT is ready" in result
        assert "see the report attached" in result
        assert "lunch plans?" not in result
        assert "Found 2 messages" in result

        # No server-side filter on any messages.list call.
        list_calls = chat_service.spaces.return_value.messages.return_value.list.call_args_list
        assert len(list_calls) == 2
        for call in list_calls:
            assert "filter" not in call.kwargs

    @pytest.mark.asyncio
    @patch("gchat.chat_tools._resolve_sender", new_callable=AsyncMock)
    async def test_single_space_search_has_no_filter_kwarg(self, mock_resolve):
        from gchat.chat_tools import search_messages

        mock_resolve.return_value = "Test User"

        chat_service = MagicMock()
        chat_service.spaces.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": [
                {
                    "name": "spaces/A/messages/1",
                    "text": "deploy went fine",
                    "createTime": "2026-01-01T00:00:00Z",
                    "sender": {"name": "users/123"},
                }
            ]
        }

        result = await _unwrap(search_messages)(
            chat_service=chat_service,
            people_service=MagicMock(),
            user_google_email="u@example.com",
            query="Deploy",
            space_id="spaces/A",
        )

        assert "deploy went fine" in result
        call = chat_service.spaces.return_value.messages.return_value.list.call_args
        assert "filter" not in call.kwargs
        assert call.kwargs["parent"] == "spaces/A"


# ---------------------------------------------------------------------------
# Fix 6: _format_thread_content — case-insensitive header extraction
# ---------------------------------------------------------------------------


class TestThreadHeaderCaseInsensitive:
    def test_outlook_style_message_id_header_is_found(self):
        from gmail.gmail_tools import _format_thread_content

        thread_data = {
            "messages": [
                {
                    "payload": {
                        "headers": [
                            {"name": "subject", "value": "Project sync"},
                            {"name": "from", "value": "sender@outlook.com"},
                            {"name": "date", "value": "Mon, 1 Jun 2026 10:00:00 +0000"},
                            {"name": "Message-Id", "value": "<abc123@outlook.com>"},
                            {"name": "In-Reply-To", "value": "<root@otbgroup.co.uk>"},
                            {"name": "references", "value": "<root@otbgroup.co.uk>"},
                        ],
                        "mimeType": "text/plain",
                        "body": {},
                    }
                }
            ]
        }

        output = _format_thread_content(thread_data, "thread-1")

        assert "Subject: Project sync" in output
        assert "From: sender@outlook.com" in output
        assert "Message-ID: <abc123@outlook.com>" in output
        assert "In-Reply-To: <root@otbgroup.co.uk>" in output
        assert "References: <root@otbgroup.co.uk>" in output


# ---------------------------------------------------------------------------
# Fix 7: attachment failures abort the send instead of being dropped
# ---------------------------------------------------------------------------


class TestGmailAttachmentFailuresRaise:
    @pytest.mark.asyncio
    async def test_one_bad_attachment_raises_and_send_never_called(self):
        from gmail.gmail_tools import send_gmail_message

        impl = _unwrap(send_gmail_message)
        service = MagicMock()

        good = {
            "content": base64.b64encode(b"hello").decode(),
            "filename": "good.txt",
        }
        bad = {"path": "/nonexistent/definitely-missing.pdf"}

        with pytest.raises(ValueError) as exc_info:
            await impl(
                service=service,
                user_google_email="u@example.com",
                to="x@example.com",
                subject="Hi",
                body="Body",
                attachments=[good, bad],
            )

        assert "/nonexistent/definitely-missing.pdf" in str(exc_info.value)
        assert "1 of 2" in str(exc_info.value)
        service.users.return_value.messages.return_value.send.assert_not_called()

    def test_prepare_succeeds_with_all_good_attachments(self):
        from gmail.gmail_tools import _prepare_gmail_message

        raw, thread_id = _prepare_gmail_message(
            subject="Hi",
            body="Body",
            to="x@example.com",
            attachments=[
                {
                    "content": base64.b64encode(b"hello").decode(),
                    "filename": "good.txt",
                    # Slashless mime_type must not raise; it falls back to
                    # application/octet-stream.
                    "mime_type": "weird",
                }
            ],
        )
        assert raw
        assert thread_id is None

    def test_missing_filename_for_content_attachment_raises(self):
        from gmail.gmail_tools import _prepare_gmail_message

        with pytest.raises(ValueError, match="missing filename"):
            _prepare_gmail_message(
                subject="Hi",
                body="Body",
                attachments=[{"content": base64.b64encode(b"x").decode()}],
            )


# ---------------------------------------------------------------------------
# Fix 8: manage_gmail_label — update preserves current visibility
# ---------------------------------------------------------------------------


class TestManageGmailLabelVisibility:
    @pytest.mark.asyncio
    async def test_rename_only_update_preserves_hidden_visibility(self):
        from gmail.gmail_tools import manage_gmail_label

        impl = _unwrap(manage_gmail_label)

        service = MagicMock()
        labels = service.users.return_value.labels.return_value
        labels.get.return_value.execute.return_value = {
            "id": "Label_1",
            "name": "Old name",
            "labelListVisibility": "labelHide",
            "messageListVisibility": "hide",
        }
        labels.update.return_value.execute.return_value = {
            "id": "Label_1",
            "name": "New name",
        }

        await impl(
            service=service,
            user_google_email="u@example.com",
            action="update",
            label_id="Label_1",
            name="New name",
        )

        body = labels.update.call_args.kwargs["body"]
        assert body["name"] == "New name"
        assert body["labelListVisibility"] == "labelHide"
        assert body["messageListVisibility"] == "hide"

    @pytest.mark.asyncio
    async def test_explicit_visibility_still_applies_on_update(self):
        from gmail.gmail_tools import manage_gmail_label

        impl = _unwrap(manage_gmail_label)

        service = MagicMock()
        labels = service.users.return_value.labels.return_value
        labels.get.return_value.execute.return_value = {
            "id": "Label_1",
            "name": "Old name",
            "labelListVisibility": "labelHide",
            "messageListVisibility": "hide",
        }
        labels.update.return_value.execute.return_value = {
            "id": "Label_1",
            "name": "Old name",
        }

        await impl(
            service=service,
            user_google_email="u@example.com",
            action="update",
            label_id="Label_1",
            label_list_visibility="labelShow",
        )

        body = labels.update.call_args.kwargs["body"]
        assert body["labelListVisibility"] == "labelShow"
        assert body["messageListVisibility"] == "hide"

    @pytest.mark.asyncio
    async def test_create_defaults_remain_visible(self):
        from gmail.gmail_tools import manage_gmail_label

        impl = _unwrap(manage_gmail_label)

        service = MagicMock()
        labels = service.users.return_value.labels.return_value
        labels.create.return_value.execute.return_value = {
            "id": "Label_2",
            "name": "Fresh",
        }

        await impl(
            service=service,
            user_google_email="u@example.com",
            action="create",
            name="Fresh",
        )

        body = labels.create.call_args.kwargs["body"]
        assert body["labelListVisibility"] == "labelShow"
        assert body["messageListVisibility"] == "show"


# ---------------------------------------------------------------------------
# Fix 9: shared-drive files fall back to permissions().list
# ---------------------------------------------------------------------------


class TestSharedDrivePermissionsFallback:
    @pytest.mark.asyncio
    async def test_get_drive_file_permissions_lists_for_shared_drive_item(
        self, monkeypatch
    ):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.get_drive_file_permissions)

        monkeypatch.setattr(
            drive_tools,
            "resolve_drive_item",
            AsyncMock(return_value=("file-1", {})),
        )

        service = MagicMock()
        # Shared-drive file: driveId set, no permissions in files().get.
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "file-1",
            "name": "Shared doc",
            "mimeType": "application/vnd.google-apps.document",
            "shared": True,
            "driveId": "0AAbCdEf",
            "webViewLink": "https://docs.google.com/document/d/file-1",
        }
        # Two pages to exercise pageToken pagination.
        service.permissions.return_value.list.return_value.execute.side_effect = [
            {
                "permissions": [
                    {
                        "id": "perm-1",
                        "type": "user",
                        "role": "writer",
                        "emailAddress": "oliver@otbgroup.co.uk",
                    }
                ],
                "nextPageToken": "page-2",
            },
            {
                "permissions": [
                    {"id": "perm-2", "type": "anyone", "role": "reader"}
                ]
            },
        ]

        result = await impl(
            service=service,
            user_google_email="oliver@otbgroup.co.uk",
            file_id="file-1",
        )

        list_calls = service.permissions.return_value.list.call_args_list
        assert len(list_calls) == 2
        assert list_calls[0].kwargs["fileId"] == "file-1"
        assert list_calls[0].kwargs["supportsAllDrives"] is True
        assert "pageToken" not in list_calls[0].kwargs
        assert list_calls[1].kwargs["pageToken"] == "page-2"

        assert "Number of permissions: 2" in result
        assert "oliver@otbgroup.co.uk" in result
        assert "Anyone with the link" in result
        assert "No additional permissions (private file)" not in result

    @pytest.mark.asyncio
    async def test_get_drive_shareable_link_falls_back_when_shared_no_permissions(
        self, monkeypatch
    ):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.get_drive_shareable_link)

        monkeypatch.setattr(
            drive_tools,
            "resolve_drive_item",
            AsyncMock(return_value=("file-2", {})),
        )

        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "file-2",
            "name": "Team sheet",
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "shared": True,
            "driveId": "0AAbCdEf",
            "webViewLink": "https://docs.google.com/spreadsheets/d/file-2",
        }
        service.permissions.return_value.list.return_value.execute.return_value = {
            "permissions": [
                {
                    "id": "perm-9",
                    "type": "domain",
                    "role": "reader",
                    "domain": "otbgroup.co.uk",
                }
            ]
        }

        result = await impl(
            service=service,
            user_google_email="oliver@otbgroup.co.uk",
            file_id="file-2",
        )

        service.permissions.return_value.list.assert_called_once()
        assert "Current permissions:" in result
        assert "otbgroup.co.uk" in result

    @pytest.mark.asyncio
    async def test_my_drive_file_with_permissions_skips_fallback(self, monkeypatch):
        from gdrive import drive_tools

        impl = _unwrap(drive_tools.get_drive_file_permissions)

        monkeypatch.setattr(
            drive_tools,
            "resolve_drive_item",
            AsyncMock(return_value=("file-3", {})),
        )

        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "file-3",
            "name": "My doc",
            "mimeType": "application/vnd.google-apps.document",
            "shared": False,
            "permissions": [
                {
                    "id": "perm-owner",
                    "type": "user",
                    "role": "owner",
                    "emailAddress": "oliver@otbgroup.co.uk",
                }
            ],
            "webViewLink": "https://docs.google.com/document/d/file-3",
        }

        result = await impl(
            service=service,
            user_google_email="oliver@otbgroup.co.uk",
            file_id="file-3",
        )

        service.permissions.return_value.list.assert_not_called()
        assert "Number of permissions: 1" in result

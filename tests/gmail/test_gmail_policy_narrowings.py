"""Gmail policy narrowings (adversarial review tier 3, items 2, 4, 7 and 8).

Item 2, create_gmail_filter: the action and criteria are validated before the
API call. A "forward" key, TRASH or SPAM in addLabelIds, empty criteria,
criteria with no specific field, and bare-wildcard criteria are refused. A
valid filter passes through with the body unchanged.

Item 4, modify_gmail_message_labels: TRASH and SPAM in add_label_ids are
refused; removing INBOX (archive) still works.

Item 7, manage_gmail_label: action="delete" needs confirm=True.

Item 8, send_gmail_message and draft_gmail_message: from_name must match the
display name Gmail holds for the sending address (users.settings.sendAs.get)
after case and whitespace normalisation. An empty display name, a mismatch,
and an unknown send-as address are all refused before anything is sent.

The Gmail service is a MagicMock; nothing touches the network.
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from core.utils import UserInputError
from gmail import gmail_tools

USER = "oliver@otbgroup.co.uk"


def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


create_gmail_filter = _unwrap(gmail_tools.create_gmail_filter)
modify_gmail_message_labels = _unwrap(gmail_tools.modify_gmail_message_labels)
manage_gmail_label = _unwrap(gmail_tools.manage_gmail_label)
send_gmail_message = _unwrap(gmail_tools.send_gmail_message)
draft_gmail_message = _unwrap(gmail_tools.draft_gmail_message)


def _http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    return HttpError(resp, b'{"error": {"message": "x"}}')


# ---------------------------------------------------------------------------
# Item 2: create_gmail_filter
# ---------------------------------------------------------------------------


def _filter_service(filter_id: str = "flt1") -> MagicMock:
    service = MagicMock()
    service.users().settings().filters().create().execute.return_value = {
        "id": filter_id
    }
    service.users.reset_mock()
    return service


def _create_calls(service: MagicMock):
    return [
        c
        for c in service.users.return_value.settings.return_value.filters.return_value.create.call_args_list
        if c.kwargs.get("userId") is not None
    ]


class TestCreateGmailFilterRefusals:
    @pytest.mark.asyncio
    async def test_forward_action_refused(self):
        service = _filter_service()
        with pytest.raises(UserInputError) as excinfo:
            await create_gmail_filter(
                service,
                USER,
                criteria={"from": "boss@otbgroup.co.uk"},
                action={"forward": "attacker@example.com"},
            )
        message = str(excinfo.value)
        assert "Refused" in message
        assert "'forward'" in message
        assert "exfiltration" in message
        assert _create_calls(service) == []

    @pytest.mark.parametrize("label", ["TRASH", "SPAM", "trash"])
    @pytest.mark.asyncio
    async def test_trash_and_spam_actions_refused(self, label):
        service = _filter_service()
        with pytest.raises(UserInputError) as excinfo:
            await create_gmail_filter(
                service,
                USER,
                criteria={"from": "newsletter@example.com"},
                action={"addLabelIds": [label]},
            )
        message = str(excinfo.value)
        assert "Refused" in message
        assert label in message
        assert "removing INBOX" in message
        assert _create_calls(service) == []

    @pytest.mark.parametrize("criteria", [{}, None, "from:x"])
    @pytest.mark.asyncio
    async def test_empty_or_non_dict_criteria_refused(self, criteria):
        service = _filter_service()
        with pytest.raises(UserInputError, match="non-empty criteria dict"):
            await create_gmail_filter(
                service, USER, criteria=criteria, action={"addLabelIds": ["L1"]}
            )
        assert _create_calls(service) == []

    @pytest.mark.asyncio
    async def test_criteria_without_a_specific_field_refused(self):
        service = _filter_service()
        with pytest.raises(UserInputError) as excinfo:
            await create_gmail_filter(
                service,
                USER,
                criteria={"excludeChats": True, "from": ""},
                action={"addLabelIds": ["L1"]},
            )
        message = str(excinfo.value)
        assert "must include at least one of" in message
        assert "every future message" in message
        assert _create_calls(service) == []

    @pytest.mark.parametrize(
        "criteria",
        [
            {"query": "*"},
            {"from": "@"},
            {"from": " * ", "to": "@"},
            {"subject": "..."},
        ],
    )
    @pytest.mark.asyncio
    async def test_bare_wildcard_criteria_refused(self, criteria):
        service = _filter_service()
        with pytest.raises(UserInputError) as excinfo:
            await create_gmail_filter(
                service, USER, criteria=criteria, action={"addLabelIds": ["L1"]}
            )
        message = str(excinfo.value)
        assert "bare wildcard" in message
        assert "Narrow the criteria" in message
        assert _create_calls(service) == []

    @pytest.mark.parametrize("action", [{}, None, ["addLabelIds"]])
    @pytest.mark.asyncio
    async def test_empty_or_non_dict_action_refused(self, action):
        service = _filter_service()
        with pytest.raises(UserInputError, match="non-empty action dict"):
            await create_gmail_filter(
                service, USER, criteria={"from": "a@b.com"}, action=action
            )
        assert _create_calls(service) == []


class TestCreateGmailFilterPassThrough:
    @pytest.mark.asyncio
    async def test_valid_filter_body_is_unchanged(self):
        service = _filter_service("flt42")
        criteria = {"from": "newsletter@example.com", "hasAttachment": False}
        action = {"addLabelIds": ["Label_7"], "removeLabelIds": ["INBOX"]}

        result = await create_gmail_filter(
            service, USER, criteria=criteria, action=action
        )

        assert "Filter ID: flt42" in result
        (call,) = _create_calls(service)
        assert call.kwargs == {
            "userId": "me",
            "body": {"criteria": criteria, "action": action},
        }

    @pytest.mark.asyncio
    async def test_wildcard_text_with_a_non_text_criterion_is_allowed(self):
        """query '*' plus hasAttachment=True is a real filter (all attachments)."""
        service = _filter_service()
        await create_gmail_filter(
            service,
            USER,
            criteria={"query": "*", "hasAttachment": True},
            action={"addLabelIds": ["Label_1"]},
        )
        assert len(_create_calls(service)) == 1

    @pytest.mark.asyncio
    async def test_other_label_ids_are_allowed_alongside_archive(self):
        service = _filter_service()
        await create_gmail_filter(
            service,
            USER,
            criteria={"subject": "Invoice"},
            action={
                "addLabelIds": ["STARRED", "IMPORTANT"],
                "removeLabelIds": ["INBOX"],
            },
        )
        assert len(_create_calls(service)) == 1


class TestDeleteGmailFilterUnblocked:
    def test_delete_gmail_filter_is_registered(self):
        import gmail.gmail_tools  # noqa: F401  (registers tools)
        from core.server import server
        from core.tool_registry import get_tool_components

        assert "delete_gmail_filter" in get_tool_components(server)


# ---------------------------------------------------------------------------
# Item 4: modify_gmail_message_labels
# ---------------------------------------------------------------------------


def _modify_calls(service: MagicMock):
    return [
        c
        for c in service.users.return_value.messages.return_value.modify.call_args_list
        if c.kwargs.get("id") is not None
    ]


class TestModifyGmailMessageLabels:
    @pytest.mark.parametrize("label", ["TRASH", "SPAM", "Trash"])
    @pytest.mark.asyncio
    async def test_trash_and_spam_refused(self, label):
        service = MagicMock()
        with pytest.raises(UserInputError) as excinfo:
            await modify_gmail_message_labels(
                service, USER, message_id="m1", add_label_ids=[label]
            )
        message = str(excinfo.value)
        assert "Refused" in message
        assert label in message
        assert "remove_label_ids=['INBOX']" in message
        assert "Gmail UI" in message
        assert _modify_calls(service) == []

    @pytest.mark.asyncio
    async def test_trash_mixed_with_other_labels_still_refused(self):
        service = MagicMock()
        with pytest.raises(UserInputError, match="Refused"):
            await modify_gmail_message_labels(
                service, USER, "m1", add_label_ids=["STARRED", "TRASH"]
            )
        assert _modify_calls(service) == []

    @pytest.mark.asyncio
    async def test_archive_by_removing_inbox_still_works(self):
        service = MagicMock()
        service.users().messages().modify().execute.return_value = {}
        service.users.reset_mock()

        result = await modify_gmail_message_labels(
            service, USER, "m1", add_label_ids=["Label_3"], remove_label_ids=["INBOX"]
        )

        assert "Message labels updated successfully" in result
        (call,) = _modify_calls(service)
        assert call.kwargs["body"] == {
            "addLabelIds": ["Label_3"],
            "removeLabelIds": ["INBOX"],
        }

    @pytest.mark.asyncio
    async def test_removing_trash_is_allowed(self):
        """Taking a message out of the trash is a restore, not a delete."""
        service = MagicMock()
        service.users().messages().modify().execute.return_value = {}
        service.users.reset_mock()

        await modify_gmail_message_labels(
            service, USER, "m1", remove_label_ids=["TRASH"]
        )

        assert len(_modify_calls(service)) == 1

    def test_docstring_no_longer_advertises_trash(self):
        doc = gmail_tools.modify_gmail_message_labels.__doc__ or ""
        assert "add the TRASH label" not in doc
        assert "refused" in doc


# ---------------------------------------------------------------------------
# Item 7: manage_gmail_label delete needs confirm
# ---------------------------------------------------------------------------


class TestManageGmailLabelDeleteConfirm:
    @pytest.mark.asyncio
    async def test_delete_without_confirm_refused(self):
        service = MagicMock()
        with pytest.raises(UserInputError) as excinfo:
            await manage_gmail_label(service, USER, action="delete", label_id="Label_9")
        message = str(excinfo.value)
        assert "Refused" in message
        assert "'Label_9'" in message
        assert "confirm=True" in message
        assert "labelHide" in message
        service.users.return_value.labels.return_value.delete.assert_not_called()
        service.users.return_value.labels.return_value.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_with_confirm_proceeds(self):
        service = MagicMock()
        service.users().labels().get().execute.return_value = {"name": "Old"}
        service.users().labels().delete().execute.return_value = {}
        service.users.reset_mock()

        result = await manage_gmail_label(
            service, USER, action="delete", label_id="Label_9", confirm=True
        )

        assert "deleted successfully" in result
        delete_calls = [
            c
            for c in service.users.return_value.labels.return_value.delete.call_args_list
            if c.kwargs.get("id") == "Label_9"
        ]
        assert len(delete_calls) == 1

    @pytest.mark.asyncio
    async def test_create_and_update_need_no_confirm(self):
        service = MagicMock()
        service.users().labels().create().execute.return_value = {
            "name": "New",
            "id": "Label_1",
        }
        service.users().labels().get().execute.return_value = {"name": "New"}
        service.users().labels().update().execute.return_value = {
            "name": "Renamed",
            "id": "Label_1",
        }

        created = await manage_gmail_label(service, USER, action="create", name="New")
        updated = await manage_gmail_label(
            service, USER, action="update", label_id="Label_1", name="Renamed"
        )

        assert "Label created successfully" in created
        assert "Label updated successfully" in updated


# ---------------------------------------------------------------------------
# Item 8: from_name must match the send-as display name
# ---------------------------------------------------------------------------


def _mail_service(display_name, send_as_error=None) -> MagicMock:
    service = MagicMock()
    get_request = service.users().settings().sendAs().get()
    if send_as_error is not None:
        get_request.execute.side_effect = send_as_error
    else:
        get_request.execute.return_value = {
            "sendAsEmail": USER,
            "displayName": display_name,
        }
    service.users().messages().send().execute.return_value = {"id": "msg1"}
    service.users().drafts().create().execute.return_value = {"id": "draft1"}
    service.users.reset_mock()
    return service


def _send_calls(service: MagicMock):
    return [
        c
        for c in service.users.return_value.messages.return_value.send.call_args_list
        if c.kwargs.get("userId") is not None
    ]


def _draft_calls(service: MagicMock):
    return [
        c
        for c in service.users.return_value.drafts.return_value.create.call_args_list
        if c.kwargs.get("userId") is not None
    ]


def _send_as_get_calls(service: MagicMock):
    return [
        c
        for c in service.users.return_value.settings.return_value.sendAs.return_value.get.call_args_list
        if c.kwargs.get("sendAsEmail") is not None
    ]


def _from_header(raw: str) -> str:
    padded = raw + "=" * (-len(raw) % 4)
    text = base64.urlsafe_b64decode(padded).decode("utf-8")
    for line in text.splitlines():
        if line.startswith("From:"):
            return line[len("From:") :].strip()
    raise AssertionError("no From header")


class TestFromNameMatchesSendAs:
    @pytest.mark.asyncio
    async def test_matching_name_is_accepted(self):
        service = _mail_service("Oliver Blake")

        result = await send_gmail_message(
            service,
            USER,
            to="a@b.com",
            subject="Hi",
            body="x",
            from_name="Oliver Blake",
        )

        assert "Email sent" in result
        (call,) = _send_calls(service)
        assert _from_header(call.kwargs["body"]["raw"]) == f"Oliver Blake <{USER}>"
        (lookup,) = _send_as_get_calls(service)
        assert lookup.kwargs == {"userId": "me", "sendAsEmail": USER}

    @pytest.mark.asyncio
    async def test_case_and_whitespace_differences_are_tolerated(self):
        service = _mail_service("Oliver Blake")

        await send_gmail_message(
            service,
            USER,
            to="a@b.com",
            subject="Hi",
            body="x",
            from_name="  oliver   BLAKE ",
        )

        assert len(_send_calls(service)) == 1

    @pytest.mark.asyncio
    async def test_different_name_is_refused_before_send(self):
        service = _mail_service("Oliver Blake")

        with pytest.raises(UserInputError) as excinfo:
            await send_gmail_message(
                service,
                USER,
                to="a@b.com",
                subject="Hi",
                body="x",
                from_name="Emily Blake",
            )

        message = str(excinfo.value)
        assert "Refused" in message
        assert "'Emily Blake'" in message
        assert "'Oliver Blake'" in message
        assert "own name" in message
        assert _send_calls(service) == []

    @pytest.mark.asyncio
    async def test_empty_display_name_is_refused_with_settings_hint(self):
        service = _mail_service("")

        with pytest.raises(UserInputError) as excinfo:
            await send_gmail_message(
                service,
                USER,
                to="a@b.com",
                subject="Hi",
                body="x",
                from_name="Oliver Blake",
            )

        message = str(excinfo.value)
        assert "Refused" in message
        assert "no display name" in message
        assert "Gmail settings" in message
        assert _send_calls(service) == []

    @pytest.mark.asyncio
    async def test_unknown_send_as_address_is_refused(self):
        service = _mail_service(None, send_as_error=_http_error(404))

        with pytest.raises(UserInputError) as excinfo:
            await send_gmail_message(
                service,
                USER,
                to="a@b.com",
                subject="Hi",
                body="x",
                from_name="Oliver Blake",
                from_email="other@otbgroup.co.uk",
            )

        assert "not one of this account's send-as addresses" in str(excinfo.value)
        (lookup,) = _send_as_get_calls(service)
        assert lookup.kwargs["sendAsEmail"] == "other@otbgroup.co.uk"
        assert _send_calls(service) == []

    @pytest.mark.asyncio
    async def test_other_send_as_errors_propagate(self):
        service = _mail_service(None, send_as_error=_http_error(500))

        with pytest.raises(HttpError):
            await send_gmail_message(
                service,
                USER,
                to="a@b.com",
                subject="Hi",
                body="x",
                from_name="Oliver Blake",
            )

        assert _send_calls(service) == []

    @pytest.mark.asyncio
    async def test_no_from_name_makes_no_send_as_lookup(self):
        service = _mail_service("Oliver Blake")

        result = await send_gmail_message(
            service, USER, to="a@b.com", subject="Hi", body="x"
        )

        assert "Email sent" in result
        assert _send_as_get_calls(service) == []
        (call,) = _send_calls(service)
        assert _from_header(call.kwargs["body"]["raw"]) == USER

    @pytest.mark.asyncio
    async def test_alias_is_checked_against_its_own_send_as_entry(self):
        service = _mail_service("OTB Group")

        await send_gmail_message(
            service,
            USER,
            to="a@b.com",
            subject="Hi",
            body="x",
            from_name="OTB Group",
            from_email="otb@otbgroup.co.uk",
        )

        (lookup,) = _send_as_get_calls(service)
        assert lookup.kwargs["sendAsEmail"] == "otb@otbgroup.co.uk"
        assert len(_send_calls(service)) == 1


class TestDraftFromNameMatchesSendAs:
    @pytest.mark.asyncio
    async def test_draft_with_matching_name_is_created(self):
        service = _mail_service("Oliver Blake")

        result = await draft_gmail_message(
            service,
            USER,
            subject="Hi",
            body="x",
            to="a@b.com",
            from_name="Oliver Blake",
        )

        assert "Draft created" in result
        (call,) = _draft_calls(service)
        raw = call.kwargs["body"]["message"]["raw"]
        assert _from_header(raw) == f"Oliver Blake <{USER}>"

    @pytest.mark.asyncio
    async def test_draft_with_different_name_is_refused(self):
        service = _mail_service("Oliver Blake")

        with pytest.raises(UserInputError, match="does not match the display name"):
            await draft_gmail_message(
                service,
                USER,
                subject="Hi",
                body="x",
                to="a@b.com",
                from_name="Someone Else",
            )

        assert _draft_calls(service) == []

    @pytest.mark.asyncio
    async def test_draft_with_empty_display_name_is_refused(self):
        service = _mail_service("")

        with pytest.raises(UserInputError, match="no display name"):
            await draft_gmail_message(
                service,
                USER,
                subject="Hi",
                body="x",
                to="a@b.com",
                from_name="Oliver Blake",
            )

        assert _draft_calls(service) == []

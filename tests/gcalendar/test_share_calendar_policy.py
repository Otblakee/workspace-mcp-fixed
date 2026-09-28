"""share_calendar policy narrowing (adversarial review tier 3, item 1).

1. Role "owner" is refused: an owner grant hands the calendar to another
   account and there is no tool on this server to take it back.
2. When DRIVE_PERMISSION_ALLOWED_DOMAINS is set (the same allowlist the Drive
   permission tools read through gdrive.drive_batch), an address outside it
   is refused before the ACL insert.
3. An internal writer grant goes through unchanged.
4. With the allowlist unset, any domain is accepted as before.

The Calendar service is a MagicMock; nothing touches the network.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.utils import UserInputError
from gcalendar import calendar_tools

USER = "oliver@otbgroup.co.uk"


def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


share_calendar = _unwrap(calendar_tools.share_calendar)


def _service(rule_id: str = "rule1", role: str = "writer") -> MagicMock:
    service = MagicMock()
    service.acl().insert().execute.return_value = {"id": rule_id, "role": role}
    service.acl.reset_mock()
    return service


def _insert_calls(service: MagicMock):
    return [
        c
        for c in service.acl.return_value.insert.call_args_list
        if c.kwargs.get("calendarId") is not None
    ]


class TestOwnerRefused:
    @pytest.mark.asyncio
    async def test_owner_role_is_refused_before_any_api_call(self, monkeypatch):
        monkeypatch.delenv("DRIVE_PERMISSION_ALLOWED_DOMAINS", raising=False)
        service = _service()

        with pytest.raises(UserInputError) as excinfo:
            await share_calendar(
                service,
                USER,
                calendar_id="cal1",
                share_with_email="colleague@otbgroup.co.uk",
                role="owner",
            )

        message = str(excinfo.value)
        assert "Refused" in message
        assert "'owner'" in message
        assert "no tool on this server to take it back" in message
        assert "reader" in message and "writer" in message
        assert _insert_calls(service) == []

    @pytest.mark.asyncio
    async def test_unknown_role_still_reports_the_allowed_set(self, monkeypatch):
        monkeypatch.delenv("DRIVE_PERMISSION_ALLOWED_DOMAINS", raising=False)
        service = _service()

        result = await share_calendar(
            service, USER, "cal1", "colleague@otbgroup.co.uk", role="editor"
        )

        assert "Invalid role 'editor'" in result
        assert "owner" not in result
        assert _insert_calls(service) == []


class TestDomainAllowlist:
    @pytest.mark.asyncio
    async def test_external_address_refused_when_allowlist_set(self, monkeypatch):
        monkeypatch.setenv(
            "DRIVE_PERMISSION_ALLOWED_DOMAINS", "otbgroup.co.uk, jit-logistics.com"
        )
        service = _service()

        with pytest.raises(UserInputError) as excinfo:
            await share_calendar(
                service, USER, "cal1", "someone@gmail.com", role="reader"
            )

        message = str(excinfo.value)
        assert "Refused" in message
        assert "'gmail.com'" in message
        assert "otbgroup.co.uk, jit-logistics.com" in message
        assert "DRIVE_PERMISSION_ALLOWED_DOMAINS" in message
        assert _insert_calls(service) == []

    @pytest.mark.asyncio
    async def test_internal_writer_allowed_when_allowlist_set(self, monkeypatch):
        monkeypatch.setenv("DRIVE_PERMISSION_ALLOWED_DOMAINS", "otbgroup.co.uk")
        service = _service(role="writer")

        result = await share_calendar(
            service, USER, "cal1", "Colleague@OTBGroup.co.uk", role="writer"
        )

        assert "Successfully shared calendar 'cal1'" in result
        assert "Role: writer" in result
        (call,) = _insert_calls(service)
        assert call.kwargs["body"] == {
            "scope": {"type": "user", "value": "Colleague@OTBGroup.co.uk"},
            "role": "writer",
        }

    @pytest.mark.asyncio
    async def test_any_domain_accepted_when_allowlist_unset(self, monkeypatch):
        monkeypatch.delenv("DRIVE_PERMISSION_ALLOWED_DOMAINS", raising=False)
        service = _service(role="reader")

        result = await share_calendar(
            service, USER, "cal1", "someone@gmail.com", role="reader"
        )

        assert "Successfully shared calendar 'cal1'" in result
        (call,) = _insert_calls(service)
        assert call.kwargs["body"]["scope"]["value"] == "someone@gmail.com"

    @pytest.mark.asyncio
    async def test_allowlist_helper_is_the_drive_one(self):
        """The calendar tool reuses gdrive.drive_batch's helper, not a copy."""
        from gdrive import drive_batch

        assert (
            calendar_tools._allowed_permission_domains
            is drive_batch._allowed_permission_domains
        )

    @pytest.mark.asyncio
    async def test_non_email_address_refused(self, monkeypatch):
        monkeypatch.delenv("DRIVE_PERMISSION_ALLOWED_DOMAINS", raising=False)
        service = _service()

        with pytest.raises(UserInputError, match="must be an email address"):
            await share_calendar(service, USER, "cal1", "not-an-email", role="reader")

        assert _insert_calls(service) == []

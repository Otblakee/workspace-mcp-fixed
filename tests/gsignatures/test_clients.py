"""Unit tests for the thin Gmail / Directory client wrappers.

Covers:

* ``list_send_as`` returns the primary address first;
* ``get_send_as`` passes ``userId='me'`` and the alias;
* ``patch_signature`` sends exactly ``{'signature': html}`` and returns the
  read-back from ``get``, never the patch response;
* ``list_directory_users`` builds the OU query, adds ``isSuspended=false`` by
  default, switches to ``domain`` when given, and drains two pages;
* ``list_group_member_emails`` keeps USER members only and lower-cases.

All fakes expose the googleapiclient call shape
(``service.users().settings().sendAs().patch(...).execute()``) so the
wrappers are exercised end to end, with ``execute`` run in a worker thread
exactly as it is in production.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gsignatures import clients  # noqa: E402


def _request(result):
    request = MagicMock()
    if isinstance(result, Exception):
        request.execute.side_effect = result
    else:
        request.execute.return_value = result
    return request


PRIMARY = {
    "sendAsEmail": "alice@otbgroup.co.uk",
    "isPrimary": True,
    "isDefault": True,
    "displayName": "Alice",
    "signature": "<div>old primary</div>",
}
ALIAS = {
    "sendAsEmail": "alice@jit-logistics.com",
    "isPrimary": False,
    "isDefault": False,
    "displayName": "Alice",
    "signature": "",
    "treatAsAlias": True,
    "verificationStatus": "accepted",
}


class FakeGmail:
    """Gmail double keyed by sendAsEmail. ``patch`` stores the new signature."""

    def __init__(self, send_as=None):
        self.calls = []
        self.send_as = {s["sendAsEmail"]: dict(s) for s in (send_as or [])}
        # Real Gmail sanitises on save. Mimic that so the read-back differs
        # from what was sent and the tests can tell the two apart.
        self.sanitise = lambda html: html.replace("<!-- c -->", "")

    def users(self):
        parent = self

        class _SendAs:
            def list(self, **kwargs):
                parent.calls.append(("sendAs.list", kwargs))
                # Alias first on purpose: the wrapper must reorder.
                ordered = sorted(
                    parent.send_as.values(), key=lambda s: bool(s.get("isPrimary"))
                )
                return _request({"sendAs": [dict(s) for s in ordered]})

            def get(self, **kwargs):
                parent.calls.append(("sendAs.get", kwargs))
                return _request(dict(parent.send_as[kwargs["sendAsEmail"]]))

            def patch(self, **kwargs):
                parent.calls.append(("sendAs.patch", kwargs))
                entry = parent.send_as[kwargs["sendAsEmail"]]
                entry["signature"] = parent.sanitise(kwargs["body"]["signature"])
                # The patch response is deliberately different from the
                # stored resource so a wrapper returning it would be caught.
                return _request({"sendAsEmail": kwargs["sendAsEmail"], "patched": True})

        class _Settings:
            def sendAs(self):
                return _SendAs()

        class _Users:
            def settings(self):
                return _Settings()

        return _Users()


class FakeDirectory:
    """Admin Directory double with two-page users.list and members.list."""

    def __init__(self):
        self.calls = []
        self.user_pages = [
            {"users": [{"primaryEmail": "a@otbgroup.co.uk"}], "nextPageToken": "p2"},
            {"users": [{"primaryEmail": "b@otbgroup.co.uk"}]},
        ]
        self.user = {"primaryEmail": "a@otbgroup.co.uk", "name": {"fullName": "A"}}
        self.member_pages = [
            {
                "members": [
                    {"email": "Alice@OTBGroup.co.uk", "type": "USER"},
                    {"email": "nested@otbgroup.co.uk", "type": "GROUP"},
                ],
                "nextPageToken": "m2",
            },
            {
                "members": [
                    {"email": "bob@otbgroup.co.uk", "type": "USER"},
                    {"email": "cust@example.com", "type": "CUSTOMER"},
                    {"type": "USER"},
                ]
            },
        ]

    def users(self):
        parent = self

        class _Users:
            def get(self, **kwargs):
                parent.calls.append(("users.get", kwargs))
                return _request(dict(parent.user))

            def list(self, **kwargs):
                parent.calls.append(("users.list", kwargs))
                index = 1 if kwargs.get("pageToken") == "p2" else 0
                return _request(dict(parent.user_pages[index]))

        return _Users()

    def members(self):
        parent = self

        class _Members:
            def list(self, **kwargs):
                parent.calls.append(("members.list", kwargs))
                index = 1 if kwargs.get("pageToken") == "m2" else 0
                return _request(dict(parent.member_pages[index]))

        return _Members()


class TestSendAs:
    @pytest.mark.asyncio
    async def test_list_send_as_returns_primary_first(self):
        gmail = FakeGmail([PRIMARY, ALIAS])
        result = await clients.list_send_as(gmail)
        assert [s["sendAsEmail"] for s in result] == [
            "alice@otbgroup.co.uk",
            "alice@jit-logistics.com",
        ]
        assert gmail.calls == [("sendAs.list", {"userId": "me"})]

    @pytest.mark.asyncio
    async def test_list_send_as_empty_response(self):
        gmail = FakeGmail([])
        assert await clients.list_send_as(gmail) == []

    @pytest.mark.asyncio
    async def test_get_send_as(self):
        gmail = FakeGmail([PRIMARY, ALIAS])
        result = await clients.get_send_as(gmail, "alice@jit-logistics.com")
        assert result["sendAsEmail"] == "alice@jit-logistics.com"
        assert gmail.calls == [
            ("sendAs.get", {"userId": "me", "sendAsEmail": "alice@jit-logistics.com"})
        ]

    @pytest.mark.asyncio
    async def test_patch_signature_sends_only_the_signature_and_returns_readback(
        self,
    ):
        gmail = FakeGmail([PRIMARY, ALIAS])
        html = "<table><tr><td>Alice<!-- c --></td></tr></table>"
        result = await clients.patch_signature(gmail, "alice@jit-logistics.com", html)

        assert gmail.calls[0] == (
            "sendAs.patch",
            {
                "userId": "me",
                "sendAsEmail": "alice@jit-logistics.com",
                "body": {"signature": html},
            },
        )
        assert gmail.calls[1] == (
            "sendAs.get",
            {"userId": "me", "sendAsEmail": "alice@jit-logistics.com"},
        )
        # The read-back, not the patch response.
        assert "patched" not in result
        assert result["signature"] == "<table><tr><td>Alice</td></tr></table>"
        assert result["sendAsEmail"] == "alice@jit-logistics.com"

    @pytest.mark.asyncio
    async def test_patch_signature_refuses_blank_alias(self):
        gmail = FakeGmail([PRIMARY])
        with pytest.raises(ValueError):
            await clients.patch_signature(gmail, "", "<p>x</p>")
        assert gmail.calls == []

    @pytest.mark.asyncio
    async def test_patch_signature_refuses_non_string_html(self):
        gmail = FakeGmail([PRIMARY])
        with pytest.raises(ValueError):
            await clients.patch_signature(gmail, "alice@otbgroup.co.uk", None)
        assert gmail.calls == []


class TestDirectory:
    @pytest.mark.asyncio
    async def test_get_directory_user_uses_full_projection(self):
        directory = FakeDirectory()
        result = await clients.get_directory_user(directory, "a@otbgroup.co.uk")
        assert result["primaryEmail"] == "a@otbgroup.co.uk"
        assert directory.calls == [
            ("users.get", {"userKey": "a@otbgroup.co.uk", "projection": "full"})
        ]

    @pytest.mark.asyncio
    async def test_list_directory_users_ou_query_and_suspended_default(self):
        directory = FakeDirectory()
        result = await clients.list_directory_users(directory, ou_path="/01 OTB")

        assert [u["primaryEmail"] for u in result] == [
            "a@otbgroup.co.uk",
            "b@otbgroup.co.uk",
        ]
        assert len(directory.calls) == 2
        first = directory.calls[0][1]
        assert first["customer"] == "my_customer"
        assert "domain" not in first
        assert first["projection"] == "full"
        assert "orgUnitPath='/01 OTB'" in first["query"]
        assert "isSuspended=false" in first["query"]
        assert "pageToken" not in first or first["pageToken"] is None
        second = directory.calls[1][1]
        assert second["pageToken"] == "p2"
        assert second["query"] == first["query"]

    @pytest.mark.asyncio
    async def test_list_directory_users_include_suspended_drops_clause(self):
        directory = FakeDirectory()
        await clients.list_directory_users(
            directory, ou_path="/02 JIT", include_suspended=True
        )
        query = directory.calls[0][1]["query"]
        assert "isSuspended" not in query
        assert "orgUnitPath='/02 JIT'" in query

    @pytest.mark.asyncio
    async def test_list_directory_users_combines_caller_query(self):
        directory = FakeDirectory()
        await clients.list_directory_users(
            directory, ou_path="/01 OTB", query="email:a*"
        )
        query = directory.calls[0][1]["query"]
        assert "orgUnitPath='/01 OTB'" in query
        assert "email:a*" in query
        assert "isSuspended=false" in query

    @pytest.mark.asyncio
    async def test_list_directory_users_domain_replaces_customer(self):
        directory = FakeDirectory()
        await clients.list_directory_users(directory, domain="jit-logistics.com")
        params = directory.calls[0][1]
        assert params["domain"] == "jit-logistics.com"
        assert "customer" not in params
        assert params["query"] == "isSuspended=false"

    @pytest.mark.asyncio
    async def test_list_directory_users_no_filters_at_all(self):
        directory = FakeDirectory()
        await clients.list_directory_users(directory, include_suspended=True)
        params = directory.calls[0][1]
        assert "query" not in params
        assert params["maxResults"] == 500

    @pytest.mark.asyncio
    async def test_list_directory_users_caps_page_size_at_api_limit(self):
        directory = FakeDirectory()
        await clients.list_directory_users(directory, max_results=5000)
        assert directory.calls[0][1]["maxResults"] == 500
        with pytest.raises(ValueError):
            await clients.list_directory_users(directory, max_results=0)

    @pytest.mark.asyncio
    async def test_list_directory_users_rejects_bad_ou_path(self):
        directory = FakeDirectory()
        with pytest.raises(ValueError):
            await clients.list_directory_users(directory, ou_path="01 OTB")
        with pytest.raises(ValueError):
            await clients.list_directory_users(directory, ou_path="/01 'OTB'")
        assert directory.calls == []

    @pytest.mark.asyncio
    async def test_list_group_member_emails_users_only_lower_cased(self):
        directory = FakeDirectory()
        result = await clients.list_group_member_emails(
            directory, "staff@otbgroup.co.uk"
        )
        assert result == ["alice@otbgroup.co.uk", "bob@otbgroup.co.uk"]
        assert len(directory.calls) == 2
        assert directory.calls[0][1]["groupKey"] == "staff@otbgroup.co.uk"
        assert directory.calls[1][1]["pageToken"] == "m2"

    @pytest.mark.asyncio
    async def test_list_group_member_emails_refuses_blank_group(self):
        directory = FakeDirectory()
        with pytest.raises(ValueError):
            await clients.list_group_member_emails(directory, " ")
        assert directory.calls == []

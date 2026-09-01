"""The gadmin_write tools must refuse to edit the groups that decide MCP
access (core/group_policy.yaml). Otherwise anyone allowed add_group_member
could add themselves to mcp-admins and escalate."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import access_policy as ap  # noqa: E402
from core.utils import UserInputError  # noqa: E402
from gadmin import admin_group_tools as groups  # noqa: E402

USER = "oliver@otbgroup.co.uk"
ADMINS = "mcp-admins@otbgroup.co.uk"


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


@pytest.fixture(autouse=True)
def _fresh_engine(monkeypatch):
    monkeypatch.delenv(ap.MODE_ENV, raising=False)
    monkeypatch.delenv(ap.FILE_ENV, raising=False)
    ap.set_engine(None)
    yield
    ap.set_engine(None)


class TestGuardHelper:
    def test_policy_group_refused(self):
        with pytest.raises(UserInputError, match="access-policy group"):
            groups._refuse_if_policy_group(ADMINS)

    def test_other_group_allowed(self):
        groups._refuse_if_policy_group("drivers@otbgroup.co.uk")


class TestToolsRefuseBeforeAnyDirectoryCall:
    @pytest.mark.asyncio
    async def test_add_member(self):
        service = MagicMock()
        with pytest.raises(UserInputError, match="Admin console"):
            await _unwrap(groups.add_group_member)(
                service,
                USER,
                group_email=ADMINS.upper(),
                member_email="x@otbgroup.co.uk",
            )
        service.groups.assert_not_called()
        service.members.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_member(self):
        service = MagicMock()
        with pytest.raises(UserInputError, match="Admin console"):
            await _unwrap(groups.remove_group_member)(
                service, USER, group_email=ADMINS, member_email="x@otbgroup.co.uk"
            )
        service.groups.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_group(self):
        service = MagicMock()
        with pytest.raises(UserInputError, match="Admin console"):
            await _unwrap(groups.create_group)(service, USER, email=ADMINS)
        service.groups.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_is_refused_too(self):
        service = MagicMock()
        with pytest.raises(UserInputError):
            await _unwrap(groups.add_group_member)(
                service,
                USER,
                group_email=ADMINS,
                member_email="x@otbgroup.co.uk",
                dry_run=True,
            )

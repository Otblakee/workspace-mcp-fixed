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


def _request(result):
    request = MagicMock()
    if isinstance(result, Exception):
        request.execute.side_effect = result
    else:
        request.execute.return_value = result
    return request


def _http_error(status):
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    return HttpError(resp, b"{}")


class _Directory:
    """Directory double: groups.get resolves aliases; members.list walks nesting."""

    def __init__(
        self, groups, members_by_group=None, list_error=None, memberships=None
    ):
        # groups: {lookup_key: group_resource}
        self.groups_map = groups
        self.members_by_group = members_by_group or {}
        self.list_error = list_error
        # memberships: {(group_email, member_email): role} answered by members.get
        self.memberships = memberships or {}
        self.mutations = []

    def groups(self):
        outer = self

        class _Groups:
            def get(self, groupKey, fields=None):
                g = outer.groups_map.get(groupKey)
                return _request(g if g is not None else _http_error(404))

        return _Groups()

    def members(self):
        outer = self

        class _Members:
            def list(self, groupKey, maxResults=200, pageToken=None):
                if outer.list_error is not None and groupKey in outer.list_error:
                    return _request(outer.list_error[groupKey])
                if groupKey not in outer.members_by_group:
                    return _request(_http_error(404))
                return _request({"members": outer.members_by_group[groupKey]})

            def get(self, groupKey, memberKey):
                role = outer.memberships.get((groupKey, memberKey))
                if role is None:
                    return _request(_http_error(404))
                return _request({"email": memberKey, "role": role, "id": "m0"})

            def delete(self, groupKey, memberKey):
                outer.mutations.append(("delete", groupKey, memberKey))
                return _request({})

            def insert(self, groupKey, body):
                outer.mutations.append(("insert", groupKey, body))
                return _request(
                    {"email": body["email"], "role": body["role"], "id": "m1"}
                )

        return _Members()


class TestResolvedGuard:
    """Second layer: the Directory's canonical view of the target group."""

    @pytest.mark.asyncio
    async def test_alias_of_policy_group_is_refused(self):
        alias = "it-admins@otbgroup.co.uk"
        d = _Directory(
            {alias: {"id": "g1", "email": ADMINS, "aliases": [alias]}},
            members_by_group={ADMINS: []},
        )
        with pytest.raises(UserInputError, match="access-policy group"):
            await _unwrap(groups.add_group_member)(
                d, USER, group_email=alias, member_email="x@otbgroup.co.uk"
            )
        assert d.mutations == []

    @pytest.mark.asyncio
    async def test_group_carrying_policy_alias_is_refused(self):
        target = "drivers@otbgroup.co.uk"
        d = _Directory(
            {target: {"id": "g2", "email": target, "aliases": [ADMINS]}},
            members_by_group={ADMINS: []},
        )
        with pytest.raises(UserInputError, match="access-policy group"):
            await _unwrap(groups.add_group_member)(
                d, USER, group_email=target, member_email="x@otbgroup.co.uk"
            )

    @pytest.mark.asyncio
    async def test_group_nested_in_policy_group_is_refused(self):
        inner = "it-team@otbgroup.co.uk"
        deeper = "it-contractors@otbgroup.co.uk"
        d = _Directory(
            {deeper: {"id": "g3", "email": deeper, "aliases": []}},
            members_by_group={
                ADMINS: [
                    {"type": "GROUP", "email": inner},
                    {"type": "USER", "email": USER},
                ],
                inner: [{"type": "GROUP", "email": deeper}],
                deeper: [],
            },
        )
        with pytest.raises(UserInputError, match="nested inside"):
            await _unwrap(groups.add_group_member)(
                d, USER, group_email=deeper, member_email="x@otbgroup.co.uk"
            )
        assert d.mutations == []

    @pytest.mark.asyncio
    async def test_unrelated_group_proceeds_by_canonical_email(self):
        alias = "drivers-alias@otbgroup.co.uk"
        canonical = "drivers@otbgroup.co.uk"
        d = _Directory(
            {alias: {"id": "g4", "email": canonical, "aliases": [alias]}},
            members_by_group={ADMINS: [], canonical: []},
        )
        out = await _unwrap(groups.add_group_member)(
            d, USER, group_email=alias, member_email="x@otbgroup.co.uk"
        )
        assert "Added" in out
        assert d.mutations == [
            ("insert", canonical, {"email": "x@otbgroup.co.uk", "role": "MEMBER"})
        ]

    @pytest.mark.asyncio
    async def test_unreadable_policy_group_members_fails_closed(self):
        target = "drivers@otbgroup.co.uk"
        d = _Directory(
            {target: {"id": "g5", "email": target, "aliases": []}},
            members_by_group={target: []},
            list_error={ADMINS: _http_error(403)},
        )
        with pytest.raises(UserInputError, match="could not read the members"):
            await _unwrap(groups.add_group_member)(
                d, USER, group_email=target, member_email="x@otbgroup.co.uk"
            )
        assert d.mutations == []

    @pytest.mark.asyncio
    async def test_remove_via_alias_of_policy_group_is_refused(self):
        alias = "it-admins@otbgroup.co.uk"
        d = _Directory(
            {alias: {"id": "g1", "email": ADMINS, "aliases": [alias]}},
            members_by_group={ADMINS: []},
            memberships={(ADMINS, USER): "OWNER"},
        )
        with pytest.raises(UserInputError, match="access-policy group"):
            await _unwrap(groups.remove_group_member)(
                d, USER, group_email=alias, member_email=USER
            )
        assert d.mutations == []

    @pytest.mark.asyncio
    async def test_remove_from_group_nested_in_policy_group_is_refused(self):
        inner = "it-team@otbgroup.co.uk"
        d = _Directory(
            {inner: {"id": "g7", "email": inner, "aliases": []}},
            members_by_group={
                ADMINS: [{"type": "GROUP", "email": inner}],
                inner: [],
            },
            memberships={(inner, "x@otbgroup.co.uk"): "MEMBER"},
        )
        with pytest.raises(UserInputError, match="nested inside"):
            await _unwrap(groups.remove_group_member)(
                d, USER, group_email=inner, member_email="x@otbgroup.co.uk"
            )
        assert d.mutations == []

    @pytest.mark.asyncio
    async def test_remove_from_unrelated_group_uses_canonical_email(self):
        alias = "drivers-alias@otbgroup.co.uk"
        canonical = "drivers@otbgroup.co.uk"
        d = _Directory(
            {alias: {"id": "g4", "email": canonical, "aliases": [alias]}},
            members_by_group={ADMINS: [], canonical: []},
            memberships={(canonical, "x@otbgroup.co.uk"): "MEMBER"},
        )
        out = await _unwrap(groups.remove_group_member)(
            d, USER, group_email=alias, member_email="x@otbgroup.co.uk"
        )
        assert "Removed" in out
        assert d.mutations == [("delete", canonical, "x@otbgroup.co.uk")]

    @pytest.mark.asyncio
    async def test_policy_group_not_yet_created_is_not_an_error(self):
        target = "drivers@otbgroup.co.uk"
        d = _Directory(
            {target: {"id": "g6", "email": target, "aliases": []}},
            members_by_group={target: []},
        )
        out = await _unwrap(groups.add_group_member)(
            d, USER, group_email=target, member_email="x@otbgroup.co.uk"
        )
        assert "Added" in out


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

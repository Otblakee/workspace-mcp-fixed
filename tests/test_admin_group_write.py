"""Tests for the opt-in Admin SDK group-write service (``gadmin_write``).

Two things are under test:

1. **The tools work** — create_group / add_group_member / remove_group_member
   are idempotent, guard against removing the last group admin, and support
   dry_run.
2. **The carve-out stays a carve-out** — the read-only ``gadmin`` module is
   untouched, the one write scope is confined to the ``gadmin_write`` service,
   the service is opt-in, and no Directory write surface beyond groups and
   membership is reachable.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from googleapiclient.errors import HttpError  # noqa: E402

from core.utils import UserInputError  # noqa: E402
from gadmin import admin_group_tools as groups  # noqa: E402

USER = "oliver@otbgroup.co.uk"
REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "gadmin" / "admin_group_tools.py"

GROUP_WRITE_TOOLS = {"create_group", "add_group_member", "remove_group_member"}


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


create_group = _unwrap(groups.create_group)
add_group_member = _unwrap(groups.add_group_member)
remove_group_member = _unwrap(groups.remove_group_member)


def _not_found() -> HttpError:
    resp = MagicMock()
    resp.status = 404
    return HttpError(resp, b'{"error": {"errors": [{"reason": "notFound"}]}}')


def _request(result):
    request = MagicMock()
    if isinstance(result, Exception):
        request.execute.side_effect = result
    else:
        request.execute.return_value = result
    return request


class FakeDirectory:
    """Admin SDK Directory double."""

    def __init__(self, *, group=None, member=None, members=None):
        self.group = group
        self.member = member
        self.members_list = members or []
        self.calls = []

    def groups(self):
        parent = self

        class _Groups:
            def get(self, **kwargs):
                parent.calls.append(("groups.get", kwargs))
                if parent.group is None:
                    return _request(_not_found())
                return _request(parent.group)

            def insert(self, **kwargs):
                parent.calls.append(("groups.insert", kwargs))
                return _request({"id": "g1", **kwargs["body"]})

        return _Groups()

    def members(self):
        parent = self

        class _Members:
            def get(self, **kwargs):
                parent.calls.append(("members.get", kwargs))
                if parent.member is None:
                    return _request(_not_found())
                return _request(parent.member)

            def insert(self, **kwargs):
                parent.calls.append(("members.insert", kwargs))
                return _request({"id": "m1", **kwargs["body"]})

            def update(self, **kwargs):
                parent.calls.append(("members.update", kwargs))
                return _request({"id": "m1", **kwargs["body"]})

            def delete(self, **kwargs):
                parent.calls.append(("members.delete", kwargs))
                return _request({})

            def list(self, **kwargs):
                parent.calls.append(("members.list", kwargs))
                return _request({"members": parent.members_list})

        return _Members()

    def call_names(self):
        return [n for n, _ in self.calls]

    def kwargs_for(self, name):
        return [kw for n, kw in self.calls if n == name]


# ---------------------------------------------------------------------------
# create_group
# ---------------------------------------------------------------------------


class TestCreateGroup:
    @pytest.mark.asyncio
    async def test_creates_a_group(self):
        service = FakeDirectory()
        result = await create_group(
            service,
            USER,
            email="otb-premises@otbgroup.co.uk",
            description="Premises & Property",
        )

        body = service.kwargs_for("groups.insert")[0]["body"]
        assert body["email"] == "otb-premises@otbgroup.co.uk"
        assert body["name"] == "otb-premises"
        assert body["description"] == "Premises & Property"
        assert "g1" in result

    @pytest.mark.asyncio
    async def test_existing_group_is_a_no_op(self):
        service = FakeDirectory(group={"id": "g0", "email": "otb-media@otbgroup.co.uk"})
        result = await create_group(service, USER, email="otb-media@otbgroup.co.uk")

        assert "already exists" in result
        assert "groups.insert" not in service.call_names()

    @pytest.mark.asyncio
    async def test_dry_run_creates_nothing(self):
        service = FakeDirectory()
        result = await create_group(
            service, USER, email="vale-projects@otbgroup.co.uk", dry_run=True
        )
        assert "DRY RUN" in result
        assert "groups.insert" not in service.call_names()

    @pytest.mark.asyncio
    async def test_rejects_a_non_email(self):
        with pytest.raises(UserInputError):
            await create_group(FakeDirectory(), USER, email="not-an-email")


# ---------------------------------------------------------------------------
# add_group_member
# ---------------------------------------------------------------------------


class TestAddGroupMember:
    @pytest.mark.asyncio
    async def test_adds_a_member(self):
        service = FakeDirectory(group={"id": "g1", "email": "otb@otbgroup.co.uk"})
        result = await add_group_member(
            service, USER, "otb@otbgroup.co.uk", "someone@otbgroup.co.uk"
        )

        body = service.kwargs_for("members.insert")[0]["body"]
        assert body == {"email": "someone@otbgroup.co.uk", "role": "MEMBER"}
        assert "Added" in result

    @pytest.mark.asyncio
    async def test_existing_member_at_same_role_is_a_no_op(self):
        service = FakeDirectory(
            group={"id": "g1"},
            member={"id": "m0", "email": "someone@otbgroup.co.uk", "role": "MEMBER"},
        )
        result = await add_group_member(
            service, USER, "otb@otbgroup.co.uk", "someone@otbgroup.co.uk"
        )

        assert "already a MEMBER" in result
        assert "members.insert" not in service.call_names()

    @pytest.mark.asyncio
    async def test_role_change_updates_rather_than_duplicates(self):
        service = FakeDirectory(
            group={"id": "g1"},
            member={"id": "m0", "email": "someone@otbgroup.co.uk", "role": "MEMBER"},
        )
        result = await add_group_member(
            service,
            USER,
            "otb@otbgroup.co.uk",
            "someone@otbgroup.co.uk",
            role="MANAGER",
        )

        assert "members.insert" not in service.call_names()
        assert service.kwargs_for("members.update")[0]["body"] == {"role": "MANAGER"}
        assert "MEMBER → MANAGER" in result

    @pytest.mark.asyncio
    async def test_missing_group_is_a_clear_error(self):
        service = FakeDirectory()
        with pytest.raises(UserInputError, match="does not exist"):
            await add_group_member(
                service, USER, "ghost@otbgroup.co.uk", "someone@otbgroup.co.uk"
            )

    @pytest.mark.asyncio
    async def test_invalid_role_rejected(self):
        service = FakeDirectory(group={"id": "g1"})
        with pytest.raises(UserInputError, match="Invalid role"):
            await add_group_member(
                service, USER, "otb@otbgroup.co.uk", "s@otbgroup.co.uk", role="ADMIN"
            )

    @pytest.mark.asyncio
    async def test_demoting_the_last_manager_is_refused(self):
        """Demoting the sole administrator strands the group exactly as
        removing them would, so the same guard must apply to both paths."""
        service = FakeDirectory(
            group={"id": "g1"},
            member={"id": "m0", "email": "boss@otbgroup.co.uk", "role": "MANAGER"},
            members=[
                {"email": "boss@otbgroup.co.uk", "role": "MANAGER"},
                {"email": "s@otbgroup.co.uk", "role": "MEMBER"},
            ],
        )

        with pytest.raises(UserInputError, match="last MANAGER"):
            await add_group_member(
                service,
                USER,
                "otb@otbgroup.co.uk",
                "boss@otbgroup.co.uk",
                role="MEMBER",
            )

        assert "members.update" not in service.call_names()

    @pytest.mark.asyncio
    async def test_demotion_allowed_when_another_admin_remains(self):
        service = FakeDirectory(
            group={"id": "g1"},
            member={"id": "m0", "email": "boss@otbgroup.co.uk", "role": "MANAGER"},
            members=[
                {"email": "boss@otbgroup.co.uk", "role": "MANAGER"},
                {"email": "owner@otbgroup.co.uk", "role": "OWNER"},
            ],
        )

        result = await add_group_member(
            service,
            USER,
            "otb@otbgroup.co.uk",
            "boss@otbgroup.co.uk",
            role="MEMBER",
        )

        assert service.kwargs_for("members.update")[0]["body"] == {"role": "MEMBER"}
        assert "MANAGER → MEMBER" in result

    @pytest.mark.asyncio
    async def test_promotion_never_trips_the_guard(self):
        """Promoting the sole manager to OWNER keeps an administrator, so it
        must not be refused."""
        service = FakeDirectory(
            group={"id": "g1"},
            member={"id": "m0", "email": "boss@otbgroup.co.uk", "role": "MANAGER"},
            members=[{"email": "boss@otbgroup.co.uk", "role": "MANAGER"}],
        )

        result = await add_group_member(
            service,
            USER,
            "otb@otbgroup.co.uk",
            "boss@otbgroup.co.uk",
            role="OWNER",
        )

        assert "MANAGER → OWNER" in result

    @pytest.mark.asyncio
    async def test_dry_run_changes_nothing(self):
        service = FakeDirectory(group={"id": "g1"})
        result = await add_group_member(
            service,
            USER,
            "otb@otbgroup.co.uk",
            "s@otbgroup.co.uk",
            dry_run=True,
        )
        assert "DRY RUN" in result
        assert "members.insert" not in service.call_names()


# ---------------------------------------------------------------------------
# remove_group_member
# ---------------------------------------------------------------------------


class TestRemoveGroupMember:
    @pytest.mark.asyncio
    async def test_removes_a_plain_member(self):
        service = FakeDirectory(
            group={"id": "g1"},
            member={"id": "m0", "email": "s@otbgroup.co.uk", "role": "MEMBER"},
        )
        result = await remove_group_member(
            service, USER, "otb@otbgroup.co.uk", "s@otbgroup.co.uk"
        )

        assert service.kwargs_for("members.delete")[0]["memberKey"] == (
            "s@otbgroup.co.uk"
        )
        assert "Removed" in result

    @pytest.mark.asyncio
    async def test_non_member_is_a_no_op(self):
        service = FakeDirectory(group={"id": "g1"})
        result = await remove_group_member(
            service, USER, "otb@otbgroup.co.uk", "ghost@otbgroup.co.uk"
        )
        assert "nothing to remove" in result
        assert "members.delete" not in service.call_names()

    @pytest.mark.asyncio
    async def test_refuses_to_remove_the_last_manager(self):
        service = FakeDirectory(
            group={"id": "g1"},
            member={"id": "m0", "email": "boss@otbgroup.co.uk", "role": "MANAGER"},
            members=[
                {"email": "boss@otbgroup.co.uk", "role": "MANAGER"},
                {"email": "s@otbgroup.co.uk", "role": "MEMBER"},
            ],
        )

        with pytest.raises(UserInputError, match="last MANAGER"):
            await remove_group_member(
                service, USER, "otb@otbgroup.co.uk", "boss@otbgroup.co.uk"
            )

    @pytest.mark.asyncio
    async def test_removes_a_manager_when_another_admin_remains(self):
        service = FakeDirectory(
            group={"id": "g1"},
            member={"id": "m0", "email": "boss@otbgroup.co.uk", "role": "MANAGER"},
            members=[
                {"email": "boss@otbgroup.co.uk", "role": "MANAGER"},
                {"email": "owner@otbgroup.co.uk", "role": "OWNER"},
            ],
        )

        result = await remove_group_member(
            service, USER, "otb@otbgroup.co.uk", "boss@otbgroup.co.uk"
        )
        assert "Removed" in result

    @pytest.mark.asyncio
    async def test_dry_run_removes_nothing(self):
        service = FakeDirectory(
            group={"id": "g1"},
            member={"id": "m0", "email": "s@otbgroup.co.uk", "role": "MEMBER"},
        )
        result = await remove_group_member(
            service, USER, "otb@otbgroup.co.uk", "s@otbgroup.co.uk", dry_run=True
        )
        assert "DRY RUN" in result
        assert "members.delete" not in service.call_names()


# ---------------------------------------------------------------------------
# The carve-out stays a carve-out
# ---------------------------------------------------------------------------


class TestWriteSurfaceIsMinimal:
    def test_module_exposes_exactly_three_tools(self):
        import inspect

        seen = set()
        for name, obj in vars(groups).items():
            if name.startswith("_") or not callable(obj):
                continue
            impl = _unwrap(obj)
            if not inspect.iscoroutinefunction(impl):
                continue
            params = list(inspect.signature(impl).parameters)
            if params[:2] == ["service", "user_google_email"]:
                seen.add(name)
        assert seen == GROUP_WRITE_TOOLS

    @pytest.mark.parametrize(
        "method_substr",
        [
            "users().insert",
            "users().update",
            "users().delete",
            "users().patch",
            "users().makeAdmin",
            "groups().delete",
            "groups().update",
            "groups().patch",
            "orgunits().insert",
            "orgunits().update",
            "orgunits().delete",
            "roles().insert",
            "roles().delete",
            "roleAssignments().insert",
            "roleAssignments().delete",
            "tokens().delete",
            "mobiledevices().action",
        ],
    )
    def test_no_directory_write_beyond_groups_and_membership(self, method_substr):
        """Group creation and membership are the whole write surface. Anything
        else stays on GAM CLI / the Admin Console."""
        src = MODULE_PATH.read_text()
        no_comments = "\n".join(
            ln for ln in src.splitlines() if not re.match(r"^\s*#", ln)
        )
        no_docstrings = re.sub(r'(?s)"""(.*?)"""', "", no_comments)
        assert re.search(re.escape(method_substr) + r"\s*\(", no_docstrings) is None

    def test_readonly_admin_module_is_untouched_by_this_service(self):
        """gadmin/admin_tools.py must still contain no write call at all —
        the group writes live in a different module on purpose."""
        src = (REPO_ROOT / "gadmin" / "admin_tools.py").read_text()
        no_comments = "\n".join(
            ln for ln in src.splitlines() if not re.match(r"^\s*#", ln)
        )
        no_docstrings = re.sub(r'(?s)"""(.*?)"""', "", no_comments)
        for needle in ("groups().insert", "members().insert", "members().delete"):
            assert re.search(re.escape(needle) + r"\s*\(", no_docstrings) is None


class TestServiceWiring:
    def test_service_is_opt_in(self):
        import main

        assert "gadmin_write" in main.OPT_IN_TOOLS

    def test_scope_group_resolves(self):
        from auth.service_decorator import SCOPE_GROUPS

        assert (
            SCOPE_GROUPS["admin_directory_group_write"]
            == "https://www.googleapis.com/auth/admin.directory.group"
        )

    def test_tools_declared_in_tool_tiers(self):
        import yaml

        data = yaml.safe_load((REPO_ROOT / "core" / "tool_tiers.yaml").read_text())
        section = data["gadmin_write"]
        listed = (
            set(section.get("core") or [])
            | set(section.get("extended") or [])
            | set(section.get("complete") or [])
        )
        assert listed == GROUP_WRITE_TOOLS

    def test_tools_are_not_blocked(self):
        from core.tool_policy import BLOCKED_TOOLS

        assert not (GROUP_WRITE_TOOLS & BLOCKED_TOOLS)

    def test_audit_tags_the_module_as_gadmin(self):
        from core.audit import _service

        for name in GROUP_WRITE_TOOLS:
            assert _service(name, "gadmin.admin_group_tools") == "gadmin"

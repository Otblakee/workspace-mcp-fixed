"""Tests for AccessPolicyMiddleware (auth/access_policy_middleware.py) and the
get_my_access tool, plus the middleware ordering in core/server.py."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp.exceptions import AuthorizationError  # noqa: E402

from auth import access_policy_middleware as apm  # noqa: E402
from core import access_policy as ap  # noqa: E402

STAFF = "mcp-staff@otbgroup.co.uk"
ADMINS = "mcp-admins@otbgroup.co.uk"


class FakeCtx:
    def __init__(self, email=None):
        self.state = {}
        self.session_id = None
        if email:
            self.state["authenticated_user_email"] = email

    async def set_state(self, key, value, serializable=True):
        self.state[key] = value

    async def get_state(self, key):
        return self.state.get(key)


def _mw_context(email=None, tool="search_gmail_messages", with_ctx=True):
    return SimpleNamespace(
        fastmcp_context=FakeCtx(email) if with_ctx else None,
        message=SimpleNamespace(name=tool),
        method="tools/call",
    )


def _tools(*names):
    return [SimpleNamespace(name=n) for n in names]


class _Source(ap.MembershipSource):
    def __init__(self, members):
        self.members = members

    async def is_member(self, email, group):
        return group in self.members.get(email, set())


def _engine(members, mode="enforce"):
    policy = ap.parse_policy(
        {
            "groups": {
                ADMINS: {"allow": ["*"]},
                STAFF: {"allow": ["gmail.core"], "deny": ["send_gmail_message"]},
            }
        },
        source="<test>",
    )
    resolver = ap.MembershipResolver(_Source(members), policy.group_emails)
    return ap.AccessPolicyEngine(
        mode=mode, policy=policy, resolver=resolver, source_name="test"
    )


@pytest.fixture
def http_transport(monkeypatch):
    monkeypatch.setattr(apm, "_transport_is_stdio", lambda: False)


@pytest.fixture
def audit_submit(monkeypatch):
    from core import audit

    fake = MagicMock()
    monkeypatch.setattr(audit, "logger", lambda: fake)
    return fake.submit


TOOLS = (
    "search_gmail_messages",
    "send_gmail_message",
    "create_shared_drive",
    "get_my_access",
)


class TestListTools:
    @pytest.mark.asyncio
    async def test_stdio_passthrough(self, monkeypatch):
        monkeypatch.setattr(apm, "_transport_is_stdio", lambda: True)
        mw = apm.AccessPolicyMiddleware(_engine({}))
        call_next = AsyncMock(return_value=_tools(*TOOLS))
        out = await mw.on_list_tools(_mw_context(), call_next)
        assert [t.name for t in out] == list(TOOLS)

    @pytest.mark.asyncio
    async def test_mode_off_passthrough(self, http_transport):
        mw = apm.AccessPolicyMiddleware(_engine({}, mode="off"))
        call_next = AsyncMock(return_value=_tools(*TOOLS))
        out = await mw.on_list_tools(_mw_context("x@otbgroup.co.uk"), call_next)
        assert [t.name for t in out] == list(TOOLS)

    @pytest.mark.asyncio
    async def test_filtered_for_staff(self, http_transport):
        mw = apm.AccessPolicyMiddleware(_engine({"k@otbgroup.co.uk": {STAFF}}))
        call_next = AsyncMock(return_value=_tools(*TOOLS))
        out = await mw.on_list_tools(_mw_context("k@otbgroup.co.uk"), call_next)
        assert {t.name for t in out} == {"search_gmail_messages", "get_my_access"}

    @pytest.mark.asyncio
    async def test_unauthenticated_sees_nothing(self, http_transport):
        mw = apm.AccessPolicyMiddleware(_engine({}))
        call_next = AsyncMock(return_value=_tools(*TOOLS))
        assert await mw.on_list_tools(_mw_context(None), call_next) == []
        assert await mw.on_list_tools(_mw_context(with_ctx=False), call_next) == []

    @pytest.mark.asyncio
    async def test_policy_load_error_lists_only_always_allowed(
        self, http_transport, monkeypatch
    ):
        def boom():
            raise ap.PolicyError("bad yaml")

        monkeypatch.setattr(apm, "get_engine", boom)
        mw = apm.AccessPolicyMiddleware()
        call_next = AsyncMock(return_value=_tools(*TOOLS))
        out = await mw.on_list_tools(_mw_context("k@otbgroup.co.uk"), call_next)
        assert [t.name for t in out] == ["get_my_access"]


class TestCallTool:
    @pytest.mark.asyncio
    async def test_stdio_passthrough(self, monkeypatch):
        monkeypatch.setattr(apm, "_transport_is_stdio", lambda: True)
        mw = apm.AccessPolicyMiddleware(_engine({}))
        call_next = AsyncMock(return_value="ok")
        assert await mw.on_call_tool(_mw_context(), call_next) == "ok"

    @pytest.mark.asyncio
    async def test_allowed_call_passes(self, http_transport, audit_submit):
        mw = apm.AccessPolicyMiddleware(_engine({"k@otbgroup.co.uk": {STAFF}}))
        call_next = AsyncMock(return_value="ok")
        assert await mw.on_call_tool(_mw_context("k@otbgroup.co.uk"), call_next) == "ok"
        call_next.assert_awaited_once()
        audit_submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_denied_call_raises_and_is_audited(
        self, http_transport, audit_submit
    ):
        mw = apm.AccessPolicyMiddleware(_engine({"k@otbgroup.co.uk": {STAFF}}))
        call_next = AsyncMock(return_value="ok")
        with pytest.raises(AuthorizationError, match="send_gmail_message"):
            await mw.on_call_tool(
                _mw_context("k@otbgroup.co.uk", tool="send_gmail_message"), call_next
            )
        call_next.assert_not_awaited()
        row = audit_submit.call_args.args[0]
        assert row["status"] == "denied"
        assert row["tool"] == "send_gmail_message"
        assert row["user"] == "k@otbgroup.co.uk"
        assert row["service"] == "gmail"
        assert row["error"].startswith("policy:")
        assert STAFF in row["params_summary"]

    @pytest.mark.asyncio
    async def test_unauthenticated_denied(self, http_transport, audit_submit):
        mw = apm.AccessPolicyMiddleware(_engine({}))
        call_next = AsyncMock()
        with pytest.raises(AuthorizationError, match="no verified identity"):
            await mw.on_call_tool(_mw_context(None), call_next)
        call_next.assert_not_awaited()
        assert audit_submit.call_args.args[0]["status"] == "denied"

    @pytest.mark.asyncio
    async def test_get_my_access_always_allowed(self, http_transport, audit_submit):
        mw = apm.AccessPolicyMiddleware(_engine({}))  # user in no group
        call_next = AsyncMock(return_value="ok")
        ctx = _mw_context("nobody@otbgroup.co.uk", tool="get_my_access")
        assert await mw.on_call_tool(ctx, call_next) == "ok"

    @pytest.mark.asyncio
    async def test_lookup_failure_denies_with_reason(
        self, http_transport, audit_submit
    ):
        class Broken(ap.MembershipSource):
            async def is_member(self, email, group):
                raise ap.MembershipLookupError("HTTP 403")

        policy = ap.parse_policy(
            {"groups": {STAFF: {"allow": ["gmail.core"]}}}, source="<t>"
        )
        eng = ap.AccessPolicyEngine(
            mode="enforce",
            policy=policy,
            resolver=ap.MembershipResolver(Broken(), policy.group_emails),
        )
        mw = apm.AccessPolicyMiddleware(eng)
        with pytest.raises(AuthorizationError, match="membership lookup unavailable"):
            await mw.on_call_tool(_mw_context("k@otbgroup.co.uk"), AsyncMock())

    @pytest.mark.asyncio
    async def test_policy_load_error_denies_everything_but_always_allowed(
        self, http_transport, audit_submit, monkeypatch, caplog
    ):
        def boom():
            raise ap.PolicyError("unknown tool 'sned_gmail'")

        monkeypatch.setattr(apm, "get_engine", boom)
        mw = apm.AccessPolicyMiddleware()
        call_next = AsyncMock(return_value="ok")
        with caplog.at_level("ERROR", logger="auth.access_policy_middleware"):
            with pytest.raises(AuthorizationError, match="failed to load"):
                await mw.on_call_tool(_mw_context("k@otbgroup.co.uk"), call_next)
            # second denial does not re-log the same error
            with pytest.raises(AuthorizationError):
                await mw.on_call_tool(_mw_context("k@otbgroup.co.uk"), call_next)
        assert len([r for r in caplog.records if "failed to load" in r.message]) == 1
        assert audit_submit.call_args.args[0]["status"] == "denied"
        ctx = _mw_context("k@otbgroup.co.uk", tool="get_my_access")
        assert await mw.on_call_tool(ctx, call_next) == "ok"

    @pytest.mark.asyncio
    async def test_mode_off_passthrough(self, http_transport):
        mw = apm.AccessPolicyMiddleware(_engine({}, mode="off"))
        call_next = AsyncMock(return_value="ok")
        assert await mw.on_call_tool(_mw_context(None), call_next) == "ok"


class TestServerWiring:
    def test_policy_middleware_runs_after_auth_middleware(self):
        from auth.auth_info_middleware import AuthInfoMiddleware
        from core.server import server

        kinds = [type(m) for m in server.middleware]
        assert AuthInfoMiddleware in kinds and apm.AccessPolicyMiddleware in kinds
        assert kinds.index(AuthInfoMiddleware) < kinds.index(apm.AccessPolicyMiddleware)

    def test_get_my_access_is_registered_and_always_enabled(self):
        from core.server import server
        from core.tool_registry import (
            ALWAYS_ENABLED_TOOLS,
            get_tool_components,
            is_tool_enabled,
            set_enabled_tools,
        )

        assert "get_my_access" in get_tool_components(server)
        assert "get_my_access" in ALWAYS_ENABLED_TOOLS
        set_enabled_tools({"search_gmail_messages"})
        try:
            assert is_tool_enabled("get_my_access")
            assert not is_tool_enabled("send_gmail_message")
        finally:
            set_enabled_tools(None)

    def test_allowed_client_redirect_uris_env(self, monkeypatch):
        from core.server import _allowed_client_redirect_uris

        monkeypatch.delenv("MCP_ALLOWED_CLIENT_REDIRECT_URIS", raising=False)
        assert _allowed_client_redirect_uris() is None
        monkeypatch.setenv("MCP_ALLOWED_CLIENT_REDIRECT_URIS", "  ")
        assert _allowed_client_redirect_uris() is None
        monkeypatch.setenv(
            "MCP_ALLOWED_CLIENT_REDIRECT_URIS",
            "https://claude.ai/api/mcp/auth_callback, http://localhost:*",
        )
        assert _allowed_client_redirect_uris() == [
            "https://claude.ai/api/mcp/auth_callback",
            "http://localhost:*",
        ]


class TestGetMyAccessTool:
    def _tool(self):
        from core import server as server_mod

        fn = server_mod.get_my_access
        fn = fn.fn if hasattr(fn, "fn") else fn
        while hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        return fn

    @pytest.mark.asyncio
    async def test_reports_decision(self, monkeypatch):
        from core import server as server_mod
        from core import tool_registry

        # Only the tools imported by this test process are registered; pin
        # the registered set so the assertion does not depend on test order.
        monkeypatch.setattr(
            tool_registry,
            "get_tool_components",
            lambda _server: {name: object() for name in TOOLS},
        )
        monkeypatch.setattr(
            server_mod, "get_context", lambda: FakeCtx("k@otbgroup.co.uk")
        )
        ap.set_engine(_engine({"k@otbgroup.co.uk": {STAFF}}))
        try:
            text = await self._tool()()
        finally:
            ap.set_engine(None)
        assert "Signed in as: k@otbgroup.co.uk" in text
        assert STAFF in text
        assert "- search_gmail_messages" in text
        assert "- send_gmail_message" not in text
        assert "decision source: policy" in text

    @pytest.mark.asyncio
    async def test_reports_policy_off(self, monkeypatch):
        from core import server as server_mod

        monkeypatch.setattr(
            server_mod, "get_context", lambda: FakeCtx("k@otbgroup.co.uk")
        )
        ap.set_engine(_engine({}, mode="off"))
        try:
            text = await self._tool()()
        finally:
            ap.set_engine(None)
        assert "Access policy: off" in text

    @pytest.mark.asyncio
    async def test_reports_load_failure(self, monkeypatch):
        from core import server as server_mod

        monkeypatch.setattr(
            server_mod, "get_context", lambda: FakeCtx("k@otbgroup.co.uk")
        )
        monkeypatch.setenv(ap.MODE_ENV, "enforce")
        monkeypatch.setenv(ap.FILE_ENV, "/nonexistent.yaml")
        ap.set_engine(None)
        try:
            text = await self._tool()()
        finally:
            ap.set_engine(None)
        assert "FAILED TO LOAD" in text

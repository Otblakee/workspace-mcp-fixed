"""AuthInfoMiddleware: tools/list identity hook and explicit domain-policy
rejection on tools/call."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp.exceptions import AuthorizationError  # noqa: E402

from auth import auth_info_middleware as aim  # noqa: E402


class FakeCtx:
    def __init__(self):
        self.state = {}
        self.session_id = None

    async def set_state(self, key, value, serializable=True):
        self.state[key] = value

    async def get_state(self, key):
        return self.state.get(key)


def _context():
    return SimpleNamespace(fastmcp_context=FakeCtx(), message=SimpleNamespace(name="x"))


class _Token:
    def __init__(self, email, hd=None):
        self.email = email
        self.claims = {"email": email}
        if hd:
            self.claims["hd"] = hd


@pytest.fixture
def http_mode(monkeypatch):
    import core.config as cfg

    monkeypatch.setattr(cfg, "get_transport_mode", lambda: "streamable-http")
    monkeypatch.setattr(aim, "get_http_headers", lambda: {})


class TestListToolsHook:
    @pytest.mark.asyncio
    async def test_populates_identity_then_continues(self, monkeypatch, http_mode):
        monkeypatch.delenv("OAUTH_ALLOWED_EMAIL_DOMAINS", raising=False)
        monkeypatch.setattr(aim, "get_access_token", lambda: _Token("k@otbgroup.co.uk"))
        ctx = _context()
        call_next = AsyncMock(return_value=["tools"])
        out = await aim.AuthInfoMiddleware().on_list_tools(ctx, call_next)
        assert out == ["tools"]
        assert (
            ctx.fastmcp_context.state["authenticated_user_email"] == "k@otbgroup.co.uk"
        )

    @pytest.mark.asyncio
    async def test_identity_failure_does_not_break_listing(self, monkeypatch):
        mw = aim.AuthInfoMiddleware()

        async def boom(context):
            raise RuntimeError("auth plumbing exploded")

        monkeypatch.setattr(mw, "_process_request_for_auth", boom)
        call_next = AsyncMock(return_value=["tools"])
        assert await mw.on_list_tools(_context(), call_next) == ["tools"]


class TestDomainRejectionIsExplicit:
    @pytest.mark.asyncio
    async def test_call_tool_raises_for_foreign_domain(self, monkeypatch, http_mode):
        monkeypatch.setenv("OAUTH_ALLOWED_EMAIL_DOMAINS", "otbgroup.co.uk")
        monkeypatch.setattr(
            aim, "get_access_token", lambda: _Token("mallory@evil.example")
        )
        ctx = _context()
        call_next = AsyncMock(return_value="ran")
        with pytest.raises(AuthorizationError, match="not permitted"):
            await aim.AuthInfoMiddleware().on_call_tool(ctx, call_next)
        call_next.assert_not_awaited()
        assert "authenticated_user_email" not in ctx.fastmcp_context.state

    @pytest.mark.asyncio
    async def test_rejection_is_logged_without_traceback(
        self, monkeypatch, http_mode, caplog
    ):
        monkeypatch.setenv("OAUTH_ALLOWED_EMAIL_DOMAINS", "otbgroup.co.uk")
        monkeypatch.setattr(
            aim, "get_access_token", lambda: _Token("mallory@evil.example")
        )
        with caplog.at_level("INFO", logger="auth.auth_info_middleware"):
            with pytest.raises(AuthorizationError):
                await aim.AuthInfoMiddleware().on_call_tool(_context(), AsyncMock())
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert errors == []
        assert any("Authentication check failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_call_tool_passes_for_allowed_domain(self, monkeypatch, http_mode):
        monkeypatch.setenv("OAUTH_ALLOWED_EMAIL_DOMAINS", "otbgroup.co.uk")
        monkeypatch.setattr(
            aim,
            "get_access_token",
            lambda: _Token("k@otbgroup.co.uk", hd="otbgroup.co.uk"),
        )
        ctx = _context()
        call_next = AsyncMock(return_value="ran")
        assert await aim.AuthInfoMiddleware().on_call_tool(ctx, call_next) == "ran"
        assert (
            ctx.fastmcp_context.state["authenticated_user_email"] == "k@otbgroup.co.uk"
        )
        assert aim.REJECTION_STATE_KEY not in ctx.fastmcp_context.state

    @pytest.mark.asyncio
    async def test_rejection_state_is_request_scoped(self, monkeypatch, http_mode):
        """The rejection marker is stored with serializable=False so FastMCP
        keeps it in request state, never session state."""
        monkeypatch.setenv("OAUTH_ALLOWED_EMAIL_DOMAINS", "otbgroup.co.uk")
        monkeypatch.setattr(
            aim, "get_access_token", lambda: _Token("mallory@evil.example")
        )
        recorded = {}

        class Ctx(FakeCtx):
            async def set_state(self, key, value, serializable=True):
                recorded[key] = serializable
                await super().set_state(key, value, serializable)

        ctx = SimpleNamespace(fastmcp_context=Ctx(), message=SimpleNamespace(name="x"))
        with pytest.raises(AuthorizationError):
            await aim.AuthInfoMiddleware().on_call_tool(ctx, AsyncMock())
        assert recorded[aim.REJECTION_STATE_KEY] is False

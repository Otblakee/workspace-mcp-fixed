"""Tests for the multi-user security hardening fixes.

Covers four behaviour changes that landed together because they all blunt
the same class of cross-user leak:

1. ``_detect_oauth_version`` fails closed when OAuth 2.1 is enabled but the
   request carries no verified identity (closed the OAuth 2.0 fallback that
   let a client pick ``user_google_email`` from kwargs).
2. ``_redact`` walks nested dicts/lists so SENSITIVE keys at any depth are
   replaced (the old shallow walk leaked nested attachments etc.).
3. ``_resolve_user_email`` warns instead of swallowing on empty/exception so
   misattribution to DEFAULT_USER is visible.
4. ``_claims_pass_domain_policy`` enforces ``email_verified`` and an optional
   domain allowlist before middleware sets ``authenticated_user_email``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Fix 1 — _detect_oauth_version fail-closed
# ---------------------------------------------------------------------------


class TestDetectOAuthVersionFailClosed:
    def test_returns_false_when_oauth21_disabled(self):
        from auth import service_decorator

        with patch.object(service_decorator, "is_oauth21_enabled", return_value=False):
            assert service_decorator._detect_oauth_version(None, None, "tool") is False

    def test_returns_true_for_authenticated_user_under_oauth21(self):
        from auth import service_decorator

        with patch.object(service_decorator, "is_oauth21_enabled", return_value=True):
            assert (
                service_decorator._detect_oauth_version("u@x.com", None, "tool") is True
            )

    def test_returns_true_when_validated_access_token_present(self):
        from auth import service_decorator

        with patch.object(
            service_decorator, "is_oauth21_enabled", return_value=True
        ), patch.object(
            service_decorator, "get_access_token", return_value=MagicMock()
        ):
            assert service_decorator._detect_oauth_version(None, None, "tool") is True

    def test_raises_when_oauth21_enabled_and_no_identity(self):
        """The critical regression: OAuth 2.1 enabled, no authenticated_user,
        no access token. The pre-fix code returned False (fall back to OAuth
        2.0) which then let kwargs['user_google_email'] choose the user."""
        from auth import service_decorator
        from auth.google_auth import GoogleAuthenticationError

        with patch.object(
            service_decorator, "is_oauth21_enabled", return_value=True
        ), patch.object(service_decorator, "get_access_token", return_value=None):
            with pytest.raises(GoogleAuthenticationError, match="OAuth 2.1"):
                service_decorator._detect_oauth_version(None, "session-x", "tool")

    def test_raises_even_when_get_access_token_throws(self):
        """A throwing get_access_token must not become an implicit pass."""
        from auth import service_decorator
        from auth.google_auth import GoogleAuthenticationError

        with patch.object(
            service_decorator, "is_oauth21_enabled", return_value=True
        ), patch.object(
            service_decorator,
            "get_access_token",
            side_effect=RuntimeError("token plumbing exploded"),
        ):
            with pytest.raises(GoogleAuthenticationError):
                service_decorator._detect_oauth_version(None, None, "tool")


# ---------------------------------------------------------------------------
# Fix 2 — deep _redact
# ---------------------------------------------------------------------------


class TestRedactDeep:
    def test_top_level_sensitive_key_redacted(self):
        from core.audit import _redact

        out = _redact({"body": "secret-payload", "tool": "send"})
        assert "secret-payload" not in out
        assert '"body":' in out and "<redacted:" in out
        assert '"tool"' in out and "send" in out

    def test_nested_sensitive_key_redacted(self):
        """The pre-fix walk only matched top-level keys, so a sensitive
        nested field like ``message.body`` slipped through verbatim."""
        from core.audit import _redact

        out = _redact({"message": {"to": "x@y.com", "body": "leaky-secret"}})
        assert "leaky-secret" not in out
        assert "x@y.com" in out

    def test_sensitive_inside_list_redacted(self):
        from core.audit import _redact

        out = _redact({"attachments": [{"content": "binary-blob"}]})
        # 'attachments' itself is in SENSITIVE so the whole list collapses.
        assert "binary-blob" not in out
        assert "<redacted:" in out

    def test_gmail_attachments_kwarg_shape_redacted(self):
        """Phase 2.5 Fix E verification. The real `attachments` kwarg passed
        to send_gmail_message / draft_gmail_message is a list of dicts whose
        ``content`` field carries base64-encoded file bytes. Confirm the
        full list collapses to a single redacted token — the payload never
        reaches the audit sheet."""
        from core.audit import SENSITIVE, _redact

        # Acceptance check 1: ``attachments`` is in the SENSITIVE constant.
        assert "attachments" in SENSITIVE

        # Acceptance check 2: a realistic kwargs shape gets redacted.
        raw_payload = "AAAA" * 2048  # ~8 KB base64 blob
        out = _redact(
            {
                "user_google_email": "oliver@otbgroup.co.uk",
                "to": "client@example.com",
                "subject": "Q1 report",
                "attachments": [
                    {
                        "filename": "report.pdf",
                        "content": raw_payload,
                        "mime_type": "application/pdf",
                    }
                ],
            }
        )
        assert raw_payload not in out
        assert "report.pdf" not in out  # whole list collapses, filename too
        assert '"attachments":' in out
        assert "<redacted:list:1>" in out

    def test_sensitive_in_list_under_neutral_key_still_redacted(self):
        """``parts`` isn't sensitive, but each part has a sensitive ``content``."""
        from core.audit import _redact

        out = _redact(
            {"parts": [{"name": "a.txt", "content": "x"}, {"content": "y"}]}
        )
        assert '"name": "a.txt"' in out
        assert '"content": "x"' not in out
        assert '"content": "y"' not in out

    def test_long_strings_truncated_anywhere(self):
        from core.audit import _redact

        big = "z" * 5000
        out = _redact({"meta": {"label": big}})
        assert "zzzz…" in out
        # outer wrap must still cap at 1000 chars
        assert len(out) <= 1000

    def test_depth_cap_prevents_runaway(self):
        from core.audit import _redact, _REDACT_MAX_DEPTH

        # Build a structure deeper than the cap.
        d: dict = {"x": "leaf"}
        for _ in range(_REDACT_MAX_DEPTH + 4):
            d = {"x": d}
        out = _redact(d)
        assert "<truncated:depth>" in out

    def test_non_serializable_values_do_not_crash(self):
        from core.audit import _redact

        class Weird:
            pass

        out = _redact({"obj": Weird()})
        assert "<Weird>" in out


# ---------------------------------------------------------------------------
# Fix 3 — _resolve_user_email logging
# ---------------------------------------------------------------------------


class TestResolveUserEmailLogging:
    @pytest.mark.asyncio
    async def test_warns_when_state_empty(self, caplog):
        from core import audit

        ctx = MagicMock()
        ctx.get_state = AsyncMock(return_value=None)

        with patch(
            "fastmcp.server.dependencies.get_context", return_value=ctx
        ), caplog.at_level("WARNING", logger="core.audit"):
            email = await audit._resolve_user_email()

        assert email == audit.DEFAULT_USER
        assert any(
            "authenticated_user_email empty" in r.message for r in caplog.records
        ), caplog.records

    @pytest.mark.asyncio
    async def test_warns_when_get_state_raises(self, caplog):
        from core import audit

        ctx = MagicMock()
        ctx.get_state = AsyncMock(side_effect=RuntimeError("boom"))

        with patch(
            "fastmcp.server.dependencies.get_context", return_value=ctx
        ), caplog.at_level("WARNING", logger="core.audit"):
            email = await audit._resolve_user_email()

        assert email == audit.DEFAULT_USER
        assert any(
            "failed to resolve user email" in r.message for r in caplog.records
        ), caplog.records

    @pytest.mark.asyncio
    async def test_no_warn_when_no_context(self, caplog):
        """Stdio dev / background tasks legitimately have no context — quiet."""
        from core import audit

        with patch(
            "fastmcp.server.dependencies.get_context", return_value=None
        ), caplog.at_level("WARNING", logger="core.audit"):
            email = await audit._resolve_user_email()

        assert email == audit.DEFAULT_USER
        assert caplog.records == []


# ---------------------------------------------------------------------------
# Fix 4 — domain policy
# ---------------------------------------------------------------------------


class TestDomainPolicy:
    def test_no_allowlist_means_permissive(self, monkeypatch):
        from auth.auth_info_middleware import _claims_pass_domain_policy

        monkeypatch.delenv("OAUTH_ALLOWED_EMAIL_DOMAINS", raising=False)
        ok, _ = _claims_pass_domain_policy({}, "anyone@anywhere.com")
        assert ok is True

    def test_email_verified_false_always_rejected(self, monkeypatch):
        from auth.auth_info_middleware import _claims_pass_domain_policy

        monkeypatch.delenv("OAUTH_ALLOWED_EMAIL_DOMAINS", raising=False)
        ok, reason = _claims_pass_domain_policy(
            {"email_verified": False}, "u@otbgroup.co.uk"
        )
        assert ok is False
        assert "email_verified" in reason

    def test_hd_claim_match_accepted(self, monkeypatch):
        from auth.auth_info_middleware import _claims_pass_domain_policy

        monkeypatch.setenv("OAUTH_ALLOWED_EMAIL_DOMAINS", "otbgroup.co.uk")
        ok, _ = _claims_pass_domain_policy(
            {"hd": "otbgroup.co.uk"}, "u@otbgroup.co.uk"
        )
        assert ok is True

    def test_hd_claim_mismatch_rejected_even_if_email_matches(self, monkeypatch):
        """``hd`` is IdP-attested; if it disagrees with the email domain we
        trust ``hd`` and reject. Stops display-name spoofing tricks."""
        from auth.auth_info_middleware import _claims_pass_domain_policy

        monkeypatch.setenv("OAUTH_ALLOWED_EMAIL_DOMAINS", "otbgroup.co.uk")
        ok, reason = _claims_pass_domain_policy(
            {"hd": "evil.example"}, "u@otbgroup.co.uk"
        )
        assert ok is False
        assert "hd=" in reason

    def test_no_hd_falls_back_to_email_domain(self, monkeypatch):
        from auth.auth_info_middleware import _claims_pass_domain_policy

        monkeypatch.setenv("OAUTH_ALLOWED_EMAIL_DOMAINS", "otbgroup.co.uk")
        ok, _ = _claims_pass_domain_policy({}, "u@otbgroup.co.uk")
        assert ok is True

        ok, reason = _claims_pass_domain_policy({}, "u@evil.example")
        assert ok is False
        assert "evil.example" in reason

    def test_multiple_domains_allowed(self, monkeypatch):
        from auth.auth_info_middleware import _claims_pass_domain_policy

        monkeypatch.setenv(
            "OAUTH_ALLOWED_EMAIL_DOMAINS", "otbgroup.co.uk, partner.com"
        )
        for email in ["a@otbgroup.co.uk", "b@partner.com"]:
            ok, _ = _claims_pass_domain_policy({}, email)
            assert ok is True, email

    def test_missing_email_and_hd_rejected_when_allowlist_set(self, monkeypatch):
        from auth.auth_info_middleware import _claims_pass_domain_policy

        monkeypatch.setenv("OAUTH_ALLOWED_EMAIL_DOMAINS", "otbgroup.co.uk")
        ok, _ = _claims_pass_domain_policy({}, None)
        assert ok is False

    def test_case_insensitive(self, monkeypatch):
        from auth.auth_info_middleware import _claims_pass_domain_policy

        monkeypatch.setenv("OAUTH_ALLOWED_EMAIL_DOMAINS", "OTBgroup.CO.uk")
        ok, _ = _claims_pass_domain_policy(
            {"hd": "otbgroup.co.uk"}, "U@OTBGROUP.CO.UK"
        )
        assert ok is True


# ---------------------------------------------------------------------------
# Fix 5 — audit attribution per user
# ---------------------------------------------------------------------------


class TestAuditPerUserAttribution:
    @pytest.mark.asyncio
    async def test_flush_groups_by_user_and_uses_each_users_creds(self, monkeypatch):
        """Two users in the same flush window → two distinct sheets clients,
        each user's rows appended via that user's own credentials. The
        pre-fix code used the first user's token for everyone."""
        from core import audit

        monkeypatch.setattr(audit, "AUDIT_SHEET_ID", "sheet-test")

        clients_built_for: list[str] = []

        def fake_build_for_user(self, email):
            clients_built_for.append(email)
            sheets = MagicMock()
            sheets.spreadsheets.return_value.values.return_value.append.return_value.execute = MagicMock(
                return_value={}
            )
            sheets.close = MagicMock()
            return sheets

        monkeypatch.setattr(
            audit.AuditLogger, "_build_sheets_for_user", fake_build_for_user
        )
        monkeypatch.setattr(
            audit.AuditLogger,
            "_ensure_tab",
            lambda self, sheets, tab: None,
        )

        logger = audit.AuditLogger()
        logger._current_tab = "9999-99"  # force _ensure_tab path to run once

        batch = [
            {"user": "alice@otb.co.uk", "tool": "x", "service": "drive"},
            {"user": "bob@otb.co.uk", "tool": "y", "service": "gmail"},
            {"user": "alice@otb.co.uk", "tool": "z", "service": "drive"},
        ]
        await logger._flush(batch)

        # Each distinct user got its own client; ordering doesn't matter.
        assert sorted(clients_built_for) == ["alice@otb.co.uk", "bob@otb.co.uk"]

    @pytest.mark.asyncio
    async def test_flush_raises_when_some_users_have_no_creds(self, monkeypatch):
        """Partial credential failures must surface — _loop's exception
        handler falls those rows back to stdout."""
        from core import audit

        monkeypatch.setattr(audit, "AUDIT_SHEET_ID", "sheet-test")

        def fake_build(self, email):
            if email == "alice@otb.co.uk":
                sheets = MagicMock()
                sheets.spreadsheets.return_value.values.return_value.append.return_value.execute = MagicMock(
                    return_value={}
                )
                sheets.close = MagicMock()
                return sheets
            return None  # bob has no creds

        monkeypatch.setattr(audit.AuditLogger, "_build_sheets_for_user", fake_build)
        monkeypatch.setattr(
            audit.AuditLogger, "_ensure_tab", lambda self, sheets, tab: None
        )

        logger = audit.AuditLogger()
        logger._current_tab = "9999-99"

        batch = [
            {"user": "alice@otb.co.uk", "tool": "x"},
            {"user": "bob@otb.co.uk", "tool": "y"},
        ]
        with pytest.raises(RuntimeError, match="no usable user creds"):
            await logger._flush(batch)

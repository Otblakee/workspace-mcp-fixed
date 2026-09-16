"""Regression tests for the token-expiry window bug (2026-09-16).

In OAuth 2.1 proxy mode this server never holds a Google refresh token: the
MCP client owns the refresh. google-auth treats a token as expired
REFRESH_THRESHOLD (3 min 45 s) before its declared expiry and calls
``refresh()`` before every request in that window, which raised
``RefreshError("The credentials do not contain the necessary fields ...")``
while the token was still good. Every tool call, and every audit flush,
failed for the last 3 min 45 s of each token hour.

Fix: only declare an expiry to google-auth when a refresh token exists, and
explain the residual (true-expiry) failure honestly instead of sending the
user to re-consent.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastmcp.server.auth import AccessToken
from google.auth.exceptions import RefreshError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth import oauth21_session_store as store_mod  # noqa: E402
from auth.oauth21_session_store import OAuth21SessionStore  # noqa: E402
from auth.service_decorator import _handle_token_refresh_error  # noqa: E402

GOOGLE_AUTH_MISSING_FIELDS = (
    "The credentials do not contain the necessary fields need to refresh the "
    "access token. You must specify refresh_token, token_uri, client_id, and "
    "client_secret."
)

EMAIL = "oliver@otbgroup.co.uk"


def _in_refresh_window() -> datetime:
    """An aware expiry two minutes ahead: inside google-auth's 3m45s threshold."""
    return datetime.now(timezone.utc) + timedelta(minutes=2)


def _access_token(expires_at: datetime | None = _in_refresh_window()) -> AccessToken:
    return AccessToken(
        token="ya29.live-token",
        client_id="client-id",
        scopes=["https://www.googleapis.com/auth/gmail.labels"],
        expires_at=int(expires_at.timestamp()) if expires_at else None,
        claims={"email": EMAIL},
    )


@pytest.fixture
def provider(monkeypatch):
    """Minimal auth provider so _build_credentials_from_provider takes the
    provider path, plus an isolated session store."""
    monkeypatch.setattr(store_mod, "_auth_provider", object())
    monkeypatch.setattr(
        store_mod, "_resolve_client_credentials", lambda: ("cid", "secret")
    )
    monkeypatch.setattr(store_mod, "is_external_oauth21_provider", lambda: False)
    fresh = OAuth21SessionStore()
    monkeypatch.setattr(store_mod, "get_oauth21_session_store", lambda: fresh)
    return fresh


class TestProviderCredentialsInsideRefreshWindow:
    def test_not_reported_expired_two_minutes_before_expiry(self, provider):
        creds = store_mod._build_credentials_from_provider(_access_token())
        assert creds is not None
        assert creds.refresh_token is None
        assert creds.expiry is None
        assert creds.expired is False
        assert creds.valid is True

    def test_ensure_session_returns_usable_creds_and_keeps_expiry_for_bookkeeping(
        self, provider
    ):
        expires_at = _in_refresh_window()
        creds = store_mod.ensure_session_from_access_token(
            _access_token(expires_at), EMAIL, mcp_session_id="mcp-1"
        )
        assert creds is not None and creds.valid is True

        info = provider.get_session_info(EMAIL)
        assert info is not None
        stored = info["expiry"]
        assert stored is not None and stored.tzinfo is None
        assert abs(stored - expires_at.replace(tzinfo=None)) < timedelta(seconds=2)

    def test_store_rebuild_without_refresh_token_is_not_expired(self, provider):
        # This is the path the audit writer uses (_resolve_credentials ->
        # store.get_credentials). It must not fail 3m45s early either.
        store_mod.ensure_session_from_access_token(_access_token(), EMAIL)
        creds = provider.get_credentials(EMAIL)
        assert creds is not None
        assert creds.expiry is None
        assert creds.expired is False

    def test_fallback_bearer_credentials_declare_no_expiry(self, provider):
        creds = store_mod.get_credentials_from_token("ya29.other", user_email=None)
        assert creds is not None
        assert creds.refresh_token is None
        assert creds.expiry is None
        assert creds.valid is True


class TestExpiryStillDeclaredWhenRefreshable:
    def test_refresh_token_present_keeps_expiry_and_early_refresh(self, provider):
        provider.store_session(
            user_email=EMAIL,
            access_token="ya29.x",
            refresh_token="1//refresh",
            client_id="cid",
            client_secret="secret",
            scopes=["s"],
            expiry=_in_refresh_window(),
            session_id="google_" + EMAIL,
        )
        creds = provider.get_credentials(EMAIL)
        assert creds is not None
        assert creds.refresh_token == "1//refresh"
        assert creds.expiry is not None
        # google-auth may pre-refresh here; it can, so that is correct.
        assert creds.expired is True

    def test_helper_rules(self):
        soon = _in_refresh_window()
        assert store_mod._google_auth_expiry(soon, None) is None
        assert store_mod._google_auth_expiry(soon, "") is None
        declared = store_mod._google_auth_expiry(soon, "1//refresh")
        assert declared is not None and declared.tzinfo is None


class TestRefreshErrorMessage:
    def test_missing_refresh_token_is_explained_not_blamed_on_login(self):
        msg = _handle_token_refresh_error(
            RefreshError(GOOGLE_AUTH_MISSING_FIELDS), EMAIL, "gmail"
        )
        assert "reconnect" in msg.lower()
        assert "retry" in msg.lower()
        assert "not a revoked login" in msg.lower()
        assert "expired or been revoked" not in msg.lower()
        assert "sign in via your mcp client" not in msg.lower()

    def test_invalid_grant_path_unchanged(self):
        msg = _handle_token_refresh_error(
            RefreshError("invalid_grant: Token has been expired or revoked."),
            EMAIL,
            "gmail",
        )
        assert "expired or been revoked" in msg.lower()

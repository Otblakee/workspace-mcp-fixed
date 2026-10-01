"""RefreshError inside google-auth's early-expiry window is a retry, not a
re-authorisation.

The per-request OAuth 2.1 credentials carry no refresh token (the proxy owns
refreshing). When google-auth tried to refresh them anyway, the handler
returned "sign in again", which sent the owner chasing a problem that was not
there (30 September 2026). It now says to retry.
"""

from __future__ import annotations

import sys
from pathlib import Path

from google.auth.exceptions import RefreshError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth.service_decorator import (  # noqa: E402
    _handle_token_refresh_error,
    _is_missing_refresh_fields_error,
)

MISSING = RefreshError(
    "The credentials do not contain the necessary fields need to refresh the "
    "access token. You must specify refresh_token, token_uri, client_id, and "
    "client_secret."
)


def test_missing_fields_error_is_recognised():
    assert _is_missing_refresh_fields_error(MISSING)
    assert not _is_missing_refresh_fields_error(RefreshError("invalid_grant: Bad"))


def test_missing_fields_message_says_retry_not_sign_in(monkeypatch):
    monkeypatch.setenv("MCP_ENABLE_OAUTH21", "true")
    msg = _handle_token_refresh_error(MISSING, "oli@example.test", "drive")
    assert "Retry the call" in msg
    assert "about to expire" in msg
    assert not msg.startswith("Authentication error occurred")


def test_invalid_grant_still_asks_for_sign_in(monkeypatch):
    monkeypatch.setenv("MCP_ENABLE_OAUTH21", "true")
    msg = _handle_token_refresh_error(
        RefreshError("invalid_grant: Token has been expired or revoked."),
        "oli@example.test",
        "drive",
    )
    assert "Token Expired/Revoked" in msg

"""The 401 challenge must name the FULL scope set (2026-09-28 outage).

FastMCP 4 fills ``WWW-Authenticate: Bearer scope="..."`` from
``required_scopes``. This server keeps the gate identity-only and carries the
enabled-service scopes as ``valid_scopes``; claude.ai requests exactly the
scopes the challenge names, so the stock GoogleProvider made a fresh sign-in
consent to identity only and every Workspace tool failed with "lack required
scopes". ``WorkspaceGoogleProvider`` widens the challenge while the verifier
gate, the metadata and the per-tool scope checks are unchanged.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IDENTITY = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
]
FULL = IDENTITY + [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


@pytest.fixture
def provider():
    from auth.google_provider import WorkspaceGoogleProvider

    return WorkspaceGoogleProvider(
        client_id="test-client-id.apps.googleusercontent.com",
        client_secret="test-secret",
        base_url="https://mcp.example.test",
        redirect_path="/oauth2callback",
        required_scopes=IDENTITY,
        valid_scopes=FULL,
    )


class TestWorkspaceGoogleProvider:
    def test_gate_stays_identity_only(self, provider):
        assert sorted(provider.required_scopes) == sorted(IDENTITY)

    def test_challenge_names_the_full_valid_set(self, provider):
        assert sorted(provider.challenge_scopes) == sorted(FULL)
        assert sorted(provider.get_challenge_scopes()) == sorted(FULL)
        assert sorted(provider.get_challenge_scopes(IDENTITY)) == sorted(FULL)

    def test_explicit_narrower_challenge_passes_through(self, provider):
        one = ["https://www.googleapis.com/auth/drive"]
        assert provider.get_challenge_scopes(one) == one

    def test_metadata_still_advertises_the_full_set(self, provider):
        assert sorted(provider.scopes_supported) == sorted(FULL)

    def test_without_valid_scopes_challenge_is_required_scopes(self):
        from auth.google_provider import WorkspaceGoogleProvider

        p = WorkspaceGoogleProvider(
            client_id="x.apps.googleusercontent.com",
            client_secret="s",
            base_url="https://mcp.example.test",
            required_scopes=IDENTITY,
        )
        assert sorted(p.challenge_scopes) == sorted(IDENTITY)


class TestLive401Challenge:
    def test_unauthenticated_mcp_request_is_challenged_with_full_scopes(self, provider):
        from fastmcp import FastMCP
        from starlette.testclient import TestClient

        app = FastMCP("t", auth=provider).http_app(transport="streamable-http")
        with TestClient(app) as client:
            resp = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert resp.status_code == 401
        header = resp.headers["www-authenticate"]
        match = re.search(r'scope="([^"]*)"', header)
        assert match, header
        assert set(match.group(1).split()) == set(FULL)


class TestServerWiring:
    def test_core_server_uses_the_workspace_provider(self):
        src = (
            Path(__file__).resolve().parent.parent / "core" / "server.py"
        ).read_text()
        assert "provider = WorkspaceGoogleProvider(" in src
        assert "provider = GoogleProvider(" not in src

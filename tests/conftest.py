"""Shared test isolation.

Two families of tests read the *process* environment on the code path under
test (the group access policy and the audit writer). A developer with the
Render values exported locally, or a CI job with them set, otherwise gets
spurious failures and, for ``AUDIT_SA_JSON_B64``, a live token exchange
against oauth2.googleapis.com from inside the test run. Clear them for every
test; tests that need a value set it explicitly with ``monkeypatch``.
"""

from __future__ import annotations

import pytest

_ISOLATED_ENV = (
    "MCP_GROUP_POLICY_MODE",
    "MCP_GROUP_POLICY_FILE",
    "MCP_GROUP_POLICY_SA_JSON_FILE",
    "MCP_GROUP_POLICY_SA_JSON_B64",
    "MCP_GROUP_POLICY_SUBJECT",
    "MCP_GROUP_POLICY_STATIC_MEMBERS",
    "MCP_GROUP_POLICY_BREAKGLASS_EMAILS",
    "MCP_GROUP_POLICY_CACHE_TTL_S",
    "MCP_GROUP_POLICY_STALE_TTL_S",
    "MCP_TOOL_RATE_LIMITS",
    "MCP_ALLOWED_CLIENT_REDIRECT_URIS",
    "MCP_OAUTH_REFRESH_TOKEN_TTL_S",
    "AUDIT_SA_JSON_FILE",
    "AUDIT_SA_JSON_B64",
    "MCP_SINGLE_USER_MODE",
)


@pytest.fixture(autouse=True)
def _isolate_deployment_env(monkeypatch):
    for name in _ISOLATED_ENV:
        monkeypatch.delenv(name, raising=False)
    try:
        from core import access_policy as ap
    except Exception:  # pragma: no cover - import problems surface elsewhere
        yield
        return
    ap.set_engine(None)
    yield
    ap.set_engine(None)

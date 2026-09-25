"""Wiring tests for the opt-in ``gsignatures`` service.

Asserts that the five tools register, sit at the core tier of the
``gsignatures`` section in ``core/tool_tiers.yaml``, that the service is
opt-in (``main.OPT_IN_TOOLS``) and unblocked, that every write tool defaults
to a dry run and needs a separate confirm, that neither ``fastmcp_server.py``
nor the start-up banner touch the feature, that no OAuth scope is requested
for it, and that the package source never reaches the Gmail settings it must
not (sharing scope, forwarding, delegates, vacation, send-as create/delete/
verify).
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import gsignatures.signature_tools as signature_tools  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PACKAGE = REPO_ROOT / "gsignatures"

TOOLS = {
    "preview_email_signature",
    "get_email_signatures",
    "set_email_signature",
    "apply_email_signatures",
    "audit_email_signatures",
}
WRITE_TOOLS = {"set_email_signature", "apply_email_signatures"}
READ_TOOLS = TOOLS - WRITE_TOOLS


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _code_only(source: str) -> str:
    """Source with comments and docstrings removed."""
    no_comments = "\n".join(
        ln for ln in source.splitlines() if not re.match(r"^\s*#", ln)
    )
    return re.sub(r'(?s)"""(.*?)"""', "", no_comments)


class TestRegistration:
    @pytest.fixture(scope="class")
    def registered(self):
        from core.server import server
        from core.tool_registry import get_tool_components

        return get_tool_components(server)

    def test_all_five_tools_registered(self, registered):
        assert TOOLS <= set(registered)

    def test_module_exposes_exactly_five_tools(self):
        seen = set()
        for name, obj in vars(signature_tools).items():
            if name.startswith("_") or not callable(obj):
                continue
            impl = _unwrap(obj)
            if inspect.iscoroutinefunction(impl) and impl.__module__ == (
                signature_tools.__name__
            ):
                seen.add(name)
        assert seen == TOOLS

    def test_no_tool_takes_user_google_email_or_a_service(self):
        """These tools use the delegated service account, never the caller's
        OAuth token, so they must not carry the require_google_service shape."""
        for name in TOOLS:
            params = list(
                inspect.signature(_unwrap(getattr(signature_tools, name))).parameters
            )
            assert "user_google_email" not in params
            assert "service" not in params
        # The docstring explains why the decorator is absent; the code must
        # not use it.
        src = _code_only((PACKAGE / "signature_tools.py").read_text())
        assert "require_google_service" not in src

    def test_write_tools_default_to_dry_run_and_no_confirm(self):
        for name in WRITE_TOOLS:
            sig = inspect.signature(_unwrap(getattr(signature_tools, name)))
            assert sig.parameters["dry_run"].default is True, name
            assert sig.parameters["confirm"].default is False, name

    def test_read_tools_have_no_write_switches(self):
        for name in READ_TOOLS:
            params = inspect.signature(
                _unwrap(getattr(signature_tools, name))
            ).parameters
            assert "confirm" not in params, name
            assert "dry_run" not in params, name


class TestTierAndPolicy:
    def test_tool_tiers_section(self):
        data = yaml.safe_load((REPO_ROOT / "core" / "tool_tiers.yaml").read_text())
        section = data["gsignatures"]
        assert set(section["core"]) == TOOLS
        assert section.get("extended") == []
        assert section.get("complete") == []

    def test_tier_loader_resolves_the_service(self):
        from core.tool_tier_loader import ToolTierLoader

        loader = ToolTierLoader()
        assert set(loader.get_tools_up_to_tier("core", ["gsignatures"])) == TOOLS

    def test_not_blocked(self):
        from core.tool_policy import BLOCKED_TOOLS

        assert not (TOOLS & BLOCKED_TOOLS)

    def test_audit_tags_the_module(self):
        from core.audit import _service

        for name in TOOLS:
            assert _service(name, "gsignatures.signature_tools") == "gsignatures"


class TestMainWiring:
    def test_service_is_opt_in(self):
        import main

        assert "gsignatures" in main.OPT_IN_TOOLS

    def test_import_and_icon_and_choice(self):
        src = (REPO_ROOT / "main.py").read_text()
        assert (
            '"gsignatures": lambda: import_module("gsignatures.signature_tools")' in src
        )
        assert '"gsignatures": "' in src  # icon entry
        assert '"gsignatures",' in src  # --tools choice

    def test_banner_prints_no_signature_setting(self):
        src = (REPO_ROOT / "main.py").read_text()
        assert "SIGNATURE_" not in src

    def test_fastmcp_server_does_not_import_the_service(self):
        src = (REPO_ROOT / "fastmcp_server.py").read_text()
        assert "gsignatures" not in src
        # Same rule as the other opt-in service.
        assert "gadmin" not in src


class TestScopes:
    def test_no_oauth_scope_is_requested(self):
        from auth import scopes

        assert scopes.TOOL_SCOPES_MAP["gsignatures"] == []
        assert scopes.TOOL_READONLY_SCOPES_MAP["gsignatures"] == []
        assert set(scopes.get_scopes_for_tools(["gsignatures"])) == set(
            scopes.BASE_SCOPES
        )

    def test_delegated_scopes_are_the_three_documented(self):
        from gsignatures.sa_auth import DELEGATED_SCOPES

        assert DELEGATED_SCOPES == [
            "https://www.googleapis.com/auth/gmail.settings.basic",
            "https://www.googleapis.com/auth/admin.directory.user.readonly",
            "https://www.googleapis.com/auth/admin.directory.group.member.readonly",
        ]


class TestPackageSourceIsNarrow:
    @pytest.fixture(scope="class")
    def sources(self):
        return {p: p.read_text() for p in sorted(PACKAGE.glob("*.py"))}

    def test_sharing_scope_mentioned_once_and_only_as_a_refusal(self, sources):
        hits = {
            p.name: src.count("settings.sharing")
            for p, src in sources.items()
            if "settings.sharing" in src
        }
        assert hits == {"sa_auth.py": 1}
        assert "deliberately not used" in sources[PACKAGE / "sa_auth.py"]

    @pytest.mark.parametrize(
        "pattern",
        [
            r"forwarding\w*\(\)",
            r"delegates\(\)",
            r"vacation\w*\(\)",
            r"getVacation|updateVacation",
            r"sendAs\(\)\s*\.\s*create\s*\(",
            r"sendAs\(\)\s*\.\s*delete\s*\(",
            r"sendAs\(\)\s*\.\s*verify\s*\(",
            r"sendAs\(\)\s*\.\s*update\s*\(",
            r"\.smimeInfo\(",
            r"\.filters\(\)",
            r"\.autoForwarding|\.imap\(\)|\.pop\(\)|\.language\(\)",
        ],
    )
    def test_no_forbidden_gmail_settings_call(self, sources, pattern):
        for path, src in sources.items():
            assert re.search(pattern, _code_only(src)) is None, (
                f"{path.name} matches {pattern!r}"
            )

    def test_only_patch_get_and_list_on_send_as(self, sources):
        methods = set()
        for src in sources.values():
            for m in re.finditer(r"sendAs\(\)\s*\.\s*(\w+)\s*\(", _code_only(src)):
                methods.add(m.group(1))
        assert methods == {"list", "get", "patch"}

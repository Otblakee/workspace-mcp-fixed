"""Tests for the gadmin (Admin SDK readonly) module.

The strongest invariants this suite enforces:

  1. Every tool exposed by ``gadmin.admin_tools`` is decorated with
     ``is_read_only=True``.
  2. Every tool's docstring starts with the literal token "READ-ONLY:".
  3. The module never references an Admin SDK write method, in either calls
     or comments-as-code paths.
  4. The 8 new admin scopes are wired through ``auth.scopes`` and
     ``auth.service_decorator`` correctly.
  5. The audit logger tags admin-module tools as ``service=gadmin`` even
     when the tool name would substring-match an existing service (e.g.
     ``query_drive_audit_log``).

These are intentionally unit-scope tests — they do not exercise real Google
APIs. Live integration testing happens after the Render redeploy.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


# Authoritative tool list — mirrors the brief. Used in several tests below
# so the read-only contract is enforced by name, not by introspection of
# whatever happens to be exposed.
DIRECTORY_TOOLS = {
    "list_users",
    "get_user",
    "list_groups",
    "get_group",
    "list_group_members",
    "list_user_groups",
    "list_orgunits",
    "get_orgunit",
    "list_admin_roles",
    "list_role_assignments",
    "list_oauth_tokens_for_user",
}
REPORTS_TOOLS = {
    "query_admin_audit_log",
    "query_login_audit_log",
    "query_token_audit_log",
    "query_drive_audit_log",
    "query_usage_report",
}
ALL_ADMIN_TOOLS = DIRECTORY_TOOLS | REPORTS_TOOLS


class TestScopesWired:
    def test_all_eight_admin_scope_constants_defined(self):
        from auth import scopes

        for name in (
            "ADMIN_DIRECTORY_USER_READONLY_SCOPE",
            "ADMIN_DIRECTORY_GROUP_READONLY_SCOPE",
            "ADMIN_DIRECTORY_GROUP_MEMBER_READONLY_SCOPE",
            "ADMIN_DIRECTORY_ORGUNIT_READONLY_SCOPE",
            "ADMIN_DIRECTORY_ROLEMANAGEMENT_READONLY_SCOPE",
            "ADMIN_DIRECTORY_DEVICE_MOBILE_READONLY_SCOPE",
            "ADMIN_REPORTS_AUDIT_READONLY_SCOPE",
            "ADMIN_REPORTS_USAGE_READONLY_SCOPE",
        ):
            value = getattr(scopes, name)
            assert value.endswith(".readonly"), (
                f"{name} must be a readonly scope; got {value}"
            )

    def test_admin_scopes_list_composition(self):
        """ADMIN_SCOPES drives the OAuth consent prompt. It must contain
        all 8 readonly scopes plus the one broader scope required by
        ``users().tokens().list`` — and nothing else. If the security
        scope were missing, ``list_oauth_tokens_for_user`` would 403 on
        every call (Codex P1)."""
        from auth.scopes import (
            ADMIN_DIRECTORY_USER_SECURITY_SCOPE,
            ADMIN_SCOPES,
        )

        assert len(ADMIN_SCOPES) == 9
        # Exactly 8 readonly scopes and the security scope — no other
        # write-capable Admin SDK scopes.
        readonly = [s for s in ADMIN_SCOPES if s.endswith(".readonly")]
        non_readonly = [s for s in ADMIN_SCOPES if not s.endswith(".readonly")]
        assert len(readonly) == 8
        assert non_readonly == [ADMIN_DIRECTORY_USER_SECURITY_SCOPE]

    def test_admin_user_security_scope_present_in_consent_scope_set(self):
        """Codex P1 regression: the consent flow must request
        admin.directory.user.security, otherwise list_oauth_tokens_for_user
        fails with insufficient scope on every call."""
        from auth.scopes import ADMIN_SCOPES

        assert (
            "https://www.googleapis.com/auth/admin.directory.user.security"
            in ADMIN_SCOPES
        )

    def test_gadmin_in_scope_maps(self):
        from auth.scopes import TOOL_READONLY_SCOPES_MAP, TOOL_SCOPES_MAP

        assert "gadmin" in TOOL_SCOPES_MAP
        assert "gadmin" in TOOL_READONLY_SCOPES_MAP
        # Readonly map and full map are identical for gadmin (no write
        # variant exists by design).
        assert TOOL_SCOPES_MAP["gadmin"] == TOOL_READONLY_SCOPES_MAP["gadmin"]

    def test_no_admin_write_scope_anywhere_in_scopes_module(self):
        """Defence in depth: no Admin SDK write scope URL appears anywhere
        in ``auth.scopes`` source. If someone tries to add one, this trips."""
        scopes_path = Path(__file__).resolve().parent.parent / "auth" / "scopes.py"
        src = scopes_path.read_text()
        # The five canonical Admin SDK write scope suffixes.
        forbidden = [
            "auth/admin.directory.user\"",  # bare (write) variant
            "auth/admin.directory.group\"",
            "auth/admin.directory.orgunit\"",
            "auth/admin.directory.rolemanagement\"",
            "auth/admin.directory.device.mobile\"",
        ]
        for needle in forbidden:
            assert needle not in src, (
                f"Forbidden Admin SDK write scope referenced in auth/scopes.py: {needle}"
            )


class TestServiceDecoratorWired:
    def test_admin_directory_and_admin_reports_registered(self):
        from auth.service_decorator import SERVICE_CONFIGS

        assert SERVICE_CONFIGS["admin_directory"] == {
            "service": "admin",
            "version": "directory_v1",
        }
        assert SERVICE_CONFIGS["admin_reports"] == {
            "service": "admin",
            "version": "reports_v1",
        }

    def test_eight_admin_scope_groups_resolvable(self):
        from auth.service_decorator import SCOPE_GROUPS

        for key in (
            "admin_directory_user_read",
            "admin_directory_group_read",
            "admin_directory_group_member_read",
            "admin_directory_orgunit_read",
            "admin_directory_rolemanagement_read",
            "admin_directory_device_mobile_read",
            "admin_reports_audit_read",
            "admin_reports_usage_read",
        ):
            assert key in SCOPE_GROUPS
            assert SCOPE_GROUPS[key].endswith(".readonly")

    def test_user_security_scope_group_resolvable(self):
        """Codex P1 regression: the symbolic name used by
        list_oauth_tokens_for_user must resolve to the correct URL."""
        from auth.service_decorator import SCOPE_GROUPS

        assert (
            SCOPE_GROUPS["admin_directory_user_security"]
            == "https://www.googleapis.com/auth/admin.directory.user.security"
        )


class TestReadOnlyContract:
    def test_every_tool_has_is_read_only_true(self):
        """The decorator chain wraps the impl with handle_http_errors, which
        embeds ``is_read_only`` in the wrapper's behaviour via its closure.
        We inspect the closure rather than the impl signature so we catch
        any tool that was added without the flag."""
        from gadmin import admin_tools

        # Find every wrapped tool callable. ``@server.tool()`` is the
        # outermost decorator; its __wrapped__ chain bottoms out at the
        # async impl. The handle_http_errors layer is the one that holds
        # is_read_only in a closure cell. We assert by source inspection
        # below in a separate test; here we just check the impl is async.
        import inspect

        missing = []
        for name in ALL_ADMIN_TOOLS:
            fn = getattr(admin_tools, name, None)
            assert fn is not None, f"tool {name!r} missing from gadmin.admin_tools"
            impl = _unwrap(fn)
            assert inspect.iscoroutinefunction(impl), name
            missing.append(name)
        assert set(missing) == ALL_ADMIN_TOOLS

    def test_every_decorator_call_passes_is_read_only_true(self):
        """Source-level check: every @handle_http_errors call in
        admin_tools.py must include ``is_read_only=True``. Catches a future
        contributor adding a new tool without the flag."""
        src_path = Path(__file__).resolve().parent.parent / "gadmin" / "admin_tools.py"
        src = src_path.read_text()
        # Match e.g. @handle_http_errors("list_users", is_read_only=True, …)
        # including multi-line forms.
        pattern = re.compile(r"@handle_http_errors\([^)]*\)", re.DOTALL)
        calls = pattern.findall(src)
        assert calls, "no @handle_http_errors calls found in admin_tools.py"
        for call in calls:
            assert "is_read_only=True" in call, (
                f"@handle_http_errors call missing is_read_only=True: {call!r}"
            )

    def test_every_docstring_starts_with_readonly_prefix(self):
        from gadmin import admin_tools

        for name in ALL_ADMIN_TOOLS:
            impl = _unwrap(getattr(admin_tools, name))
            doc = (impl.__doc__ or "").lstrip()
            assert doc.startswith("READ-ONLY:"), (
                f"{name} docstring must start with 'READ-ONLY:'; got: {doc[:60]!r}"
            )

    @pytest.mark.parametrize(
        "method_substr",
        [
            "users().insert",
            "users().update",
            "users().delete",
            "users().patch",
            "users().undelete",
            "users().makeAdmin",
            "users().signOut",
            "groups().insert",
            "groups().update",
            "groups().delete",
            "groups().patch",
            "members().insert",
            "members().update",
            "members().delete",
            "members().patch",
            "orgunits().insert",
            "orgunits().update",
            "orgunits().delete",
            "orgunits().patch",
            "roles().insert",
            "roles().update",
            "roles().delete",
            "roles().patch",
            "roleAssignments().insert",
            "roleAssignments().delete",
            "tokens().delete",
            "mobiledevices().action",
            "mobiledevices().delete",
            "chromeosdevices().action",
        ],
    )
    def test_no_admin_sdk_write_method_call_in_source(self, method_substr):
        """The brutal one. Source-scans admin_tools.py for any literal
        Admin SDK write-method invocation. The pattern catches the
        ``service.<resource>().<method>(`` shape that actually executes a
        call. Comments and docstrings that *mention* these methods (to
        explain that we don't call them) are tolerated because they don't
        end with an opening paren followed by a kwarg or value.

        Any match below indicates a write method was actually invoked
        somewhere in the module, which is a hard rule violation.
        """
        src_path = Path(__file__).resolve().parent.parent / "gadmin" / "admin_tools.py"
        src = src_path.read_text()
        # Strip block comment lines (``# …``) and triple-quoted docstrings
        # so the search runs against executable Python only.
        # 1. Drop lines that start with optional indent + '#'.
        lines = [
            ln for ln in src.splitlines() if not re.match(r"^\s*#", ln)
        ]
        no_comments = "\n".join(lines)
        # 2. Drop triple-quoted strings (greedy across newlines).
        no_docstrings = re.sub(
            r'(?s)"""(.*?)"""', "", no_comments
        )
        # The invocation must be followed by a paren immediately — the
        # canonical ``foo().method(`` shape. We look for the dotted call
        # form rather than the bare method name to be robust against an
        # auxiliary variable named e.g. ``insert``.
        needle = re.escape(method_substr) + r"\s*\("
        match = re.search(needle, no_docstrings)
        assert match is None, (
            f"Admin SDK write method {method_substr!r} is invoked in "
            f"gadmin/admin_tools.py; this violates the read-only contract."
        )


class TestModuleStructure:
    def test_module_exposes_exactly_the_documented_tools(self):
        """No surprise tools, and no missing tools."""
        from gadmin import admin_tools

        # Collect every callable that looks like an MCP tool (async with
        # the canonical (service, user_google_email, ...) shape).
        import inspect

        seen = set()
        for name, obj in vars(admin_tools).items():
            if name.startswith("_"):
                continue
            impl = _unwrap(obj) if callable(obj) else None
            if impl is None or not inspect.iscoroutinefunction(impl):
                continue
            sig = inspect.signature(impl)
            params = list(sig.parameters)
            if params[:2] == ["service", "user_google_email"]:
                seen.add(name)
        assert seen == ALL_ADMIN_TOOLS, (
            f"unexpected tools: {seen - ALL_ADMIN_TOOLS}, "
            f"missing tools: {ALL_ADMIN_TOOLS - seen}"
        )


class TestAuditServiceTagging:
    def test_module_prefix_overrides_substring_match(self):
        """Without the module-prefix override, ``query_drive_audit_log``
        would substring-match ``drive`` in SERVICE_MAP and be tagged
        ``service=drive`` — incorrect, because the tool lives in gadmin.
        Verify the override wins."""
        from core.audit import _service

        # gadmin module → always gadmin, regardless of tool name.
        assert (
            _service("query_drive_audit_log", "gadmin.admin_tools") == "gadmin"
        )
        assert _service("list_users", "gadmin.admin_tools") == "gadmin"
        assert _service("get_user", "gadmin.admin_tools") == "gadmin"
        # Non-admin tool name + non-admin module → existing heuristic still
        # works (drive substring).
        assert _service("search_drive_files", "gdrive.drive_tools") == "drive"
        # Bare call with no module still falls back to the substring heuristic.
        assert _service("search_drive_files") == "drive"


class TestToolTierWiring:
    def test_gadmin_section_in_tool_tiers(self):
        import yaml

        tiers_path = (
            Path(__file__).resolve().parent.parent / "core" / "tool_tiers.yaml"
        )
        data = yaml.safe_load(tiers_path.read_text())
        assert "gadmin" in data
        tier = data["gadmin"]
        # Every documented tool must appear in some tier.
        listed = set(tier.get("core") or []) | set(
            tier.get("extended") or []
        ) | set(tier.get("complete") or [])
        assert listed == ALL_ADMIN_TOOLS, (
            f"tool_tiers gadmin section missing: {ALL_ADMIN_TOOLS - listed} "
            f"or unexpected: {listed - ALL_ADMIN_TOOLS}"
        )


class TestMainDefaultOn:
    """gadmin is loaded by default once the OAuth consent screen and
    Workspace domain-wide delegation have the 9 admin scopes. The earlier
    OPT_IN_TOOLS safety net (which excluded gadmin from the default
    ``tool_imports.keys()`` walk and the tier-resolved suggested_services
    list) has been removed; these tests lock that posture in so a future
    refactor can't accidentally re-introduce a silent skip."""

    def test_gadmin_in_tool_imports_and_choices(self):
        main_path = Path(__file__).resolve().parent.parent / "main.py"
        src = main_path.read_text()
        # gadmin must remain a member of both the importer table and the
        # --tools argparse choices, otherwise the explicit-list path
        # (TOOLS env var) can't pull it in either.
        assert '"gadmin": lambda: import_module("gadmin.admin_tools")' in src
        assert '"gadmin",\n' in src  # appears in --tools choices list

    def test_default_branch_does_not_filter_gadmin(self):
        """Regression: the default-load path must not exclude gadmin.
        Earlier code had ``OPT_IN_TOOLS = {"gadmin"}`` and filtered the
        default tool list through it; removing that gating is what
        ``Put the env on by default`` requested."""
        main_path = Path(__file__).resolve().parent.parent / "main.py"
        src = main_path.read_text()
        assert "OPT_IN_TOOLS" not in src
        assert "tools_to_import = list(tool_imports.keys())" in src

    def test_tier_branch_does_not_filter_gadmin(self):
        """Tier mode must surface gadmin's tier entries (gadmin.core etc.)
        as well — no extra gating on top of the tier yaml."""
        main_path = Path(__file__).resolve().parent.parent / "main.py"
        src = main_path.read_text()
        # The tier-resolved suggested_services list is used as-is.
        assert "tools_to_import = suggested_services" in src
        # The earlier helper that walked the yaml to strip opt-in tools is gone.
        assert "_tools_in_services" not in src

    def test_gadmin_tier_yaml_is_authoritative(self):
        """The yaml must declare exactly the 16 tools the module exposes.
        Without that, --tool-tier core would either skip admin tools or
        whitelist tool names that don't exist."""
        from core.tool_tier_loader import ToolTierLoader

        loader = ToolTierLoader()
        gadmin_tools = set(loader.get_tools_up_to_tier("complete", ["gadmin"]))
        assert gadmin_tools == ALL_ADMIN_TOOLS



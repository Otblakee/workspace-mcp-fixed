"""Tool profiles: the everyday / admin connector split (v1.18.0).

The membership of ``ADMIN_TOOLS`` is pinned against the source modules so a
new Directory, signature, migration or banner tool cannot drift onto the
everyday connector unnoticed, and the filter is exercised end to end.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _decorated_tools(rel_path: str) -> set:
    tree = ast.parse((REPO_ROOT / rel_path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool":
                    names.add(node.name)
    return names


ADMIN_ONLY_MODULES = [
    "gadmin/admin_tools.py",
    "gadmin/admin_group_tools.py",
    "gsignatures/signature_tools.py",
    "gdrive/shared_drive_theme_tools.py",
]


class TestMembership:
    @pytest.mark.parametrize("module", ADMIN_ONLY_MODULES)
    def test_every_tool_in_admin_only_modules_is_admin(self, module):
        from core.tool_profiles import ADMIN_TOOLS

        missing = _decorated_tools(module) - ADMIN_TOOLS
        assert not missing, f"{module}: not in ADMIN_TOOLS: {sorted(missing)}"

    def test_migration_engine_is_admin_except_the_metadata_read(self):
        from core.tool_profiles import ADMIN_TOOLS

        tools = _decorated_tools("gdrive/drive_migration_tools.py")
        assert "get_drive_file_metadata" in tools
        assert "get_drive_file_metadata" not in ADMIN_TOOLS
        missing = (tools - {"get_drive_file_metadata"}) - ADMIN_TOOLS
        assert not missing, sorted(missing)

    def test_shared_drive_build_is_admin_but_routing_helpers_are_not(self):
        from core.tool_profiles import ADMIN_TOOLS

        tools = _decorated_tools("gdrive/shared_drive_tools.py")
        assert {"list_shared_drives", "create_shortcut"} <= tools
        assert "list_shared_drives" not in ADMIN_TOOLS
        assert "create_shortcut" not in ADMIN_TOOLS
        expected_admin = tools - {"list_shared_drives", "create_shortcut"}
        assert expected_admin <= ADMIN_TOOLS, sorted(expected_admin - ADMIN_TOOLS)

    @pytest.mark.parametrize(
        "module",
        [
            "gmail/gmail_tools.py",
            "gcalendar/calendar_tools.py",
            "gdocs/docs_tools.py",
            "gsheets/sheets_tools.py",
            "gcontacts/contacts_tools.py",
            "gdrive/drive_tools.py",
        ],
    )
    def test_everyday_modules_have_no_admin_tool(self, module):
        from core.tool_profiles import ADMIN_TOOLS

        leaked = _decorated_tools(module) & ADMIN_TOOLS
        assert not leaked, (
            f"{module}: everyday module lists admin tools {sorted(leaked)}"
        )

    def test_admin_tools_exist_in_tiers_and_are_not_blocked(self):
        from core.tool_policy import BLOCKED_TOOLS
        from core.tool_profiles import ADMIN_TOOLS

        tiers = yaml.safe_load(
            (REPO_ROOT / "core" / "tool_tiers.yaml").read_text(encoding="utf-8")
        )
        declared = set()
        for service in tiers.values():
            if isinstance(service, dict):
                for names in service.values():
                    declared.update(names or [])
        unknown = ADMIN_TOOLS - declared
        assert not unknown, (
            f"ADMIN_TOOLS names not in tool_tiers.yaml: {sorted(unknown)}"
        )
        assert not (ADMIN_TOOLS & set(BLOCKED_TOOLS))


class TestProfileSemantics:
    @pytest.fixture(autouse=True)
    def _reset(self):
        from core.tool_profiles import set_tool_profile

        yield
        set_tool_profile("all")

    def test_default_is_all(self):
        from core.tool_profiles import get_tool_profile, set_tool_profile

        assert set_tool_profile(None) == "all"
        assert set_tool_profile("") == "all"
        assert get_tool_profile() == "all"

    def test_unknown_profile_is_rejected(self):
        from core.tool_profiles import set_tool_profile

        with pytest.raises(ValueError):
            set_tool_profile("minimal")

    def test_excludes(self):
        from core.tool_profiles import profile_excludes, set_tool_profile

        assert not profile_excludes("walk_drive")  # all
        set_tool_profile("everyday")
        assert profile_excludes("walk_drive")
        assert not profile_excludes("search_drive_files")
        set_tool_profile("Admin")  # case-insensitive
        assert not profile_excludes("walk_drive")
        assert profile_excludes("search_drive_files")


class _StubProvider:
    def __init__(self):
        self.removed = []

    def remove_tool(self, name):
        self.removed.append(name)


class _StubServer:
    def __init__(self):
        self.local_provider = _StubProvider()


class TestFilterIntegration:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        import core.tool_registry as registry
        from core.tool_profiles import set_tool_profile

        components = {
            "search_drive_files": object(),
            "walk_drive": object(),
            "list_users": object(),
            "create_shortcut": object(),
        }
        monkeypatch.setattr(registry, "get_tool_components", lambda server: components)
        monkeypatch.setattr(registry, "is_oauth21_enabled", lambda: False)
        monkeypatch.setattr(registry, "is_read_only_mode", lambda: False)
        registry.set_enabled_tools(None)
        yield registry
        set_tool_profile("all")

    def test_all_profile_removes_nothing(self, _env):
        from core.tool_profiles import set_tool_profile

        set_tool_profile("all")
        server = _StubServer()
        _env.filter_server_tools(server)
        assert server.local_provider.removed == []

    def test_everyday_profile_removes_admin_tools_only(self, _env):
        from core.tool_profiles import set_tool_profile

        set_tool_profile("everyday")
        server = _StubServer()
        _env.filter_server_tools(server)
        assert sorted(server.local_provider.removed) == ["list_users", "walk_drive"]

    def test_admin_profile_keeps_admin_tools_only(self, _env):
        from core.tool_profiles import set_tool_profile

        set_tool_profile("admin")
        server = _StubServer()
        _env.filter_server_tools(server)
        assert sorted(server.local_provider.removed) == [
            "create_shortcut",
            "search_drive_files",
        ]

    def test_profile_never_restores_a_tier_removed_tool(self, _env):
        from core.tool_profiles import set_tool_profile

        set_tool_profile("admin")
        _env.set_enabled_tools({"list_users"})  # tier keeps only one
        server = _StubServer()
        _env.filter_server_tools(server)
        assert sorted(server.local_provider.removed) == [
            "create_shortcut",
            "search_drive_files",
            "walk_drive",
        ]


class TestDeployWiring:
    def test_dockerfile_passes_the_profile(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert 'ENV TOOL_PROFILE=""' in dockerfile
        assert "${TOOL_PROFILE:+--tool-profile" in dockerfile

    def test_main_accepts_the_flag(self):
        src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        assert '"--tool-profile"' in src
        assert 'choices=["all", "everyday", "admin"]' in src

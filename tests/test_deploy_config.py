"""Guards for the deployment/config fixes in fix/deploy-and-test-config.

Covers what's unit-testable of the deploy-surface fixes:
- version lookup resolves the real distribution name (workspace-mcp-fixed)
  with fallback, never returning "dev" on a properly installed environment;
- main.py rejects an explicitly-empty --tools list instead of booting a
  "healthy" server with zero tools;
- direct imports (uvicorn, starlette, pydantic) are declared in
  pyproject [project] dependencies, not just present transitively;
- tests/gmail is a real package (has __init__.py) like every other
  tests/ subpackage.
"""

from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - repo targets >=3.10
    tomllib = None

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Version lookup (main.py banner + /health endpoint)
# ---------------------------------------------------------------------------

class TestPackageVersionLookup:
    def _patch_metadata(self, monkeypatch, mapping):
        import core.server as server_mod

        def fake_version(name):
            if name in mapping:
                return mapping[name]
            raise metadata.PackageNotFoundError(name)

        monkeypatch.setattr(server_mod.metadata, "version", fake_version)

    def test_prefers_fixed_distribution_name(self, monkeypatch):
        from core.server import get_package_version

        self._patch_metadata(
            monkeypatch,
            {"workspace-mcp-fixed": "1.13.1", "workspace-mcp": "9.9.9"},
        )
        assert get_package_version() == "1.13.1"

    def test_falls_back_to_upstream_name(self, monkeypatch):
        from core.server import get_package_version

        self._patch_metadata(monkeypatch, {"workspace-mcp": "1.13.0"})
        assert get_package_version() == "1.13.0"

    def test_dev_only_when_nothing_installed(self, monkeypatch):
        from core.server import get_package_version

        self._patch_metadata(monkeypatch, {})
        assert get_package_version() == "dev"

    def test_real_environment_reports_non_dev(self):
        """In the synced venv the distribution is installed, so the previous
        bug (looking up the upstream name 'workspace-mcp' only) is what made
        /health report 'dev'. The fixed lookup must find the real version."""
        from core.server import get_package_version

        assert get_package_version() != "dev"

    def test_main_uses_shared_helper(self):
        import main as main_mod
        from core.server import get_package_version

        assert main_mod.get_package_version is get_package_version


# ---------------------------------------------------------------------------
# Empty --tools guard (Dockerfile ${TOOLS:+--tools $TOOLS} with blank TOOLS)
# ---------------------------------------------------------------------------

class TestEmptyToolsGuard:
    def test_empty_list_exits(self):
        from main import validate_tools_argument

        with pytest.raises(SystemExit) as excinfo:
            validate_tools_argument([])
        assert excinfo.value.code == 2

    def test_none_passes(self):
        from main import validate_tools_argument

        validate_tools_argument(None)  # must not raise

    def test_populated_list_passes(self):
        from main import validate_tools_argument

        validate_tools_argument(["gmail", "drive"])  # must not raise


# ---------------------------------------------------------------------------
# Direct dependencies declared in pyproject
# ---------------------------------------------------------------------------

class TestDeclaredDependencies:
    def test_direct_imports_are_declared(self):
        assert tomllib is not None, "Python >=3.11 expected in the dev venv"
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        deps = pyproject["project"]["dependencies"]
        for package in ("uvicorn", "starlette", "pydantic"):
            assert any(
                dep.split(">=")[0].split("==")[0].strip() == package
                for dep in deps
            ), f"{package} is imported directly but not declared in [project] dependencies"


# ---------------------------------------------------------------------------
# tests/gmail packaging
# ---------------------------------------------------------------------------

class TestGmailTestPackage:
    def test_init_exists(self):
        assert (REPO_ROOT / "tests" / "gmail" / "__init__.py").is_file()

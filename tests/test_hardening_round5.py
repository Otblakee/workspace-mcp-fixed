"""Fifth hardening round: OAuth store key-rotation resilience, Google
sign-in domain hint, refresh-token lifetime cap, single-user env guard under
OAuth 2.1, Dependabot configuration, dependency floors for published
advisories, and process-environment isolation of the policy engine."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestProviderHardeningKwargs:
    def test_nothing_set_is_empty(self):
        from core.server import _provider_hardening_kwargs

        assert _provider_hardening_kwargs({}) == {}

    def test_single_domain_becomes_hd_hint(self):
        from core.server import _provider_hardening_kwargs

        out = _provider_hardening_kwargs(
            {"OAUTH_ALLOWED_EMAIL_DOMAINS": " OTBgroup.co.uk "}
        )
        assert out == {"extra_authorize_params": {"hd": "otbgroup.co.uk"}}

    def test_several_domains_no_hint(self):
        from core.server import _provider_hardening_kwargs

        out = _provider_hardening_kwargs(
            {"OAUTH_ALLOWED_EMAIL_DOMAINS": "otbgroup.co.uk,jitlogistics.co.uk"}
        )
        assert "extra_authorize_params" not in out

    def test_refresh_ttl_parsed(self):
        from core.server import _provider_hardening_kwargs

        out = _provider_hardening_kwargs({"MCP_OAUTH_REFRESH_TOKEN_TTL_S": "2592000"})
        assert out == {"fallback_refresh_token_expiry_seconds": 2592000}

    @pytest.mark.parametrize("bad", ["0", "-5", "30d", "abc", "1.5"])
    def test_bad_refresh_ttl_ignored_with_warning(self, bad, caplog):
        from core.server import _provider_hardening_kwargs

        with caplog.at_level(logging.WARNING):
            out = _provider_hardening_kwargs({"MCP_OAUTH_REFRESH_TOKEN_TTL_S": bad})
        assert "fallback_refresh_token_expiry_seconds" not in out
        assert "MCP_OAUTH_REFRESH_TOKEN_TTL_S" in caplog.text

    def test_provider_construction_uses_helper(self):
        src = (REPO_ROOT / "core" / "server.py").read_text()
        assert "hardening_kwargs = _provider_hardening_kwargs()" in src
        assert "provider_kwargs.update(hardening_kwargs)" in src

    def test_fastmcp_accepts_the_kwargs(self):
        import inspect

        from fastmcp.server.auth.providers.google import GoogleProvider

        params = inspect.signature(GoogleProvider.__init__).parameters
        assert "extra_authorize_params" in params
        assert "fallback_refresh_token_expiry_seconds" in params


class TestOAuthStoreKeyRotation:
    def test_every_fernet_wrapper_misses_instead_of_raising(self):
        src = (REPO_ROOT / "core" / "server.py").read_text()
        constructions = src.count("FernetEncryptionWrapper(")
        assert constructions >= 2  # disk and Valkey backends
        assert src.count("raise_on_decryption_error=False") == constructions

    def test_wrapper_supports_the_flag(self):
        import inspect

        from key_value.aio.wrappers.encryption.base import BaseEncryptionWrapper

        assert (
            "raise_on_decryption_error"
            in inspect.signature(BaseEncryptionWrapper.__init__).parameters
        )


class TestSingleUserGuard:
    def test_env_var_ignored_under_oauth21(self, monkeypatch, caplog):
        from auth import google_auth

        monkeypatch.setenv("MCP_SINGLE_USER_MODE", "1")
        monkeypatch.setattr(google_auth, "is_oauth21_enabled", lambda: True)
        with caplog.at_level(logging.WARNING):
            assert google_auth._single_user_mode_active() is False
        assert "MCP_SINGLE_USER_MODE=1 ignored" in caplog.text

    def test_env_var_honoured_in_legacy_mode(self, monkeypatch):
        from auth import google_auth

        monkeypatch.setenv("MCP_SINGLE_USER_MODE", "1")
        monkeypatch.setattr(google_auth, "is_oauth21_enabled", lambda: False)
        assert google_auth._single_user_mode_active() is True

    def test_unset_is_off(self, monkeypatch):
        from auth import google_auth

        monkeypatch.setattr(google_auth, "is_oauth21_enabled", lambda: False)
        assert google_auth._single_user_mode_active() is False

    def test_loader_uses_the_guard(self):
        src = (REPO_ROOT / "auth" / "google_auth.py").read_text()
        assert 'if os.getenv("MCP_SINGLE_USER_MODE") == "1":' not in src
        assert "if _single_user_mode_active():" in src

    @pytest.mark.parametrize(
        "flag,env,expected",
        [
            (True, None, True),
            (False, "1", True),
            (False, "true", True),
            (False, "0", False),
            (False, None, False),
        ],
    )
    def test_main_counts_env_as_single_user(self, monkeypatch, flag, env, expected):
        import main

        if env is None:
            monkeypatch.delenv("MCP_SINGLE_USER_MODE", raising=False)
        else:
            monkeypatch.setenv("MCP_SINGLE_USER_MODE", env)
        assert (
            main._single_user_requested(SimpleNamespace(single_user=flag)) is expected
        )


class TestDependencyHygiene:
    @staticmethod
    def _locked(pkg: str):
        text = (REPO_ROOT / "uv.lock").read_text()
        m = re.search(
            r"\[\[package\]\]\nname = \"%s\"\nversion = \"([^\"]+)\"" % re.escape(pkg),
            text,
        )
        assert m, f"{pkg} not in uv.lock"
        return tuple(int(x) for x in m.group(1).split(".")[:3])

    def test_mcp_carries_session_principal_fix(self):
        # CVE-2026-52869 (GHSA-jpw9-pfvf-9f58): HTTP transports served session
        # requests without checking the authenticated principal; fixed in 1.27.2.
        assert self._locked("mcp") >= (1, 27, 2)

    def test_python_multipart_carries_dos_fixes(self):
        assert self._locked("python-multipart") >= (0, 0, 32)

    def test_dependabot_covers_uv_actions_and_docker(self):
        cfg = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text())
        ecosystems = {u["package-ecosystem"] for u in cfg["updates"]}
        assert {"uv", "github-actions", "docker"} <= ecosystems
        assert "" not in ecosystems


class TestBlueprint:
    def test_allowed_domains_cover_every_signin_domain(self):
        """One Workspace customer, two staff sign-in domains. A single-domain
        value locks JIT staff out (enumerated from the live Directory)."""
        doc = yaml.safe_load((REPO_ROOT / "render.yaml").read_text())
        env = {
            e["key"]: e.get("value")
            for svc in doc["services"]
            for e in svc.get("envVars", [])
        }
        domains = {d.strip() for d in env["OAUTH_ALLOWED_EMAIL_DOMAINS"].split(",")}
        assert {"otbgroup.co.uk", "jit-logistics.com"} <= domains
        assert "blakefamily.uk" not in domains  # alias domain, nobody signs in
        assert "arthistorywithemily.co.uk" not in domains  # personal, excluded

    def test_refresh_token_ttl_is_thirty_days(self):
        doc = yaml.safe_load((REPO_ROOT / "render.yaml").read_text())
        env = {
            e["key"]: e.get("value")
            for svc in doc["services"]
            for e in svc.get("envVars", [])
        }
        assert env.get("MCP_OAUTH_REFRESH_TOKEN_TTL_S") == "2592000"


class TestPolicyEngineEnvIsolation:
    def test_explicit_environ_never_reads_process_env(self, monkeypatch):
        from core import access_policy as ap

        monkeypatch.setenv(ap.FILE_ENV, "/nonexistent/policy.yaml")
        engine = ap.AccessPolicyEngine.from_env({ap.MODE_ENV: "off"})
        assert engine.policy_error is None
        assert engine.policy is not None

    def test_explicit_environ_path_is_used(self, monkeypatch, tmp_path):
        from core import access_policy as ap

        monkeypatch.delenv(ap.FILE_ENV, raising=False)
        with pytest.raises(ap.PolicyError, match="not found"):
            ap.AccessPolicyEngine.from_env(
                {ap.MODE_ENV: "enforce", ap.FILE_ENV: str(tmp_path / "missing.yaml")}
            )

    def test_conftest_clears_deployment_env(self):
        import os

        for name in (
            "MCP_GROUP_POLICY_FILE",
            "MCP_GROUP_POLICY_MODE",
            "AUDIT_SA_JSON_B64",
            "AUDIT_SA_JSON_FILE",
        ):
            assert name not in os.environ


class TestRepositoryMove:
    @pytest.mark.parametrize(
        "artefact",
        [
            ".github/workflows/publish-mcp-registry.yml",
            "smithery.yaml",
            "glama.json",
            "manifest.json",
            "server.json",
            "README_NEW.md",
            "google_workspace_mcp.dxt",
        ],
    )
    def test_distribution_artefacts_are_gone(self, artefact):
        assert not (REPO_ROOT / artefact).exists()

    def test_package_is_named_for_otb(self):
        import tomllib

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        assert data["project"]["name"] == "otb-workspace-mcp"
        assert "otb-workspace-mcp" in data["project"]["scripts"]
        assert "Otblakee/otb-workspace-mcp" in data["project"]["urls"]["Repository"]

"""Unit tests for the signature service-account auth helpers.

Covers:

* loading the service-account JSON from a file path, from inline JSON, and
  the precedence between the two;
* the failure messages when nothing is configured, when the JSON is
  malformed, and when the JSON is not a service-account key, none of which
  may echo key material;
* the exact delegated scope list the owner pastes into the Admin console;
* credential construction (impersonation subject and scopes) and the client
  builds (``cache_discovery=False``), all fully mocked;
* the caller allowlist and its default;
* the log redaction helper;
* a source scan proving the Gmail sharing scope is never requested, and a
  second scan for the house rule on dashes.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from auth.scopes import (  # noqa: E402
    ADMIN_DIRECTORY_GROUP_MEMBER_READONLY_SCOPE,
    ADMIN_DIRECTORY_USER_READONLY_SCOPE,
    GMAIL_SETTINGS_BASIC_SCOPE,
)
from gsignatures import sa_auth  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# A fake key. The private_key value is a recognisable marker so the tests can
# assert it never leaks into an error message. It is not a real key.
FAKE_PRIVATE_KEY = (
    "-----BEGIN PRIVATE KEY-----\nFAKEKEYMATERIAL0000\n-----END PRIVATE KEY-----\n"
)
FAKE_SA_INFO = {
    "type": "service_account",
    "project_id": "otb-fake-project",
    "private_key_id": "fakekeyid0123",
    "private_key": FAKE_PRIVATE_KEY,
    "client_email": "sig-writer@otb-fake-project.iam.gserviceaccount.com",
    "client_id": "000000000000000000000",
    "token_uri": "https://oauth2.googleapis.com/token",
}

ALL_ENV = (
    sa_auth.ENV_SA_FILE,
    sa_auth.ENV_SA_JSON,
    sa_auth.ENV_DIRECTORY_ADMIN,
    sa_auth.ENV_ADMIN_EMAILS,
    sa_auth.ENV_LEDGER_SHEET_ID,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ALL_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def sa_file(tmp_path):
    path = tmp_path / "sa.json"
    path.write_text(json.dumps(FAKE_SA_INFO), encoding="utf-8")
    return path


class TestEnvNames:
    def test_env_names_are_the_documented_ones(self):
        assert sa_auth.ENV_SA_FILE == "SIGNATURE_SERVICE_ACCOUNT_FILE"
        assert sa_auth.ENV_SA_JSON == "SIGNATURE_SERVICE_ACCOUNT_JSON"
        assert sa_auth.ENV_DIRECTORY_ADMIN == "SIGNATURE_DIRECTORY_ADMIN"
        assert sa_auth.ENV_ADMIN_EMAILS == "SIGNATURE_ADMIN_EMAILS"
        assert sa_auth.ENV_LEDGER_SHEET_ID == "SIGNATURE_LEDGER_SHEET_ID"


class TestLoadServiceAccountInfo:
    def test_loads_from_file_path(self, monkeypatch, sa_file):
        monkeypatch.setenv(sa_auth.ENV_SA_FILE, str(sa_file))
        info = sa_auth.load_service_account_info()
        assert info == FAKE_SA_INFO
        assert sa_auth.service_account_email() == FAKE_SA_INFO["client_email"]

    def test_loads_from_inline_json_when_file_var_unset(self, monkeypatch):
        monkeypatch.setenv(sa_auth.ENV_SA_JSON, json.dumps(FAKE_SA_INFO))
        assert sa_auth.load_service_account_info() == FAKE_SA_INFO

    def test_file_var_wins_when_both_set(self, monkeypatch, sa_file):
        inline = dict(
            FAKE_SA_INFO, client_email="inline@example.iam.gserviceaccount.com"
        )
        monkeypatch.setenv(sa_auth.ENV_SA_FILE, str(sa_file))
        monkeypatch.setenv(sa_auth.ENV_SA_JSON, json.dumps(inline))
        loaded = sa_auth.load_service_account_info()
        assert loaded["client_email"] == FAKE_SA_INFO["client_email"]

    def test_neither_set_names_both_env_vars_and_holds_no_key_material(self):
        with pytest.raises(sa_auth.SignatureAuthError) as excinfo:
            sa_auth.load_service_account_info()
        message = str(excinfo.value)
        assert sa_auth.ENV_SA_FILE in message
        assert sa_auth.ENV_SA_JSON in message
        assert "PRIVATE KEY" not in message
        assert "FAKEKEYMATERIAL" not in message

    def test_missing_file_is_a_clean_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv(sa_auth.ENV_SA_FILE, str(tmp_path / "does-not-exist.json"))
        with pytest.raises(sa_auth.SignatureAuthError) as excinfo:
            sa_auth.load_service_account_info()
        assert sa_auth.ENV_SA_FILE in str(excinfo.value)

    def test_malformed_json_from_file(self, monkeypatch, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text('{"type": "service_account", FAKEKEYMATERIAL', encoding="utf-8")
        monkeypatch.setenv(sa_auth.ENV_SA_FILE, str(path))
        with pytest.raises(sa_auth.SignatureAuthError) as excinfo:
            sa_auth.load_service_account_info()
        assert "FAKEKEYMATERIAL" not in str(excinfo.value)

    def test_malformed_inline_json_does_not_echo_the_value(self, monkeypatch):
        monkeypatch.setenv(sa_auth.ENV_SA_JSON, "not json FAKEKEYMATERIAL")
        with pytest.raises(sa_auth.SignatureAuthError) as excinfo:
            sa_auth.load_service_account_info()
        assert "FAKEKEYMATERIAL" not in str(excinfo.value)

    def test_wrong_type_is_refused_without_echoing_key(self, monkeypatch):
        wrong = dict(FAKE_SA_INFO, type="authorized_user")
        monkeypatch.setenv(sa_auth.ENV_SA_JSON, json.dumps(wrong))
        with pytest.raises(sa_auth.SignatureAuthError) as excinfo:
            sa_auth.load_service_account_info()
        message = str(excinfo.value)
        assert "service_account" in message
        assert "FAKEKEYMATERIAL" not in message

    def test_json_that_is_not_an_object_is_refused(self, monkeypatch):
        monkeypatch.setenv(sa_auth.ENV_SA_JSON, json.dumps(["service_account"]))
        with pytest.raises(sa_auth.SignatureAuthError):
            sa_auth.load_service_account_info()

    def test_missing_client_email_is_refused(self, monkeypatch):
        incomplete = {k: v for k, v in FAKE_SA_INFO.items() if k != "client_email"}
        monkeypatch.setenv(sa_auth.ENV_SA_JSON, json.dumps(incomplete))
        with pytest.raises(sa_auth.SignatureAuthError) as excinfo:
            sa_auth.load_service_account_info()
        assert "client_email" in str(excinfo.value)


class TestDelegatedScopes:
    def test_exactly_three_scopes_in_order(self):
        assert sa_auth.DELEGATED_SCOPES == [
            GMAIL_SETTINGS_BASIC_SCOPE,
            ADMIN_DIRECTORY_USER_READONLY_SCOPE,
            ADMIN_DIRECTORY_GROUP_MEMBER_READONLY_SCOPE,
        ]

    def test_no_sharing_scope_and_no_directory_write_scope(self):
        for scope in sa_auth.DELEGATED_SCOPES:
            assert "sharing" not in scope
            if "admin.directory" in scope:
                assert scope.endswith(".readonly"), scope

    def test_sheets_scope_constant(self):
        assert sa_auth.SHEETS_SCOPE == "https://www.googleapis.com/auth/spreadsheets"


class TestCredentialConstruction:
    def test_delegated_credentials_impersonates_subject_with_scopes(self, monkeypatch):
        monkeypatch.setenv(sa_auth.ENV_SA_JSON, json.dumps(FAKE_SA_INFO))
        base = MagicMock(name="base-creds")
        delegated = MagicMock(name="delegated-creds")
        base.with_subject.return_value = delegated
        with patch("google.oauth2.service_account.Credentials") as creds_cls:
            creds_cls.from_service_account_info.return_value = base
            result = sa_auth.delegated_credentials(
                "alice@otbgroup.co.uk", [GMAIL_SETTINGS_BASIC_SCOPE]
            )
        creds_cls.from_service_account_info.assert_called_once_with(
            FAKE_SA_INFO, scopes=[GMAIL_SETTINGS_BASIC_SCOPE]
        )
        base.with_subject.assert_called_once_with("alice@otbgroup.co.uk")
        assert result is delegated

    def test_delegated_credentials_requires_subject_and_scopes(self, monkeypatch):
        monkeypatch.setenv(sa_auth.ENV_SA_JSON, json.dumps(FAKE_SA_INFO))
        with patch("google.oauth2.service_account.Credentials"):
            with pytest.raises(sa_auth.SignatureAuthError):
                sa_auth.delegated_credentials("", [GMAIL_SETTINGS_BASIC_SCOPE])
            with pytest.raises(sa_auth.SignatureAuthError):
                sa_auth.delegated_credentials("alice@otbgroup.co.uk", [])

    def test_service_account_credentials_has_no_subject(self, monkeypatch):
        monkeypatch.setenv(sa_auth.ENV_SA_JSON, json.dumps(FAKE_SA_INFO))
        base = MagicMock(name="base-creds")
        with patch("google.oauth2.service_account.Credentials") as creds_cls:
            creds_cls.from_service_account_info.return_value = base
            result = sa_auth.service_account_credentials([sa_auth.SHEETS_SCOPE])
        creds_cls.from_service_account_info.assert_called_once_with(
            FAKE_SA_INFO, scopes=[sa_auth.SHEETS_SCOPE]
        )
        base.with_subject.assert_not_called()
        assert result is base

    def test_build_gmail_for_user(self, monkeypatch):
        monkeypatch.setenv(sa_auth.ENV_SA_JSON, json.dumps(FAKE_SA_INFO))
        base = MagicMock(name="base-creds")
        delegated = MagicMock(name="delegated-creds")
        base.with_subject.return_value = delegated
        with (
            patch("google.oauth2.service_account.Credentials") as creds_cls,
            patch("googleapiclient.discovery.build") as build,
        ):
            creds_cls.from_service_account_info.return_value = base
            build.return_value = "gmail-service"
            result = sa_auth.build_gmail_for_user("bob@jit-logistics.com")
        creds_cls.from_service_account_info.assert_called_once_with(
            FAKE_SA_INFO, scopes=[GMAIL_SETTINGS_BASIC_SCOPE]
        )
        base.with_subject.assert_called_once_with("bob@jit-logistics.com")
        build.assert_called_once_with(
            "gmail", "v1", credentials=delegated, cache_discovery=False
        )
        assert result == "gmail-service"

    def test_build_gmail_for_user_refuses_blank_email(self, monkeypatch):
        monkeypatch.setenv(sa_auth.ENV_SA_JSON, json.dumps(FAKE_SA_INFO))
        with (
            patch("google.oauth2.service_account.Credentials"),
            patch("googleapiclient.discovery.build") as build,
        ):
            with pytest.raises(sa_auth.SignatureAuthError):
                sa_auth.build_gmail_for_user("  ")
        build.assert_not_called()

    def test_build_directory_as_admin_uses_configured_admin(self, monkeypatch):
        monkeypatch.setenv(sa_auth.ENV_SA_JSON, json.dumps(FAKE_SA_INFO))
        base = MagicMock(name="base-creds")
        delegated = MagicMock(name="delegated-creds")
        base.with_subject.return_value = delegated
        with (
            patch("google.oauth2.service_account.Credentials") as creds_cls,
            patch("googleapiclient.discovery.build") as build,
        ):
            creds_cls.from_service_account_info.return_value = base
            sa_auth.build_directory_as_admin()
        creds_cls.from_service_account_info.assert_called_once_with(
            FAKE_SA_INFO,
            scopes=[
                ADMIN_DIRECTORY_USER_READONLY_SCOPE,
                ADMIN_DIRECTORY_GROUP_MEMBER_READONLY_SCOPE,
            ],
        )
        base.with_subject.assert_called_once_with("oliver@otbgroup.co.uk")
        build.assert_called_once_with(
            "admin", "directory_v1", credentials=delegated, cache_discovery=False
        )

    def test_build_sheets_as_service_account(self, monkeypatch):
        monkeypatch.setenv(sa_auth.ENV_SA_JSON, json.dumps(FAKE_SA_INFO))
        base = MagicMock(name="base-creds")
        with (
            patch("google.oauth2.service_account.Credentials") as creds_cls,
            patch("googleapiclient.discovery.build") as build,
        ):
            creds_cls.from_service_account_info.return_value = base
            sa_auth.build_sheets_as_service_account()
        creds_cls.from_service_account_info.assert_called_once_with(
            FAKE_SA_INFO, scopes=[sa_auth.SHEETS_SCOPE]
        )
        base.with_subject.assert_not_called()
        build.assert_called_once_with(
            "sheets", "v4", credentials=base, cache_discovery=False
        )


class TestAdminIdentities:
    def test_directory_admin_defaults_to_oliver(self):
        assert sa_auth.directory_admin_email() == "oliver@otbgroup.co.uk"

    def test_directory_admin_from_env_is_normalised(self, monkeypatch):
        monkeypatch.setenv(sa_auth.ENV_DIRECTORY_ADMIN, "  Admin@OTBgroup.co.uk ")
        assert sa_auth.directory_admin_email() == "admin@otbgroup.co.uk"

    def test_directory_admin_blank_env_is_refused(self, monkeypatch):
        monkeypatch.setenv(sa_auth.ENV_DIRECTORY_ADMIN, "   ")
        with pytest.raises(sa_auth.SignatureAuthError):
            sa_auth.directory_admin_email()

    def test_allowlist_default_is_oliver_only(self):
        assert sa_auth.allowed_admin_emails() == {"oliver@otbgroup.co.uk"}

    def test_allowlist_from_env_is_lower_cased_and_stripped(self, monkeypatch):
        monkeypatch.setenv(
            sa_auth.ENV_ADMIN_EMAILS,
            " Oliver@OTBGroup.co.uk , second@otbgroup.co.uk,, ",
        )
        assert sa_auth.allowed_admin_emails() == {
            "oliver@otbgroup.co.uk",
            "second@otbgroup.co.uk",
        }

    def test_allowlist_blank_env_is_refused(self, monkeypatch):
        monkeypatch.setenv(sa_auth.ENV_ADMIN_EMAILS, " , ")
        with pytest.raises(sa_auth.SignatureAuthError):
            sa_auth.allowed_admin_emails()

    def test_assert_caller_allowed_is_case_insensitive(self):
        sa_auth.assert_caller_allowed("Oliver@OTBGroup.co.uk")
        sa_auth.assert_caller_allowed(" oliver@otbgroup.co.uk ")

    def test_assert_caller_allowed_refuses_none_and_strangers(self):
        with pytest.raises(sa_auth.SignatureAuthError):
            sa_auth.assert_caller_allowed(None)
        with pytest.raises(sa_auth.SignatureAuthError):
            sa_auth.assert_caller_allowed("")
        with pytest.raises(sa_auth.SignatureAuthError):
            sa_auth.assert_caller_allowed("someone@otbgroup.co.uk")


class TestRedactForLog:
    @pytest.mark.parametrize(
        "value",
        [
            "oliver@otbgroup.co.uk",
            "/etc/secrets/service-account.json",
            "123456789",
            FAKE_PRIVATE_KEY,
        ],
    )
    def test_long_values_are_never_returned_unchanged(self, value):
        redacted = sa_auth.redact_for_log(value)
        assert redacted != value
        assert len(redacted) < len(value)

    def test_short_values_are_fully_masked(self):
        assert sa_auth.redact_for_log("abc") == "***"
        assert sa_auth.redact_for_log("") == "***"

    def test_none_is_tolerated(self):
        assert sa_auth.redact_for_log(None) == "***"

    def test_private_key_marker_does_not_survive(self):
        assert "FAKEKEYMATERIAL" not in sa_auth.redact_for_log(FAKE_PRIVATE_KEY)


class TestSourceScan:
    def test_sharing_scope_is_only_mentioned_as_not_used(self):
        package = REPO_ROOT / "gsignatures"
        hits = []
        for path in package.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in {".py", ".md", ".yaml", ".yml", ".html", ".txt"}:
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "gmail.settings.sharing" in line:
                    hits.append((path.name, lineno, line))
        assert len(hits) <= 1, hits
        for name, _lineno, line in hits:
            assert name == "sa_auth.py"
            assert "not" in line.lower()

    def test_no_dashes_in_signature_package_or_tests(self):
        # Built from code points so this file never contains the characters
        # it is checking for.
        pattern = re.compile("[" + chr(0x2013) + chr(0x2014) + "]")
        offenders = []
        for root in (REPO_ROOT / "gsignatures", REPO_ROOT / "tests" / "gsignatures"):
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix not in {".py", ".md", ".yaml", ".yml", ".html"}:
                    continue
                if pattern.search(path.read_text(encoding="utf-8")):
                    offenders.append(str(path))
        assert offenders == []

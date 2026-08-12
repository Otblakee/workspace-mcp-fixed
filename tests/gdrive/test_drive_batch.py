"""Unit tests for the shared migration plumbing in ``gdrive/drive_batch.py``.

Covers the three concerns that module owns: retry/backoff classification,
the groups-only permission guardrail, and manifest parsing / JSONL reporting.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from googleapiclient.errors import HttpError  # noqa: E402

from core.utils import UserInputError  # noqa: E402
from gdrive import drive_batch  # noqa: E402


def _http_error(status: int, reason: str = "") -> HttpError:
    resp = MagicMock()
    resp.status = status
    payload = {"error": {"errors": [{"reason": reason}] if reason else []}}
    return HttpError(resp, json.dumps(payload).encode("utf-8"))


# ---------------------------------------------------------------------------
# Retry classification
# ---------------------------------------------------------------------------


class TestRetryClassification:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_transient_statuses_are_retryable(self, status):
        assert drive_batch.is_retryable_http_error(_http_error(status)) is True

    def test_403_rate_limit_is_retryable(self):
        """Drive signals rate limiting as 403 + reason, not 429."""
        assert (
            drive_batch.is_retryable_http_error(
                _http_error(403, "userRateLimitExceeded")
            )
            is True
        )

    def test_403_permission_denied_is_not_retryable(self):
        """A real authorisation failure must surface immediately, not after
        five backoff sleeps."""
        assert (
            drive_batch.is_retryable_http_error(
                _http_error(403, "insufficientFilePermissions")
            )
            is False
        )

    @pytest.mark.parametrize("status", [400, 404, 409])
    def test_client_errors_are_not_retryable(self, status):
        assert drive_batch.is_retryable_http_error(_http_error(status)) is False


class TestExecuteWithBackoff:
    @pytest.mark.asyncio
    async def test_returns_result_without_retrying_on_success(self):
        request = MagicMock()
        request.execute.return_value = {"id": "abc"}
        factory = MagicMock(return_value=request)

        result = await drive_batch.execute_with_backoff(factory)

        assert result == {"id": "abc"}
        assert factory.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_transient_then_succeeds(self):
        good = MagicMock()
        good.execute.return_value = {"id": "abc"}
        bad = MagicMock()
        bad.execute.side_effect = _http_error(503)
        factory = MagicMock(side_effect=[bad, bad, good])

        with patch("gdrive.drive_batch.asyncio.sleep") as sleep:
            result = await drive_batch.execute_with_backoff(factory, base_delay=0.01)

        assert result == {"id": "abc"}
        assert factory.call_count == 3
        # A fresh request object per attempt — re-executing a consumed
        # googleapiclient request is not guaranteed safe.
        assert sleep.await_count == 2

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts(self):
        bad = MagicMock()
        bad.execute.side_effect = _http_error(503)
        factory = MagicMock(return_value=bad)

        with patch("gdrive.drive_batch.asyncio.sleep"):
            with pytest.raises(HttpError):
                await drive_batch.execute_with_backoff(factory, max_attempts=3)

        assert factory.call_count == 3

    @pytest.mark.asyncio
    async def test_non_retryable_raises_immediately(self):
        bad = MagicMock()
        bad.execute.side_effect = _http_error(404)
        factory = MagicMock(return_value=bad)

        with pytest.raises(HttpError):
            await drive_batch.execute_with_backoff(factory)

        assert factory.call_count == 1


class TestPaginate:
    @pytest.mark.asyncio
    async def test_drains_every_page(self):
        pages = [
            {"files": [{"id": "1"}, {"id": "2"}], "nextPageToken": "t1"},
            {"files": [{"id": "3"}], "nextPageToken": "t2"},
            {"files": [{"id": "4"}]},
        ]
        seen_tokens = []

        def factory(token):
            seen_tokens.append(token)
            request = MagicMock()
            request.execute.return_value = pages[len(seen_tokens) - 1]
            return request

        rows = await drive_batch.paginate(factory)

        assert [r["id"] for r in rows] == ["1", "2", "3", "4"]
        assert seen_tokens == [None, "t1", "t2"]

    @pytest.mark.asyncio
    async def test_max_items_truncates_and_warns(self, caplog):
        def factory(token):
            request = MagicMock()
            request.execute.return_value = {
                "files": [{"id": "a"}, {"id": "b"}],
                "nextPageToken": "more",
            }
            return request

        rows = await drive_batch.paginate(factory, max_items=2)

        assert len(rows) == 2
        assert any("max_items" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Groups-only guardrail
# ---------------------------------------------------------------------------


class TestResolvePrincipal:
    def test_defaults_to_group_type(self):
        assert drive_batch.resolve_principal("otb-premises@otbgroup.co.uk") == (
            "group",
            "otb-premises@otbgroup.co.uk",
        )

    def test_individual_requires_explicit_opt_in(self):
        """The declared type is what makes an individual grant fail closed:
        Drive rejects type=group for a personal address."""
        principal_type, email = drive_batch.resolve_principal(
            "oliver@otbgroup.co.uk", allow_individual=True
        )
        assert (principal_type, email) == ("user", "oliver@otbgroup.co.uk")

    @pytest.mark.parametrize("bad", ["", "   ", "not-an-email", "anyone", "domain"])
    def test_rejects_non_email_and_public_principals(self, bad):
        with pytest.raises(UserInputError):
            drive_batch.resolve_principal(bad)

    def test_domain_allowlist_blocks_external(self, monkeypatch):
        monkeypatch.setenv("DRIVE_PERMISSION_ALLOWED_DOMAINS", "otbgroup.co.uk")
        with pytest.raises(UserInputError, match="outside the allowed domains"):
            drive_batch.resolve_principal("someone@example.com")

    def test_domain_allowlist_permits_listed_domain(self, monkeypatch):
        monkeypatch.setenv(
            "DRIVE_PERMISSION_ALLOWED_DOMAINS", "otbgroup.co.uk, jitlogistics.co.uk"
        )
        assert drive_batch.resolve_principal("otb-media@otbgroup.co.uk")[1] == (
            "otb-media@otbgroup.co.uk"
        )

    def test_unset_allowlist_imposes_no_restriction(self, monkeypatch):
        monkeypatch.delenv("DRIVE_PERMISSION_ALLOWED_DOMAINS", raising=False)
        assert drive_batch.resolve_principal("team@example.com")[0] == "group"


class TestValidateDriveRole:
    @pytest.mark.parametrize(
        "role", ["organizer", "fileOrganizer", "writer", "commenter", "reader"]
    )
    def test_accepts_every_shared_drive_role(self, role):
        assert drive_batch.validate_drive_role(role) == role

    @pytest.mark.parametrize("role", ["owner", "OWNER", "editor", ""])
    def test_rejects_owner_and_unknown_roles(self, role):
        with pytest.raises(UserInputError):
            drive_batch.validate_drive_role(role)


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------


class TestParseManifest:
    def test_json_array(self):
        rows = drive_batch.parse_manifest('[{"source_id": "a"}, {"source_id": "b"}]')
        assert [r["source_id"] for r in rows] == ["a", "b"]

    def test_single_json_object(self):
        assert drive_batch.parse_manifest('{"source_id": "a"}') == [{"source_id": "a"}]

    def test_jsonl(self):
        rows = drive_batch.parse_manifest('{"source_id": "a"}\n\n{"source_id": "b"}\n')
        assert [r["source_id"] for r in rows] == ["a", "b"]

    def test_required_keys_enforced(self):
        with pytest.raises(UserInputError, match="missing"):
            drive_batch.parse_manifest(
                '[{"source_id": "a"}]', required_keys=("source_id", "dest_folder_id")
            )

    def test_rejects_both_inputs(self):
        with pytest.raises(UserInputError, match="not both"):
            drive_batch.parse_manifest("[]", "/tmp/x.jsonl")

    def test_rejects_neither_input(self):
        with pytest.raises(UserInputError, match="required"):
            drive_batch.parse_manifest()

    def test_rejects_malformed_jsonl_line(self):
        with pytest.raises(UserInputError, match="line 2"):
            drive_batch.parse_manifest('{"source_id": "a"}\nnot json{')

    def test_reads_from_file(self, tmp_path, monkeypatch):
        manifest = tmp_path / "m.jsonl"
        manifest.write_text('{"source_id": "a", "dest_folder_id": "d"}\n')
        monkeypatch.setenv("ALLOWED_FILE_DIRS", str(tmp_path))

        rows = drive_batch.parse_manifest(
            manifest_path=str(manifest), required_keys=("source_id",)
        )
        assert rows[0]["dest_folder_id"] == "d"


class TestWriteJsonlReport:
    def test_writes_rows_and_registers_attachment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ATTACHMENT_DIR", str(tmp_path))
        import core.attachment_storage as storage_mod

        monkeypatch.setattr(storage_mod, "STORAGE_DIR", tmp_path)
        monkeypatch.setattr(storage_mod, "_attachment_storage", None)

        rows = [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}]
        attachment_id, path, access_line = drive_batch.write_jsonl_report(
            rows, filename="test_report.jsonl"
        )

        written = Path(path).read_text().strip().splitlines()
        assert [json.loads(line)["id"] for line in written] == ["1", "2"]
        assert attachment_id
        assert access_line
        # 0600: reports can carry file names and paths from a whole drive.
        assert Path(path).stat().st_mode & 0o777 == 0o600

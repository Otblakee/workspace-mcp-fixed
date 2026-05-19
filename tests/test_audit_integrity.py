"""Tests for the Wave 2 audit-hardening fixes.

Three behaviour changes covered:

1. The audit decorator records ``status=error`` even when the tool returned
   a "successful" string/dict that actually represents a Google API failure
   (Fix 1 audit-layer defence). The tool-layer fix to stop swallowing
   ``HttpError`` in ``BatchOperationManager`` is also exercised by raising
   through the decorator.
2. The audit row carries a coarse ``client`` label derived from the
   User-Agent of the originating MCP request (Fix 2).
3. ``base64_content`` / ``fileUrl`` / ``attachments`` are deep-redacted by
   ``_redact`` (Fix 3 regression check).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Fix 1 — audit decorator records failures correctly
# ---------------------------------------------------------------------------


class _Captured:
    """Tiny side-channel: replaces logger().submit() so we can read the row."""

    def __init__(self):
        self.entries: list[dict] = []

    def submit(self, entry: dict) -> None:
        self.entries.append(entry)


@pytest.fixture
def captured_audit(monkeypatch):
    cap = _Captured()
    from core import audit

    monkeypatch.setattr(audit, "logger", lambda: cap)
    # Skip the FastMCP context lookup; we don't care about user attribution here.
    monkeypatch.setattr(audit, "_resolve_user_email",
                        lambda: _async_return("test@example.com"))
    monkeypatch.setattr(audit, "_resolve_client", lambda: "")
    return cap


async def _async_return(v):
    return v


class TestAuditFailureDetection:
    @pytest.mark.asyncio
    async def test_raised_exception_records_error(self, captured_audit):
        """Smoke check: a raised exception still produces status=error.
        This is the existing happy path — guarded so the new result-shape
        inspection didn't regress it."""
        from core.audit import audit_log

        @audit_log()
        async def boom(**kwargs):
            raise ValueError("boom")

        with pytest.raises(ValueError):
            await boom(document_id="d1")

        assert len(captured_audit.entries) == 1
        row = captured_audit.entries[0]
        assert row["status"] == "error"
        assert "ValueError" in row["error"]
        assert "boom" in row["error"]

    @pytest.mark.asyncio
    async def test_error_string_overrides_status_to_error(self, captured_audit):
        """A tool that swallowed a Google API failure and returned
        ``"Error: ..."`` must NOT show up in the audit log as success."""
        from core.audit import audit_log

        @audit_log()
        async def swallowed(**kwargs):
            return "Error: Invalid requests[0].insertText: Index -1 is not in [0, 1]."

        result = await swallowed(document_id="d1")
        assert result.startswith("Error:")

        assert len(captured_audit.entries) == 1
        row = captured_audit.entries[0]
        assert row["status"] == "error"
        assert "Index -1" in row["error"]

    @pytest.mark.asyncio
    async def test_batch_operation_failure_string_detected(self, captured_audit):
        """Mirror of the actual batch_update_doc swallow path: the manager
        returned ``Batch operation failed: ...`` and the tool wrapped it as
        ``Error: Batch operation failed: ...``. Both substrings flagged."""
        from core.audit import audit_log

        @audit_log()
        async def batchy(**kwargs):
            return "Error: Batch operation failed: <HttpError 400>"

        await batchy(document_id="d1")
        row = captured_audit.entries[0]
        assert row["status"] == "error"

    @pytest.mark.asyncio
    async def test_dict_with_top_level_error_key(self, captured_audit):
        """Google API JSON error shape: {"error": {"code": 400, ...}}."""
        from core.audit import audit_log

        @audit_log()
        async def jsonish(**kwargs):
            return {"error": {"code": 400, "message": "Bad request: index out of range"}}

        await jsonish(document_id="d1")
        row = captured_audit.entries[0]
        assert row["status"] == "error"
        assert "index out of range" in row["error"]

    @pytest.mark.asyncio
    async def test_replies_array_with_per_operation_error(self, captured_audit):
        """A batchUpdate response can come back 200 OK overall but with one
        of its ``replies`` carrying a per-operation error object."""
        from core.audit import audit_log

        @audit_log()
        async def batch_with_partial(**kwargs):
            return {
                "replies": [
                    {"insertText": {}},
                    {"error": {"message": "Invalid index"}},
                    {"insertText": {}},
                ],
            }

        await batch_with_partial(document_id="d1")
        row = captured_audit.entries[0]
        assert row["status"] == "error"
        assert "replies[1]" in row["error"]
        assert "Invalid index" in row["error"]

    @pytest.mark.asyncio
    async def test_clean_success_stays_success(self, captured_audit):
        """The happy path: a normal return is left alone."""
        from core.audit import audit_log

        @audit_log()
        async def ok(**kwargs):
            return "Successfully updated 3 paragraphs."

        await ok(document_id="d1")
        row = captured_audit.entries[0]
        assert row["status"] == "success"
        assert row["error"] == ""

    @pytest.mark.asyncio
    async def test_batch_manager_propagates_httperror(self, monkeypatch):
        """End-to-end-ish: ``BatchOperationManager.execute_batch_operations``
        must no longer swallow ``HttpError`` from the Google client. With
        ``except (ValueError, KeyError)`` in place, an HttpError raised by
        ``_execute_batch_requests`` propagates instead of collapsing to a
        ``(False, "...", {})`` tuple."""
        from gdocs.managers.batch_operation_manager import BatchOperationManager

        class _FakeHttpError(Exception):
            pass

        mgr = BatchOperationManager(service=object())

        async def _explode(*_args, **_kwargs):
            raise _FakeHttpError("Index -1 is not in [0, 1]")

        # Bypass validation; jump straight to the execute step.
        async def _stub_validate(_ops):
            return [{"insertText": {"location": {"index": -1}, "text": "x"}}], ["x"]

        monkeypatch.setattr(mgr, "_validate_and_build_requests", _stub_validate)
        monkeypatch.setattr(mgr, "_execute_batch_requests", _explode)

        with pytest.raises(_FakeHttpError):
            await mgr.execute_batch_operations("doc-1", [{"type": "insert_text"}])


# ---------------------------------------------------------------------------
# Fix 2 — client classification + column wiring
# ---------------------------------------------------------------------------


class TestClientClassification:
    def test_claude_code_pattern(self):
        from core.audit import _classify_client

        assert _classify_client("claude-code/1.2.3") == "claude-code"
        assert _classify_client("Claude Code (Linux)") == "claude-code"
        assert _classify_client("ClaudeCode/0.0.1") == "claude-code"

    def test_claude_web_pattern(self):
        from core.audit import _classify_client

        assert _classify_client(
            "Mozilla/5.0 ... claude.ai/web/2026"
        ) == "claude-web"
        assert _classify_client("Anthropic-Web/1.0") == "claude-web"

    def test_claude_desktop_pattern(self):
        from core.audit import _classify_client

        assert _classify_client("Anthropic-Claude-Desktop/2.1.0") == "claude-desktop"
        assert _classify_client("Claude Desktop/1.0 (macOS)") == "claude-desktop"

    def test_unknown_falls_back_to_snippet(self):
        from core.audit import _classify_client

        out = _classify_client("MysteryAgent/9.9 (custom integration)")
        assert out.startswith("other:")
        assert "MysteryAgent" in out
        # Capped — no half-page raw UAs in the sheet.
        assert len(out) <= len("other:") + 40

    def test_empty_or_none_returns_empty(self):
        from core.audit import _classify_client

        assert _classify_client("") == ""
        assert _classify_client(None) == ""

    def test_client_column_present_in_headers(self):
        from core.audit import HEADERS

        assert "client" in HEADERS
        # Append-only: existing columns kept in order.
        assert HEADERS[:9] == [
            "timestamp_utc", "user", "service", "tool", "params_summary",
            "resource_id", "status", "error", "latency_ms",
        ]

    @pytest.mark.asyncio
    async def test_audit_row_carries_client(self, monkeypatch):
        """The submitted audit entry includes the classified client label."""
        from core import audit

        cap = _Captured()
        monkeypatch.setattr(audit, "logger", lambda: cap)
        monkeypatch.setattr(
            audit, "_resolve_user_email", lambda: _async_return("u@x.com")
        )
        monkeypatch.setattr(audit, "_resolve_client", lambda: "claude-code")

        @audit.audit_log()
        async def t(**_kw):
            return "ok"

        await t()
        assert cap.entries[0]["client"] == "claude-code"


# ---------------------------------------------------------------------------
# Fix 3 — SENSITIVE redaction completeness regression
# ---------------------------------------------------------------------------


class TestSensitiveCompleteness:
    def test_base64_content_redacted(self):
        from core.audit import SENSITIVE, _redact

        assert "base64_content" in SENSITIVE
        blob = "iVBORw0KGgoAAAANSU" + "A" * 5000  # PNG header + filler
        out = _redact({"base64_content": blob, "file_name": "logo.png"})
        assert blob not in out
        assert "<redacted:str:" in out
        assert "logo.png" in out  # neutral fields preserved

    def test_fileurl_redacted(self):
        from core.audit import SENSITIVE, _redact

        assert "fileUrl" in SENSITIVE
        url = "https://internal-share.example.com/very-private-token-12345"
        out = _redact({"fileUrl": url, "file_name": "report.pdf"})
        assert url not in out
        assert "<redacted:str:" in out

    def test_attachments_list_redacted_completely(self):
        from core.audit import SENSITIVE, _redact

        assert "attachments" in SENSITIVE
        payload = "BASE64_BLOB_" + "x" * 5000
        out = _redact({
            "to": "ops@example.com",
            "attachments": [{"filename": "secret.pdf", "content": payload}],
        })
        assert payload not in out
        assert "secret.pdf" not in out  # whole list collapses
        assert "<redacted:list:1>" in out

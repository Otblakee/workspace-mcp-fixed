"""Tests for the audit-logging lifecycle/integrity fixes.

Five behaviour changes covered:

1. Graceful shutdown (``AuditLogger.stop``) drains and writes everything
   still queued, so a Render SIGTERM no longer drops up to 30s of rows.
   On timeout/failure the remaining rows are stdout-dumped as
   AUDIT_FALLBACK — never silently lost.
2. Partial-failure flushes (``_flush`` returns the unwritten entries
   instead of raising) fallback-log ONLY the rows that were not written,
   so successfully appended rows never appear in both the Sheet and the
   stdout dump.
3. ``_drain`` flushes successive batches inside one tick until the queue
   is empty (the old loop wrote at most one BATCH per 30s tick).
4. ``"raw"`` (Gmail RFC822 send format) is in SENSITIVE and deep-redacted.
5. The monkey-patched ``server.tool()`` decorator actually wires audit
   logging: invoking a registered tool lands a row in the audit queue.

Sheets-client mocking follows the patterns in test_audit_integrity.py and
test_multi_user_security.py (MagicMock client per user via
``_build_sheets_for_user``; ``_ensure_tab`` stubbed out).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _appended_tools(rows: list[list]) -> list[str]:
    """Extract the ``tool`` column from raw appended sheet rows."""
    from core.audit import HEADERS

    idx = HEADERS.index("tool")
    return [r[idx] for r in rows]


def _make_recording_build(appended_rows: list, no_creds: set[str] | None = None):
    """Mimic test_multi_user_security's fake ``_build_sheets_for_user``:
    returns a MagicMock Sheets client whose append().execute() records the
    rows it was given; returns None (no creds) for users in ``no_creds``."""
    no_creds = no_creds or set()

    def fake_build(self, email):
        if email in no_creds:
            return None
        sheets = MagicMock()

        def _append(**kw):
            call = MagicMock()

            def _execute():
                appended_rows.extend(kw["body"]["values"])
                return {}

            call.execute = _execute
            return call

        sheets.spreadsheets.return_value.values.return_value.append.side_effect = (
            _append
        )
        sheets.close = MagicMock()
        return sheets

    return fake_build


@pytest.fixture
def audit_logger_factory(monkeypatch):
    """Fresh AuditLogger with audit force-enabled and tab handling stubbed."""
    from core import audit

    monkeypatch.setattr(audit, "ENABLED", True)
    monkeypatch.setattr(audit, "AUDIT_SHEET_ID", "sheet-test")
    monkeypatch.setattr(
        audit.AuditLogger, "_ensure_tab", lambda self, sheets, tab: None
    )

    def factory():
        logger = audit.AuditLogger()
        logger._current_tab = "9999-99"  # skip the ensure-tab path
        return logger

    return factory


def _fallback_dumps(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "AUDIT_FALLBACK" in r.getMessage()]


# ---------------------------------------------------------------------------
# Fix 1 — graceful shutdown drains queued rows
# ---------------------------------------------------------------------------


class TestShutdownDrain:
    @pytest.mark.asyncio
    async def test_stop_writes_all_queued_rows(self, audit_logger_factory, monkeypatch):
        """SIGTERM scenario: rows queued but the 30s tick hasn't fired.
        stop() must cancel the flusher and write everything queued."""
        from core import audit

        appended: list = []
        monkeypatch.setattr(
            audit.AuditLogger,
            "_build_sheets_for_user",
            _make_recording_build(appended),
        )

        logger = audit_logger_factory()
        await logger.start()
        assert logger._task is not None  # flusher running, far from its first tick

        for i in range(3):
            logger.submit({"user": "alice@otb.co.uk", "tool": f"t{i}"})

        await logger.stop()

        assert _appended_tools(appended) == ["t0", "t1", "t2"]
        assert logger.q.empty()
        assert logger._task is None

    @pytest.mark.asyncio
    async def test_stop_lets_in_flight_flush_finish_without_duplicates(
        self, audit_logger_factory, monkeypatch, caplog
    ):
        """stop() must signal the loop and WAIT for an in-flight flush, not
        cancel it: cancelling the awaiter doesn't stop the underlying Sheets
        thread, so a redeploy overlapping a flush could append the rows AND
        fallback-dump them — duplicate audit records. (Codex P2 on PR #19.)"""
        from core import audit

        appended: list = []
        flush_started = asyncio.Event()
        release_flush = asyncio.Event()

        async def slow_flush(self, batch):
            flush_started.set()
            await release_flush.wait()
            appended.extend(batch)
            return []

        monkeypatch.setattr(audit.AuditLogger, "_flush", slow_flush)
        monkeypatch.setattr(audit, "FLUSH_S", 0.01)

        logger = audit_logger_factory()
        await logger.start()
        logger.submit({"user": "alice@otb.co.uk", "tool": "inflight"})
        await asyncio.wait_for(flush_started.wait(), timeout=2)

        with caplog.at_level("ERROR", logger="core.audit"):
            stop_task = asyncio.create_task(logger.stop())
            await asyncio.sleep(0.05)
            # stop() is waiting on the in-flight flush, not cancelling it.
            assert not stop_task.done()
            release_flush.set()
            await asyncio.wait_for(stop_task, timeout=2)

        assert [e["tool"] for e in appended] == ["inflight"]
        assert "AUDIT_FALLBACK" not in caplog.text

    @pytest.mark.asyncio
    async def test_stop_is_idempotent_and_safe_without_start(
        self, audit_logger_factory
    ):
        logger = audit_logger_factory()
        await logger.stop()
        await logger.stop()  # no task, empty queue — must not raise
        assert logger._task is None

    @pytest.mark.asyncio
    async def test_stop_dumps_rows_to_stdout_when_flush_hangs(
        self, audit_logger_factory, monkeypatch, caplog
    ):
        """Bounded shutdown: a wedged flush must not stall the redeploy, and
        the rows must surface as AUDIT_FALLBACK instead of vanishing."""
        from core import audit

        async def hang(self, batch):
            await asyncio.sleep(3600)

        monkeypatch.setattr(audit.AuditLogger, "_flush", hang)
        monkeypatch.setattr(audit, "SHUTDOWN_TIMEOUT_S", 0.05)

        logger = audit_logger_factory()
        for i in range(3):
            logger.submit({"user": "alice@otb.co.uk", "tool": f"t{i}"})

        with caplog.at_level("ERROR", logger="core.audit"):
            await logger.stop()

        dumps = "\n".join(_fallback_dumps(caplog))
        for i in range(3):
            assert f"t{i}" in dumps  # in-flight batch + leftover queue both dumped
        assert logger.q.empty()

    @pytest.mark.asyncio
    async def test_server_shutdown_handler_calls_stop(self, monkeypatch):
        """The Starlette shutdown handler wired in core.server delegates to
        AuditLogger.stop exactly once (idempotent guard)."""
        import core.server as core_server

        stopped = []

        class _FakeLogger:
            async def stop(self):
                stopped.append(True)

        monkeypatch.setattr(core_server, "audit_logger", lambda: _FakeLogger())
        monkeypatch.setattr(core_server, "_audit_started", True)

        await core_server._ensure_audit_stopped()
        await core_server._ensure_audit_stopped()  # second call is a no-op

        assert stopped == [True]


# ---------------------------------------------------------------------------
# Fix 4 — partial-failure fallback logs ONLY unwritten rows
# ---------------------------------------------------------------------------


class TestPartialFailureFallback:
    @pytest.mark.asyncio
    async def test_drain_fallback_logs_only_unwritten_rows(
        self, audit_logger_factory, monkeypatch, caplog
    ):
        """alice's creds resolve, bob's don't: alice's rows reach the Sheet,
        and ONLY bob's rows hit the AUDIT_FALLBACK stdout path. The pre-fix
        code dumped the whole batch, double-logging alice."""
        from core import audit

        appended: list = []
        monkeypatch.setattr(
            audit.AuditLogger,
            "_build_sheets_for_user",
            _make_recording_build(appended, no_creds={"bob@otb.co.uk"}),
        )

        logger = audit_logger_factory()
        logger.submit({"user": "alice@otb.co.uk", "tool": "alice_tool"})
        logger.submit({"user": "bob@otb.co.uk", "tool": "bob_tool"})

        with caplog.at_level("ERROR", logger="core.audit"):
            await logger._drain()

        assert _appended_tools(appended) == ["alice_tool"]

        dumps = "\n".join(_fallback_dumps(caplog))
        assert "bob_tool" in dumps
        assert "alice_tool" not in dumps  # written rows never double-logged

    @pytest.mark.asyncio
    async def test_flush_exception_dumps_whole_batch_and_stops_drain(
        self, audit_logger_factory, monkeypatch, caplog
    ):
        """A hard _flush failure (not partial) still dumps the in-flight
        batch and stops draining; later rows stay queued for the next tick."""
        from core import audit

        monkeypatch.setattr(audit, "BATCH", 2)

        async def explode(self, batch):
            raise RuntimeError("sheets exploded")

        monkeypatch.setattr(audit.AuditLogger, "_flush", explode)

        logger = audit_logger_factory()
        for i in range(3):
            logger.submit({"user": "alice@otb.co.uk", "tool": f"t{i}"})

        with caplog.at_level("ERROR", logger="core.audit"):
            await logger._drain()

        dumps = "\n".join(_fallback_dumps(caplog))
        assert "t0" in dumps and "t1" in dumps
        assert "t2" not in dumps
        assert logger.q.qsize() == 1  # t2 retained for the next tick


# ---------------------------------------------------------------------------
# Fix 3 — multi-batch drain in one tick
# ---------------------------------------------------------------------------


class TestMultiBatchDrain:
    @pytest.mark.asyncio
    async def test_drain_empties_queue_across_multiple_batches(
        self, audit_logger_factory, monkeypatch
    ):
        """With BATCH=2 and 5 queued rows, one drain call must perform three
        successive flushes ([2, 2, 1]) and leave the queue empty. The old
        loop flushed a single batch per tick, capping throughput at
        ~100 rows/min and backlogging the queue into AUDIT_DROP territory."""
        from core import audit

        monkeypatch.setattr(audit, "BATCH", 2)

        flushed_batches: list[list] = []

        async def record_flush(self, batch):
            flushed_batches.append(list(batch))
            return []

        monkeypatch.setattr(audit.AuditLogger, "_flush", record_flush)

        logger = audit_logger_factory()
        for i in range(5):
            logger.submit({"user": "alice@otb.co.uk", "tool": f"t{i}"})

        await logger._drain()

        assert [len(b) for b in flushed_batches] == [2, 2, 1]
        assert [e["tool"] for b in flushed_batches for e in b] == [
            "t0", "t1", "t2", "t3", "t4",
        ]
        assert logger.q.empty()

    @pytest.mark.asyncio
    async def test_drain_noop_on_empty_queue(self, audit_logger_factory, monkeypatch):
        from core import audit

        async def fail_if_called(self, batch):  # pragma: no cover - guard
            raise AssertionError("flush must not run with an empty queue")

        monkeypatch.setattr(audit.AuditLogger, "_flush", fail_if_called)
        logger = audit_logger_factory()
        await logger._drain()


# ---------------------------------------------------------------------------
# "raw" redaction (Gmail RFC822 send format)
# ---------------------------------------------------------------------------


class TestRawRedaction:
    def test_raw_in_sensitive(self):
        from core.audit import SENSITIVE

        assert "raw" in SENSITIVE

    def test_raw_redacted_top_level(self):
        from core.audit import _redact

        rfc822 = (
            "From: oliver@otbgroup.co.uk\r\nTo: client@example.com\r\n"
            "Subject: Confidential offer\r\n\r\nSecret body text"
        )
        out = _redact({"raw": rfc822, "user_google_email": "oliver@otbgroup.co.uk"})
        assert "Confidential offer" not in out
        assert "client@example.com" not in out
        assert "Secret body text" not in out
        assert "<redacted:str:" in out
        assert "oliver@otbgroup.co.uk" in out  # neutral fields preserved

    def test_raw_redacted_nested(self):
        """Gmail kwargs shape: message={"raw": <b64url rfc822>}. The string
        truncation that used to be the only protection leaked the first
        ~150 header bytes."""
        from core.audit import _redact

        payload = "RnJvbTogb2xpdmVy" + "A" * 5000
        out = _redact({"message": {"raw": payload, "threadId": "t-1"}})
        assert payload not in out
        assert payload[:150] not in out
        assert "<redacted:str:" in out
        assert "t-1" in out


# ---------------------------------------------------------------------------
# server.tool() monkey-patch wiring
# ---------------------------------------------------------------------------


class TestServerToolAuditWiring:
    @pytest.mark.asyncio
    async def test_tool_registered_via_patched_decorator_lands_audit_row(
        self, monkeypatch
    ):
        """Integration-style guard on the monkey-patch in core/server.py:
        a tool registered through ``server.tool()`` must, when invoked, put
        a row on the audit queue. Nothing else exercised this wiring — a
        refactor of the patch would have silently disabled all auditing."""
        import core.server as core_server
        from core import audit

        # Force-enable audit and give it a fresh queue we can inspect.
        monkeypatch.setattr(audit, "ENABLED", True)
        fresh = audit.AuditLogger()
        monkeypatch.setattr(audit, "_inst", fresh)
        # Skip the lazy flusher start — we only care about the submit path.
        monkeypatch.setattr(core_server, "_audit_started", True)

        @core_server.server.tool()
        async def _audit_wiring_probe(document_id: str = "") -> str:
            """Test-only probe tool."""
            return "probe-ok"

        try:
            # Invoke through the server's registry, the same way CLI mode
            # resolves tools — proves registration AND audit wrapping.
            from core.tool_registry import get_tool_components

            tools = get_tool_components(core_server.server)
            assert "_audit_wiring_probe" in tools
            fn = tools["_audit_wiring_probe"].fn
            result = await fn(document_id="doc-123")
            assert result == "probe-ok"

            assert fresh.q.qsize() == 1
            row = fresh.q.get_nowait()
            assert row["tool"] == "_audit_wiring_probe"
            assert row["status"] == "success"
            assert row["resource_id"] == "doc-123"
            assert row["user"]  # attributed (DEFAULT_USER without context)
            assert set(audit.HEADERS) <= set(row.keys())
        finally:
            try:
                core_server.server.local_provider.remove_tool("_audit_wiring_probe")
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_patched_decorator_starts_audit_lazily(self, monkeypatch):
        """Invoking a registered tool triggers _ensure_audit_started."""
        import core.server as core_server

        started = []

        async def fake_start():
            started.append(True)

        monkeypatch.setattr(core_server, "_ensure_audit_started", fake_start)

        @core_server.server.tool()
        async def _audit_lazy_probe() -> str:
            """Test-only probe tool."""
            return "ok"

        try:
            from core.tool_registry import get_tool_components

            tools = get_tool_components(core_server.server)
            await tools["_audit_lazy_probe"].fn()
            assert started == [True]
        finally:
            try:
                core_server.server.local_provider.remove_tool("_audit_lazy_probe")
            except Exception:
                pass

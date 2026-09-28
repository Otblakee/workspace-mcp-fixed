"""The HTTP app that ``server.run(transport="streamable-http")`` boots.

Pins the FastMCP 4 / Starlette 1 contract for ``SecureFastMCP.http_app``:

1. Building the app must not touch the Starlette startup/shutdown event
   API. Starlette 1.x removed ``add_event_handler``; the 1.15.x server
   raised ``AttributeError`` at boot on FastMCP 4 because of it. On
   Starlette 0.x the handlers were silently ignored (FastMCP always
   installs its own lifespan), so the audit flusher's documented
   start-on-boot and drain-on-shutdown never actually ran either.
2. The audit flusher is started when the ASGI lifespan is entered and
   stopped when it exits, around FastMCP's own lifespan.
3. The custom routes (``/``, ``/health``, ``/attachments/{file_id}``)
   are served by the built app.
4. Every request to ``/attachments/{file_id}`` queues one audit row (tool
   ``attachments_download``, service ``attachments``) through the audit
   logger, hit or miss, and an audit failure never breaks the download.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fake_audit(monkeypatch):
    """Replace the audit logger with a recorder and reset the start flag."""
    import core.server as core_server

    events: list[str] = []

    class _FakeLogger:
        async def start(self):
            events.append("start")

        async def stop(self):
            events.append("stop")

        def submit(self, entry):
            events.append("submit")

    monkeypatch.setattr(core_server, "audit_logger", lambda: _FakeLogger())
    monkeypatch.setattr(core_server, "_audit_started", False)
    return events


@pytest.fixture
def audit_rows(monkeypatch):
    """Replace the audit logger with one that records submitted rows."""
    import core.server as core_server

    rows: list[dict] = []

    class _Recorder:
        async def start(self):
            return None

        async def stop(self):
            return None

        def submit(self, entry):
            rows.append(dict(entry))

    monkeypatch.setattr(core_server, "audit_logger", lambda: _Recorder())
    monkeypatch.setattr(core_server, "_audit_started", False)
    return rows


@pytest.fixture
def attachment_dir(tmp_path, monkeypatch):
    """An isolated attachment store so the tests can register a real file."""
    import core.attachment_storage as storage_mod

    monkeypatch.setenv("WORKSPACE_ATTACHMENT_DIR", str(tmp_path))
    monkeypatch.setattr(storage_mod, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(storage_mod, "_attachment_storage", None)
    yield tmp_path
    monkeypatch.setattr(storage_mod, "_attachment_storage", None)


def _register_attachment(attachment_dir, content: bytes = b"hello") -> str:
    from core.attachment_storage import get_attachment_storage

    storage = get_attachment_storage()
    file_id, path = storage.reserve_path("note.txt")
    Path(path).write_bytes(content)
    storage.register_existing_file(
        file_id, str(path), filename="note.txt", mime_type="text/plain"
    )
    return file_id


def _build_app():
    import core.server as core_server

    return core_server.server.http_app(transport="streamable-http")


class TestHttpAppLifespan:
    def test_http_app_builds_without_starlette_event_handlers(self, fake_audit):
        app = _build_app()
        assert app is not None
        # Nothing has started yet: building the app is not entering the lifespan.
        assert fake_audit == []

    def test_lifespan_starts_and_stops_audit_around_fastmcp_lifespan(self, fake_audit):
        from starlette.testclient import TestClient

        import core.server as core_server

        app = _build_app()
        with TestClient(app):
            assert core_server._audit_started is True
            assert fake_audit == ["start"]
        assert core_server._audit_started is False
        assert fake_audit == ["start", "stop"]

    def test_lifespan_property_exposes_wrapped_lifespan(self, fake_audit):
        """FastMCP's ``app.lifespan`` (used when mounting into a parent ASGI
        app) must be the wrapped lifespan, not FastMCP's bare one."""
        app = _build_app()
        assert app.lifespan is app.router.lifespan_context
        assert app.lifespan.__name__ == "lifespan_with_audit"

    def test_audit_start_failure_does_not_block_boot(self, monkeypatch):
        import core.server as core_server
        from starlette.testclient import TestClient

        class _BrokenLogger:
            async def start(self):
                raise RuntimeError("malformed audit config")

            async def stop(self):
                return None

        monkeypatch.setattr(core_server, "audit_logger", lambda: _BrokenLogger())
        monkeypatch.setattr(core_server, "_audit_started", False)

        app = _build_app()
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200

    def test_custom_routes_are_served(self, fake_audit):
        from starlette.testclient import TestClient

        app = _build_app()
        with TestClient(app) as client:
            for path in ("/", "/health"):
                response = client.get(path)
                assert response.status_code == 200, path
                body = response.json()
                assert body["status"] == "healthy"
                assert body["service"] == "workspace-mcp"
                assert body["version"]
            missing = client.get("/attachments/does-not-exist")
            assert missing.status_code == 404
            assert missing.json()["error"] == "Attachment not found or expired"


class TestNoStarletteEventApi:
    """Source-level pin: the event API is gone in Starlette 1.x, and a
    reintroduction would boot-loop the container again."""

    @pytest.mark.parametrize(
        "relative_path",
        ["core/server.py", "main.py", "fastmcp_server.py"],
    )
    def test_no_add_event_handler_or_on_startup(self, relative_path):
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert not re.search(r"\badd_event_handler\s*\(", source), relative_path
        assert not re.search(r"\bon_startup\s*=", source), relative_path
        assert not re.search(r"\bon_shutdown\s*=", source), relative_path


class TestAttachmentRouteAudit:
    """Tier 3 item 10: the capability URL is logged, hit or miss."""

    def test_miss_queues_an_error_row_with_the_user_agent_only(
        self, audit_rows, attachment_dir
    ):
        from starlette.testclient import TestClient

        from core.audit import DEFAULT_USER, HEADERS

        app = _build_app()
        with TestClient(app) as client:
            response = client.get(
                "/attachments/does-not-exist",
                headers={"User-Agent": "curl/8.0 " + "x" * 200},
            )
        assert response.status_code == 404
        assert len(audit_rows) == 1
        row = audit_rows[0]
        assert set(row) == set(HEADERS)
        assert row["tool"] == "attachments_download"
        assert row["service"] == "attachments"
        assert row["status"] == "error"
        assert row["error"].startswith("404")
        assert row["resource_id"] == "does-not-exist"
        # No MCP context on a plain GET: the usual fallback names the row.
        assert row["user"] == DEFAULT_USER
        summary = json.loads(row["params_summary"])
        assert set(summary) == {"user_agent"}
        assert summary["user_agent"] == ("curl/8.0 " + "x" * 200)[:100]
        assert len(summary["user_agent"]) == 100
        assert isinstance(row["latency_ms"], int)
        assert row["timestamp_utc"]

    def test_hit_serves_the_file_and_queues_a_success_row(
        self, audit_rows, attachment_dir
    ):
        from starlette.testclient import TestClient

        file_id = _register_attachment(attachment_dir, b"hello there")
        app = _build_app()
        with TestClient(app) as client:
            response = client.get(f"/attachments/{file_id}")
        assert response.status_code == 200
        assert response.content == b"hello there"
        assert response.headers["cache-control"] == "no-store"
        assert len(audit_rows) == 1
        row = audit_rows[0]
        assert row["tool"] == "attachments_download"
        assert row["status"] == "success"
        assert row["error"] == ""
        assert row["resource_id"] == file_id
        assert json.loads(row["params_summary"]) == {"user_agent": "testclient"}

    def test_expired_attachment_is_an_error_row(self, audit_rows, attachment_dir):
        from starlette.testclient import TestClient

        from core.attachment_storage import get_attachment_storage

        file_id = _register_attachment(attachment_dir)
        storage = get_attachment_storage()
        storage._metadata[file_id]["expires_at"] = datetime.now() - timedelta(seconds=1)
        app = _build_app()
        with TestClient(app) as client:
            response = client.get(f"/attachments/{file_id}")
        assert response.status_code == 404
        assert audit_rows[-1]["status"] == "error"
        assert audit_rows[-1]["resource_id"] == file_id

    def test_audit_failure_never_breaks_the_download(
        self, attachment_dir, monkeypatch, caplog
    ):
        import core.server as core_server
        from starlette.testclient import TestClient

        class _Broken:
            async def start(self):
                return None

            async def stop(self):
                return None

            def submit(self, entry):
                raise RuntimeError("queue exploded")

        monkeypatch.setattr(core_server, "audit_logger", lambda: _Broken())
        monkeypatch.setattr(core_server, "_audit_started", False)
        file_id = _register_attachment(attachment_dir, b"still served")
        app = _build_app()
        with caplog.at_level(logging.ERROR, logger="core.server"):
            with TestClient(app) as client:
                assert client.get(f"/attachments/{file_id}").content == b"still served"
                assert client.get("/attachments/nope").status_code == 404
        assert "Attachment audit submit failed" in caplog.text

    def test_route_source_audits_every_return(self):
        """Every return in ``serve_attachment`` is preceded by an audit
        submit, so a new branch cannot slip out unlogged."""
        source = (REPO_ROOT / "core/server.py").read_text(encoding="utf-8")
        start = source.index("async def serve_attachment(")
        end = source.index("async def legacy_oauth2_callback(")
        body = source[start:end]
        returns = body.count("return ")
        assert returns == 3
        assert body.count("await _submit_attachment_audit(") == returns

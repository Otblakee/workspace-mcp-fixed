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
"""

from __future__ import annotations

import re
import sys
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

    monkeypatch.setattr(core_server, "audit_logger", lambda: _FakeLogger())
    monkeypatch.setattr(core_server, "_audit_started", False)
    return events


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

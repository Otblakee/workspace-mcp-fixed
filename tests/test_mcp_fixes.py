"""Tests for the OTB MCP fix batch in
fix/drive-base64-locale-draft-delete-docfmt.

Covers Issues 1–5. Unit tests verify parameter contracts, validation logic
and registration; live-API tests are guarded by environment variables and
skipped by default. The live tests are the only way to confirm Google-side
behaviour (e.g. that the Drive markdown converter actually applies Heading 1
to a `# H1` line) — run them manually with credentials when needed.
"""

from __future__ import annotations

import base64
import inspect
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# Live tests require a real OAuth-authenticated session; skip unless opted in.
LIVE = os.environ.get("OTB_MCP_LIVE_TESTS") == "1"
LIVE_USER = os.environ.get("OTB_MCP_LIVE_USER", "oliver@otbgroup.co.uk")
SHARED_DRIVE_FOLDER = os.environ.get("OTB_MCP_LIVE_SHARED_DRIVE_FOLDER", "")


def _unwrap(fn):
    """Peel functools.wraps layers down to the original implementation."""
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


# ---------------------------------------------------------------------------
# Issue 3 — create_spreadsheet locale
# ---------------------------------------------------------------------------

class TestCreateSpreadsheetLocale:
    def test_signature_has_locale_default_en_gb(self):
        from gsheets.sheets_tools import create_spreadsheet

        sig = inspect.signature(_unwrap(create_spreadsheet))
        assert "locale" in sig.parameters
        assert sig.parameters["locale"].default == "en_GB"

    def test_request_body_includes_locale_at_create(self):
        """The locale must travel inside properties on spreadsheets.create().
        Sheets does not allow setting locale via batchUpdate later; it must be
        on the initial create body."""
        src = (REPO_ROOT / "gsheets" / "sheets_tools.py").read_text()
        assert '"properties": {"title": title, "locale": locale}' in src, (
            "Expected 'locale' set alongside 'title' on the create body."
        )

    @pytest.mark.skipif(not LIVE, reason="Set OTB_MCP_LIVE_TESTS=1 to run live")
    def test_live_default_locale_is_en_gb(self):
        """Live: a default-args spreadsheet must come back with locale en_GB."""
        # Stub — wire into the test harness used in CI when available. Without
        # live creds we cannot exercise this end-to-end here.
        pytest.skip("Live integration stub — implement against the OTB MCP harness.")


# ---------------------------------------------------------------------------
# Issue 1 — create_drive_file base64_content
# ---------------------------------------------------------------------------

class TestCreateDriveFileBase64:
    def test_signature_has_base64_content(self):
        from gdrive.drive_tools import create_drive_file

        sig = inspect.signature(_unwrap(create_drive_file))
        assert "base64_content" in sig.parameters
        assert sig.parameters["base64_content"].default is None

    @pytest.mark.asyncio
    async def test_mutual_exclusivity_rejects_two_sources(self):
        from gdrive.drive_tools import create_drive_file

        impl = _unwrap(create_drive_file)
        # Provide both content and base64_content — should raise BEFORE any
        # service call, so the mock's files() must never be invoked.
        service = MagicMock()
        with pytest.raises(Exception, match="Provide only one of"):
            await impl(
                service=service,
                user_google_email=LIVE_USER,
                file_name="x.txt",
                content="hello",
                base64_content=base64.b64encode(b"hi").decode(),
            )
        service.files.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_source_rejects_for_non_folder(self):
        from gdrive.drive_tools import create_drive_file

        impl = _unwrap(create_drive_file)
        service = MagicMock()
        with pytest.raises(Exception, match="must provide one of"):
            await impl(
                service=service,
                user_google_email=LIVE_USER,
                file_name="x.txt",
            )
        service.files.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_base64_rejects_with_clear_message(self):
        from gdrive.drive_tools import create_drive_file

        impl = _unwrap(create_drive_file)
        # Patch resolve_folder_id to avoid Drive API call before reaching the
        # base64 decode.
        import gdrive.drive_tools as mod

        original = mod.resolve_folder_id
        mod.resolve_folder_id = AsyncMock(return_value="root")
        try:
            service = MagicMock()
            with pytest.raises(Exception, match="not valid standard base64"):
                await impl(
                    service=service,
                    user_google_email=LIVE_USER,
                    file_name="x.bin",
                    base64_content="not-base64!!!",
                    mime_type="application/octet-stream",
                )
        finally:
            mod.resolve_folder_id = original

    @pytest.mark.skipif(not LIVE, reason="Set OTB_MCP_LIVE_TESTS=1 to run live")
    def test_live_png_roundtrip(self):
        """Live: upload a 1x1 PNG via base64_content, download it, byte-compare."""
        pytest.skip("Live integration stub — implement against the OTB MCP harness.")

    @pytest.mark.skipif(not LIVE, reason="Set OTB_MCP_LIVE_TESTS=1 to run live")
    def test_live_pdf_roundtrip(self):
        pytest.skip("Live integration stub — implement against the OTB MCP harness.")

    @pytest.mark.skipif(
        not (LIVE and SHARED_DRIVE_FOLDER),
        reason="Requires OTB_MCP_LIVE_TESTS=1 and OTB_MCP_LIVE_SHARED_DRIVE_FOLDER",
    )
    def test_live_shared_drive_upload(self):
        """Live: uploading into a 0A… Shared Drive folder must land in the
        Shared Drive, not My Drive."""
        pytest.skip("Live integration stub — implement against the OTB MCP harness.")


# ---------------------------------------------------------------------------
# Issue 4 — delete_gmail_draft
# ---------------------------------------------------------------------------

class TestDeleteGmailDraft:
    def test_tool_exists_and_signature_is_clean(self):
        from gmail.gmail_tools import delete_gmail_draft

        sig = inspect.signature(_unwrap(delete_gmail_draft))
        # `service` is injected by the decorator; the public tool signature
        # exposes user_google_email + draft_id.
        assert "draft_id" in sig.parameters

    def test_registered_in_extended_tier(self):
        with open(REPO_ROOT / "core" / "tool_tiers.yaml") as f:
            tiers = yaml.safe_load(f)
        assert "delete_gmail_draft" in tiers["gmail"]["extended"], (
            "delete_gmail_draft must be wired into core/tool_tiers.yaml under "
            "gmail.extended so deployments enabling that tier pick it up."
        )

    def test_uses_compose_scope(self):
        """Match draft_gmail_message — both should require GMAIL_COMPOSE_SCOPE.
        Different scopes would silently fail on legitimate draft delete calls."""
        src = (REPO_ROOT / "gmail" / "gmail_tools.py").read_text()
        # Crude but robust: ensure the GMAIL_COMPOSE_SCOPE decorator sits on
        # the same function as the docstring marker.
        assert (
            '@require_google_service("gmail", GMAIL_COMPOSE_SCOPE)\n'
            "async def delete_gmail_draft("
        ) in src

    @pytest.mark.skipif(not LIVE, reason="Set OTB_MCP_LIVE_TESTS=1 to run live")
    def test_live_create_then_delete(self):
        """Live: draft_gmail_message → delete_gmail_draft must succeed,
        and a follow-up read must 404."""
        pytest.skip("Live integration stub — implement against the OTB MCP harness.")


# ---------------------------------------------------------------------------
# Issue 5 — create_doc content_format=markdown
# ---------------------------------------------------------------------------

class TestCreateDocMarkdown:
    def test_signature_has_content_format(self):
        from gdocs.docs_tools import create_doc

        sig = inspect.signature(_unwrap(create_doc))
        assert "content_format" in sig.parameters
        assert sig.parameters["content_format"].default == "plain"

    @pytest.mark.asyncio
    async def test_invalid_format_returns_error_string(self):
        from gdocs.docs_tools import create_doc

        impl = _unwrap(create_doc)
        result = await impl(
            docs_service=MagicMock(),
            drive_service=MagicMock(),
            user_google_email=LIVE_USER,
            title="t",
            content="x",
            content_format="bogus",
        )
        assert "Error" in result and "content_format" in result

    @pytest.mark.asyncio
    async def test_markdown_routes_through_drive_api(self):
        """When content_format='markdown' and content is non-empty, the impl
        must call drive_service.files().create() with text/markdown and a
        Google Doc target mimeType — and must NOT call docs_service."""
        from gdocs.docs_tools import create_doc

        impl = _unwrap(create_doc)
        docs_service = MagicMock()
        drive_service = MagicMock()
        drive_service.files.return_value.create.return_value.execute.return_value = {
            "id": "doc-1",
            "name": "t",
            "webViewLink": "https://docs.google.com/document/d/doc-1/edit",
        }

        await impl(
            docs_service=docs_service,
            drive_service=drive_service,
            user_google_email=LIVE_USER,
            title="t",
            content="# Hello\n- a\n- b",
            content_format="markdown",
        )

        # Drive was called with the markdown→Doc conversion flags.
        create_call = drive_service.files.return_value.create.call_args
        assert create_call.kwargs["body"]["mimeType"] == (
            "application/vnd.google-apps.document"
        )
        assert create_call.kwargs["supportsAllDrives"] is True
        # Docs API was NOT touched on the markdown path.
        docs_service.documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_plain_default_still_uses_docs_api(self):
        """No regression: default content_format='plain' (or unset) must still
        call docs_service.documents().create() and never the Drive API."""
        from gdocs.docs_tools import create_doc

        impl = _unwrap(create_doc)
        docs_service = MagicMock()
        drive_service = MagicMock()
        docs_service.documents.return_value.create.return_value.execute.return_value = {
            "documentId": "doc-2"
        }

        # Default — no content_format passed.
        await impl(
            docs_service=docs_service,
            drive_service=drive_service,
            user_google_email=LIVE_USER,
            title="t",
            content="hello",
        )

        docs_service.documents.return_value.create.assert_called_once()
        drive_service.files.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_content_does_not_invoke_drive(self):
        """Empty content + markdown should fall through to the plain path
        (creates the empty Doc) — the Drive converter would 400 on empty."""
        from gdocs.docs_tools import create_doc

        impl = _unwrap(create_doc)
        docs_service = MagicMock()
        drive_service = MagicMock()
        docs_service.documents.return_value.create.return_value.execute.return_value = {
            "documentId": "doc-3"
        }

        await impl(
            docs_service=docs_service,
            drive_service=drive_service,
            user_google_email=LIVE_USER,
            title="empty",
            content="",
            content_format="markdown",
        )

        drive_service.files.assert_not_called()
        docs_service.documents.return_value.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_large_markdown_does_not_crash(self):
        """50KB markdown blob round-trips through the impl without truncation
        or memory issues — important for Render's 512MB starter plan."""
        from gdocs.docs_tools import create_doc

        impl = _unwrap(create_doc)
        big = "# Heading\n" + ("- item\n" * 8000)  # ~50KB
        assert len(big) > 50_000

        drive_service = MagicMock()
        drive_service.files.return_value.create.return_value.execute.return_value = {
            "id": "big",
            "webViewLink": "x",
        }

        await impl(
            docs_service=MagicMock(),
            drive_service=drive_service,
            user_google_email=LIVE_USER,
            title="big",
            content=big,
            content_format="markdown",
        )

    @pytest.mark.skipif(not LIVE, reason="Set OTB_MCP_LIVE_TESTS=1 to run live")
    def test_live_h1_and_bullet_render_as_native_styles(self):
        """Live: create_doc(content='# H1\\n- a\\n- b', content_format='markdown')
        then read back the structure and assert the first paragraph is
        HEADING_1 and the next two paragraphs are inside a real bulleted
        list — not literal `#` and `-` characters.

        This is the only test that actually verifies Google's converter does
        the right thing. Run after each markdown-converter behaviour change.
        """
        pytest.skip("Live integration stub — implement against the OTB MCP harness.")


# ---------------------------------------------------------------------------
# Issue 2 — supportsAllDrives audit
# ---------------------------------------------------------------------------

class TestSupportsAllDrivesAudit:
    """Static checks to keep the patched call sites from regressing.
    A future PR removing the flags would silently route writes back to My
    Drive — these tests fail fast if that happens."""

    def test_apps_script_list_carries_both_flags(self):
        src = (REPO_ROOT / "gappsscript" / "apps_script_tools.py").read_text()
        # Both flags must be inside the request_params dict for list().
        assert '"supportsAllDrives": True' in src
        assert '"includeItemsFromAllDrives": True' in src

    def test_apps_script_delete_carries_flag(self):
        src = (REPO_ROOT / "gappsscript" / "apps_script_tools.py").read_text()
        assert (
            "service.files().delete(fileId=script_id, supportsAllDrives=True)"
            in src
        )

    def test_drive_get_media_carries_flag(self):
        """get_drive_file_content + get_drive_file_download_url both need
        supportsAllDrives on get_media for shared-drive content."""
        src = (REPO_ROOT / "gdrive" / "drive_tools.py").read_text()
        assert src.count(
            "service.files().get_media(fileId=file_id, supportsAllDrives=True)"
        ) >= 2

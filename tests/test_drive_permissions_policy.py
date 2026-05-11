"""Tests for OTB Drive-permission policy messaging.

OTB Drive sharing policy is groups-only. The Drive-permissions tools
(``get_drive_file_permissions``, ``check_drive_file_public_access``)
previously suggested enabling ``Anyone with the link`` as the remediation
when a file lacked public access — that hint is the only path that
satisfies Google's ``insert_doc_image_url`` API, but advising it would
push users to breach OTB governance.

This suite locks in the replacement messaging: the response surfaces the
policy stance and points to compliant alternatives (group-share + link,
or direct image upload via the Docs UI) instead of the previous
public-link instruction.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_no_public_access_policy_note_is_groups_only():
    from gdrive.drive_helpers import NO_PUBLIC_ACCESS_POLICY_NOTE

    # Must explicitly reject the previous hint.
    assert "do NOT enable 'Anyone with the link'" in NO_PUBLIC_ACCESS_POLICY_NOTE
    # Must reference at least one policy-compliant alternative path.
    # Either group-share + link, or direct upload via the Docs UI.
    assert "group" in NO_PUBLIC_ACCESS_POLICY_NOTE
    assert (
        "Insert → Image → Upload" in NO_PUBLIC_ACCESS_POLICY_NOTE
        or "upload it directly" in NO_PUBLIC_ACCESS_POLICY_NOTE
    )


def test_get_drive_file_permissions_uses_policy_note():
    """The non-public branch of get_drive_file_permissions must render
    the policy note instead of the old "Right-click → Share → Anyone with
    the link" hint."""
    src = (
        Path(__file__).resolve().parent.parent / "gdrive" / "drive_tools.py"
    ).read_text()
    assert (
        "To fix: Right-click the file in Google Drive → Share → Anyone with the link"
        not in src
    )
    assert "NO_PUBLIC_ACCESS_POLICY_NOTE" in src


def test_check_drive_file_public_access_uses_policy_note():
    """Same fix applies to the standalone check_drive_file_public_access
    tool's no-access branch."""
    src = (
        Path(__file__).resolve().parent.parent / "gdrive" / "drive_tools.py"
    ).read_text()
    assert "Fix: Drive → Share → 'Anyone with the link' → 'Viewer'" not in src


def test_format_public_sharing_error_helper_also_updated():
    """The dead-but-exported ``format_public_sharing_error`` helper is
    updated for consistency in case a future caller revives it."""
    from gdrive.drive_helpers import format_public_sharing_error

    msg = format_public_sharing_error("test.png", "abc123")
    # The "Set 'Anyone with the link' → 'Viewer'" string from the previous
    # helper would breach policy if it ever got re-wired into a tool.
    assert "Set 'Anyone with the link' → 'Viewer'" not in msg
    assert "do NOT enable 'Anyone with the link'" in msg

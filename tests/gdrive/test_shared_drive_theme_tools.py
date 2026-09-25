"""Unit tests for the shared drive banner (theme) tools.

Covers:

* exactly one of theme_id / image_file_id, and crop only with an image;
* the Manager check (``canChangeDriveBackground``) and the domain-admin
  fallback, in explicit and automatic modes;
* crop defaults (largest centred 80:9 box) and Google's 1280x144 minimum;
* before/after reporting re-read from ``drives.get``;
* dry runs never call ``drives.update``;
* the registry bulk helper: drive-level row selection, category precedence,
  per-drive failure isolation and the up-front image check.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from googleapiclient.errors import HttpError  # noqa: E402

from core.utils import UserInputError  # noqa: E402
from gdrive import shared_drive_theme_tools as theme_tools  # noqa: E402


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


get_shared_drive_theme = _unwrap(theme_tools.get_shared_drive_theme)
set_shared_drive_theme = _unwrap(theme_tools.set_shared_drive_theme)
set_themes_from_registry = _unwrap(theme_tools.set_shared_drive_themes_from_registry)

USER = "oliver@otbgroup.co.uk"

ABACUS_LINK = "https://ssl.gstatic.com/team_drive_themes/abacus_background.jpg"
BOK_LINK = "https://ssl.gstatic.com/team_drive_themes/bok_choy_background.jpg"


def _http_error(status: int, reason: str = "x") -> HttpError:
    resp = MagicMock()
    resp.status = status
    body = f'{{"error": {{"errors": [{{"reason": "{reason}"}}]}}}}'.encode()
    return HttpError(resp, body)


def _request(result):
    request = MagicMock()
    if isinstance(result, Exception):
        request.execute.side_effect = result
    else:
        request.execute.return_value = result
    return request


class FakeDrive:
    """Drive double. Drives are keyed by ID; member and admin views differ."""

    def __init__(self):
        self.calls = []
        # drive_id -> resource as seen by the caller as a member (None = 404)
        self.member_view = {}
        # drive_id -> resource as seen with useDomainAdminAccess, or an
        # HttpError to raise (e.g. 403 for a non-admin).
        self.admin_view = {}
        # Applied on drives.update so the re-read reflects the change.
        self.after_update = {}
        self.update_error = {}
        self.images = {}
        self.themes = [
            {"id": "abacus", "backgroundImageLink": ABACUS_LINK, "colorRgb": "#1a73e8"},
            {"id": "bok", "backgroundImageLink": BOK_LINK, "colorRgb": "#689f38"},
        ]
        self.about_error = None
        # Real Drive applies a colorRgb sent on drives.update; set this to
        # simulate the colour not sticking.
        self.ignore_color = False

    def drives(self):
        parent = self

        class _Drives:
            def get(self, **kwargs):
                parent.calls.append(("drives.get", kwargs))
                drive_id = kwargs["driveId"]
                view = (
                    parent.admin_view
                    if kwargs.get("useDomainAdminAccess")
                    else parent.member_view
                )
                result = view.get(drive_id)
                if isinstance(result, Exception):
                    return _request(result)
                if result is None:
                    return _request(_http_error(404, "notFound"))
                return _request(dict(result))

            def update(self, **kwargs):
                parent.calls.append(("drives.update", kwargs))
                drive_id = kwargs["driveId"]
                if drive_id in parent.update_error:
                    return _request(parent.update_error[drive_id])
                changes = dict(parent.after_update.get(drive_id, {}))
                body_colour = (kwargs.get("body") or {}).get("colorRgb")
                if body_colour and not parent.ignore_color:
                    changes["colorRgb"] = body_colour
                for view in (parent.member_view, parent.admin_view):
                    if isinstance(view.get(drive_id), dict):
                        view[drive_id].update(changes)
                return _request({"id": drive_id, **changes})

        return _Drives()

    def files(self):
        parent = self

        class _Files:
            def get(self, **kwargs):
                parent.calls.append(("files.get", kwargs))
                meta = parent.images.get(kwargs["fileId"])
                if meta is None:
                    return _request(_http_error(404, "notFound"))
                return _request(meta)

        return _Files()

    def about(self):
        parent = self

        class _About:
            def get(self, **kwargs):
                parent.calls.append(("about.get", kwargs))
                if parent.about_error is not None:
                    return _request(parent.about_error)
                return _request({"driveThemes": parent.themes})

        return _About()

    def call_names(self):
        return [name for name, _ in self.calls]

    def kwargs_for(self, name):
        return [kw for n, kw in self.calls if n == name]


def _drive(drive_id="d1", name="JIT-Operations", can_change=True, **extra):
    # No themeId: it is write-only in the Drive API and never comes back on a
    # read. The drive shows the abacus stock theme's image and colour instead.
    return {
        "id": drive_id,
        "name": name,
        "colorRgb": "#1a73e8",
        "backgroundImageLink": ABACUS_LINK,
        "capabilities": {"canChangeDriveBackground": can_change},
        **extra,
    }


PNG = {
    "id": "img1",
    "name": "JIT banner.png",
    "mimeType": "image/png",
    "trashed": False,
    "imageMediaMetadata": {"width": 1920, "height": 1080},
}


# ---------------------------------------------------------------------------
# compute_banner_crop
# ---------------------------------------------------------------------------


class TestComputeBannerCrop:
    def test_tall_image_uses_full_width_centred_vertically(self):
        crop, notes = theme_tools.compute_banner_crop(1920, 1080)
        assert crop["width"] == 1.0
        assert crop["xCoordinate"] == 0.0
        # 1920 * 9/80 = 216px tall band, centred in 1080px.
        assert crop["yCoordinate"] == pytest.approx((1 - 216 / 1080) / 2, abs=1e-6)
        assert notes == []

    def test_wide_image_uses_full_height_centred_horizontally(self):
        # 4000x300: an 80:9 box at full height is 2666.67px wide.
        crop, _ = theme_tools.compute_banner_crop(4000, 300)
        assert crop["yCoordinate"] == 0.0
        assert crop["width"] == pytest.approx(2666.6667 / 4000, abs=1e-5)
        assert crop["xCoordinate"] == pytest.approx((1 - crop["width"]) / 2, abs=1e-5)

    def test_exact_banner_ratio_is_the_whole_image(self):
        crop, _ = theme_tools.compute_banner_crop(1920, 216)
        assert crop == {"xCoordinate": 0.0, "yCoordinate": 0.0, "width": 1.0}

    def test_caller_values_are_kept(self):
        crop, _ = theme_tools.compute_banner_crop(
            3840, 2160, x_coordinate=0.1, y_coordinate=0.2, width=0.5
        )
        assert crop == {"xCoordinate": 0.1, "yCoordinate": 0.2, "width": 0.5}

    def test_width_only_is_centred(self):
        crop, _ = theme_tools.compute_banner_crop(3840, 2160, width=0.5)
        assert crop["xCoordinate"] == 0.25

    def test_too_small_image_is_refused(self):
        with pytest.raises(UserInputError, match="1280x144"):
            theme_tools.compute_banner_crop(1000, 800)

    def test_crop_below_minimum_is_refused(self):
        with pytest.raises(UserInputError, match="1280x144"):
            theme_tools.compute_banner_crop(1920, 1080, width=0.5)

    def test_crop_off_the_right_edge_is_refused(self):
        with pytest.raises(UserInputError, match="right edge"):
            theme_tools.compute_banner_crop(3840, 2160, x_coordinate=0.8, width=0.5)

    def test_crop_off_the_bottom_is_refused(self):
        with pytest.raises(UserInputError, match="bottom"):
            theme_tools.compute_banner_crop(1920, 216, y_coordinate=0.5)

    @pytest.mark.parametrize("field", ["x_coordinate", "y_coordinate", "width"])
    def test_out_of_range_values_are_refused(self, field):
        with pytest.raises(UserInputError, match="between 0 and 1"):
            theme_tools.compute_banner_crop(1920, 1080, **{field: 1.5})

    def test_zero_width_is_refused(self):
        with pytest.raises(UserInputError, match="greater than 0"):
            theme_tools.compute_banner_crop(1920, 1080, width=0)

    def test_unknown_dimensions_fall_back_with_a_note(self):
        crop, notes = theme_tools.compute_banner_crop(None, None)
        assert crop == {"xCoordinate": 0.0, "yCoordinate": 0.0, "width": 1.0}
        assert "could not be centred" in notes[0]


# ---------------------------------------------------------------------------
# set_shared_drive_theme
# ---------------------------------------------------------------------------


class TestSetSharedDriveTheme:
    @pytest.mark.asyncio
    async def test_requires_exactly_one_source(self):
        service = FakeDrive()
        with pytest.raises(UserInputError, match="exactly one"):
            await set_shared_drive_theme(service, USER, drive_id="d1")
        with pytest.raises(UserInputError, match="exactly one"):
            await set_shared_drive_theme(
                service, USER, drive_id="d1", theme_id="bok", image_file_id="img1"
            )
        assert service.calls == []

    @pytest.mark.asyncio
    async def test_crop_with_theme_is_refused(self):
        with pytest.raises(UserInputError, match="only apply to image_file_id"):
            await set_shared_drive_theme(
                FakeDrive(), USER, drive_id="d1", theme_id="bok", width=0.5
            )

    @pytest.mark.asyncio
    async def test_manager_sets_image_as_member_with_before_and_after(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive()
        service.images["img1"] = PNG
        service.after_update["d1"] = {
            "themeId": None,
            "backgroundImageLink": "https://lh3/new",
        }

        result = await set_shared_drive_theme(
            service, USER, drive_id="d1", image_file_id="img1"
        )

        update = service.kwargs_for("drives.update")[0]
        assert update["useDomainAdminAccess"] is False
        image = update["body"]["backgroundImageFile"]
        assert image["id"] == "img1"
        assert set(image) == {"id", "xCoordinate", "yCoordinate", "width"}
        assert "themeId" not in update["body"]
        assert "Access: drive Manager" in result
        assert ABACUS_LINK in result and "https://lh3/new" in result
        assert "Stock theme: abacus (matched" in result
        assert "Stock theme: none (custom image)" in result
        assert "#1a73e8" in result
        assert "⚠️" not in result

    @pytest.mark.asyncio
    async def test_sets_a_stock_theme(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive()
        # Real Drive behaviour: the image and colour change, themeId does not
        # come back.
        service.after_update["d1"] = {
            "backgroundImageLink": BOK_LINK,
            "colorRgb": "#689f38",
        }

        result = await set_shared_drive_theme(
            service, USER, drive_id="d1", theme_id="bok"
        )

        assert service.kwargs_for("drives.update")[0]["body"] == {"themeId": "bok"}
        assert "Stock theme: abacus (matched" in result
        assert "Stock theme: bok (matched" in result
        assert "#689f38" in result
        # Regression: a successful theme change must not raise the
        # "verify the banner" warning just because themeId is not readable.
        assert "⚠️" not in result
        assert "themeId:" not in result

    @pytest.mark.asyncio
    async def test_theme_change_verified_by_colour_when_link_differs(self):
        """The image link may come back in a different form (signed, resized);
        a matching colour still proves the theme applied."""
        service = FakeDrive()
        service.member_view["d1"] = _drive()
        service.after_update["d1"] = {
            "backgroundImageLink": "https://lh3.googleusercontent.com/resized",
            "colorRgb": "#689F38",
        }
        result = await set_shared_drive_theme(
            service, USER, drive_id="d1", theme_id="bok"
        )
        assert "⚠️" not in result

    @pytest.mark.asyncio
    async def test_reapplying_the_current_theme_is_not_flagged(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive()
        result = await set_shared_drive_theme(
            service, USER, drive_id="d1", theme_id="abacus"
        )
        assert "⚠️" not in result

    @pytest.mark.asyncio
    async def test_unknown_theme_is_refused_before_update(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive()
        with pytest.raises(UserInputError, match="Valid theme IDs: abacus, bok"):
            await set_shared_drive_theme(service, USER, drive_id="d1", theme_id="nope")
        assert "drives.update" not in service.call_names()

    @pytest.mark.asyncio
    async def test_theme_list_unavailable_still_proceeds_with_a_note(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive()
        service.about_error = _http_error(403, "forbidden")
        service.after_update["d1"] = {"backgroundImageLink": BOK_LINK}

        result = await set_shared_drive_theme(
            service, USER, drive_id="d1", theme_id="bok"
        )
        assert "Could not read the list of Google themes" in result
        assert "Could not verify the change" in result
        assert "Google's theme list could not be read" in result
        assert service.kwargs_for("drives.update")

    @pytest.mark.asyncio
    async def test_dry_run_changes_nothing(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive()
        service.images["img1"] = PNG

        result = await set_shared_drive_theme(
            service, USER, drive_id="d1", image_file_id="img1", dry_run=True
        )

        assert "DRY RUN" in result and "Would set" in result
        assert "drives.update" not in service.call_names()

    @pytest.mark.asyncio
    async def test_non_manager_falls_back_to_domain_admin(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive(can_change=False)
        service.admin_view["d1"] = _drive(can_change=False)
        service.images["img1"] = PNG
        service.after_update["d1"] = {"backgroundImageLink": "https://lh3/new"}

        result = await set_shared_drive_theme(
            service, USER, drive_id="d1", image_file_id="img1"
        )

        assert service.kwargs_for("drives.update")[0]["useDomainAdminAccess"] is True
        # The re-read must use the same access mode as the update.
        assert service.kwargs_for("drives.get")[-1].get("useDomainAdminAccess") is True
        assert "Access: domain admin" in result

    @pytest.mark.asyncio
    async def test_non_member_admin_uses_domain_admin(self):
        service = FakeDrive()
        service.admin_view["d1"] = _drive(can_change=False)
        service.after_update["d1"] = {"themeId": "bok"}

        await set_shared_drive_theme(service, USER, drive_id="d1", theme_id="bok")
        assert service.kwargs_for("drives.update")[0]["useDomainAdminAccess"] is True

    @pytest.mark.asyncio
    async def test_non_manager_non_admin_is_refused(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive(can_change=False)
        service.admin_view["d1"] = _http_error(403, "insufficientFilePermissions")

        with pytest.raises(UserInputError, match="not a Manager"):
            await set_shared_drive_theme(service, USER, drive_id="d1", theme_id="bok")
        assert "drives.update" not in service.call_names()

    @pytest.mark.asyncio
    async def test_explicit_member_mode_never_tries_admin(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive(can_change=False)
        service.admin_view["d1"] = _drive()

        with pytest.raises(UserInputError, match="only a Manager"):
            await set_shared_drive_theme(
                service,
                USER,
                drive_id="d1",
                theme_id="bok",
                use_domain_admin_access=False,
            )
        assert all(
            not kw.get("useDomainAdminAccess")
            for kw in service.kwargs_for("drives.get")
        )

    @pytest.mark.asyncio
    async def test_explicit_admin_mode_refusal_is_explained(self):
        service = FakeDrive()
        service.admin_view["d1"] = _http_error(403, "forbidden")
        with pytest.raises(UserInputError, match="must be a Workspace admin"):
            await set_shared_drive_theme(
                service,
                USER,
                drive_id="d1",
                theme_id="bok",
                use_domain_admin_access=True,
            )

    @pytest.mark.asyncio
    async def test_unknown_drive_is_refused(self):
        with pytest.raises(UserInputError, match="not a shared drive"):
            await set_shared_drive_theme(
                FakeDrive(), USER, drive_id="nope", theme_id="bok"
            )

    @pytest.mark.asyncio
    async def test_non_image_file_is_refused(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive()
        service.images["doc"] = {**PNG, "id": "doc", "mimeType": "application/pdf"}
        with pytest.raises(UserInputError, match="JPG or PNG"):
            await set_shared_drive_theme(
                service, USER, drive_id="d1", image_file_id="doc"
            )

    @pytest.mark.asyncio
    async def test_trashed_image_is_refused(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive()
        service.images["img1"] = {**PNG, "trashed": True}
        with pytest.raises(UserInputError, match="trash"):
            await set_shared_drive_theme(
                service, USER, drive_id="d1", image_file_id="img1"
            )

    @pytest.mark.asyncio
    async def test_missing_image_is_refused(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive()
        with pytest.raises(UserInputError, match="not found"):
            await set_shared_drive_theme(
                service, USER, drive_id="d1", image_file_id="gone"
            )

    @pytest.mark.asyncio
    async def test_image_that_did_not_stick_is_flagged(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive()
        service.images["img1"] = PNG
        # No after_update: the re-read shows the old link.

        result = await set_shared_drive_theme(
            service, USER, drive_id="d1", image_file_id="img1"
        )
        assert "did not change" in result

    @pytest.mark.asyncio
    async def test_theme_that_did_not_stick_is_flagged(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive()
        result = await set_shared_drive_theme(
            service, USER, drive_id="d1", theme_id="bok"
        )
        assert "verify the banner" in result


# ---------------------------------------------------------------------------
# get_shared_drive_theme
# ---------------------------------------------------------------------------


class TestGetSharedDriveTheme:
    @pytest.mark.asyncio
    async def test_returns_the_three_fields(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive()
        result = await get_shared_drive_theme(service, USER, drive_id="d1")
        assert "Stock theme: abacus (matched from the banner image)" in result
        assert "colorRgb: #1a73e8" in result
        assert f"backgroundImageLink: {ABACUS_LINK}" in result
        assert "You can change it: yes" in result
        assert "themeId:" not in result

    @pytest.mark.asyncio
    async def test_custom_image_is_reported_as_custom(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive(
            backgroundImageLink="https://lh3.googleusercontent.com/custom"
        )
        result = await get_shared_drive_theme(service, USER, drive_id="d1")
        assert "Stock theme: none (custom image)" in result

    @pytest.mark.asyncio
    async def test_signed_link_still_matches_the_stock_theme(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive(backgroundImageLink=BOK_LINK + "?sig=xyz")
        result = await get_shared_drive_theme(service, USER, drive_id="d1")
        assert "Stock theme: bok (matched" in result

    @pytest.mark.asyncio
    async def test_unreadable_theme_list_says_unknown(self):
        service = FakeDrive()
        service.member_view["d1"] = _drive()
        service.about_error = _http_error(403, "forbidden")
        result = await get_shared_drive_theme(service, USER, drive_id="d1")
        assert "unknown: Google's theme list could not be read" in result

    @pytest.mark.asyncio
    async def test_admin_read_passes_the_flag(self):
        service = FakeDrive()
        service.admin_view["d1"] = _drive(can_change=False)
        await get_shared_drive_theme(
            service, USER, drive_id="d1", use_domain_admin_access=True
        )
        assert service.kwargs_for("drives.get")[0]["useDomainAdminAccess"] is True

    @pytest.mark.asyncio
    async def test_unknown_drive_is_refused(self):
        with pytest.raises(UserInputError, match="not a shared drive"):
            await get_shared_drive_theme(FakeDrive(), USER, drive_id="nope")


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


def _row(drive, entity, depth="0", folder_id=None, **flags):
    return {
        "drive": drive,
        "entity": entity,
        "path": drive if depth == "0" else f"{drive}/sub",
        "folder_id": folder_id or f"id-{drive}",
        "depth": depth,
        "restricted": flags.get("restricted", "FALSE"),
        "external": flags.get("external", "FALSE"),
        "hub": flags.get("hub", "FALSE"),
    }


class TestRegistryHelpers:
    def test_one_entry_per_drive_using_the_depth_0_row(self):
        rows = [
            _row("JIT-Ops", "JIT", depth="1", folder_id="child"),
            _row("JIT-Ops", "JIT", folder_id="0AJIT"),
            _row("JIT-Ops", "JIT", depth="2", folder_id="grandchild"),
            _row("BIR-Site", "BIR", folder_id="0ABIR"),
        ]
        drives = theme_tools.drives_from_registry(rows)
        assert [(d["drive_name"], d["drive_id"]) for d in drives] == [
            ("JIT-Ops", "0AJIT"),
            ("BIR-Site", "0ABIR"),
        ]

    def test_drive_without_a_drive_level_row_has_no_id(self):
        drives = theme_tools.drives_from_registry([_row("X", "OTB", depth="1")])
        assert drives[0]["drive_id"] is None

    @pytest.mark.parametrize(
        "flags, entity, expected",
        [
            ({"restricted": "TRUE", "external": "TRUE"}, "JIT", "Restricted"),
            ({"external": "TRUE", "hub": "TRUE"}, "JIT", "ExternalShare"),
            ({"hub": "true"}, "OTB", "Hub"),
            ({}, "vale", "VALE"),
            ({}, "Hub", "Hub"),
            ({}, "Unknown", None),
        ],
    )
    def test_category_precedence(self, flags, entity, expected):
        category, reason = theme_tools.classify_registry_drive(
            _row("D", entity, **flags)
        )
        assert category == expected
        assert reason

    def test_mapping_keys_are_case_insensitive(self):
        assert theme_tools.normalise_entity_images(
            {"jit": "a", "EXTERNALSHARE": "b"}
        ) == {
            "JIT": {"image": "a", "accent": None},
            "ExternalShare": {"image": "b", "accent": None},
        }

    def test_mapping_accepts_image_and_accent_per_category(self):
        assert theme_tools.normalise_entity_images(
            {"JIT": {"image": "a", "accent": "C8102E"}, "BIR": "b"},
            default_accent="#0B2545",
        ) == {
            "JIT": {"image": "a", "accent": "#c8102e"},
            "BIR": {"image": "b", "accent": "#0b2545"},
        }

    def test_mapping_rejects_unknown_keys_in_a_category(self):
        with pytest.raises(UserInputError, match="unknown key"):
            theme_tools.normalise_entity_images({"JIT": {"image": "a", "color": "x"}})

    def test_mapping_rejects_a_bad_accent(self):
        with pytest.raises(UserInputError, match=r"entity_images\['JIT'\]\.accent"):
            theme_tools.normalise_entity_images(
                {"JIT": {"image": "a", "accent": "navy"}}
            )

    def test_mapping_dict_needs_an_image(self):
        with pytest.raises(UserInputError, match="blank image"):
            theme_tools.normalise_entity_images({"JIT": {"accent": "#000000"}})

    def test_unknown_mapping_key_is_refused(self):
        with pytest.raises(UserInputError, match="Unknown category"):
            theme_tools.normalise_entity_images({"Acme": "a"})

    def test_blank_image_id_is_refused(self):
        with pytest.raises(UserInputError, match="blank"):
            theme_tools.normalise_entity_images({"JIT": "  "})

    def test_empty_mapping_is_refused(self):
        with pytest.raises(UserInputError, match="required"):
            theme_tools.normalise_entity_images({})


# ---------------------------------------------------------------------------
# set_shared_drive_themes_from_registry
# ---------------------------------------------------------------------------


def _sheets_with(rows):
    header = [
        "drive",
        "entity",
        "path",
        "folder_name",
        "folder_id",
        "depth",
        "restricted",
        "external",
        "hub",
    ]
    values = [header] + [
        [
            r["drive"],
            r["entity"],
            r["path"],
            r["drive"],
            r["folder_id"],
            r["depth"],
            r["restricted"],
            r["external"],
            r["hub"],
        ]
        for r in rows
    ]
    sheets = MagicMock()
    sheets.spreadsheets.return_value.values.return_value.get.return_value = _request(
        {"values": values}
    )
    return sheets


class TestSetThemesFromRegistry:
    def _setup(self):
        service = FakeDrive()
        for drive_id, name in (
            ("0AJIT", "JIT-Ops"),
            ("0ABIR", "BIR-Site"),
            ("0AHR", "JIT-HR"),
        ):
            service.member_view[drive_id] = _drive(drive_id=drive_id, name=name)
            service.after_update[drive_id] = {"backgroundImageLink": f"new-{drive_id}"}
        service.images["img-jit"] = {**PNG, "id": "img-jit"}
        service.images["img-restricted"] = {**PNG, "id": "img-restricted"}
        rows = [
            _row("JIT-Ops", "JIT", folder_id="0AJIT"),
            _row("JIT-Ops", "JIT", depth="1", folder_id="child"),
            _row("BIR-Site", "BIR", folder_id="0ABIR"),
            _row("JIT-HR", "JIT", folder_id="0AHR", restricted="TRUE"),
            _row("Orphan", "OTB", depth="1"),
        ]
        return service, _sheets_with(rows)

    async def _run(self, service, sheets, **kwargs):
        with patch.object(
            theme_tools, "_hub_registry_service", new=AsyncMock(return_value=sheets)
        ):
            return await set_themes_from_registry(
                service,
                USER,
                registry_spreadsheet_id="reg1",
                entity_images=kwargs.pop(
                    "entity_images",
                    {"JIT": "img-jit", "Restricted": "img-restricted"},
                ),
                **kwargs,
            )

    @pytest.mark.asyncio
    async def test_applies_mapped_images_and_skips_the_rest(self):
        service, sheets = self._setup()

        result = await self._run(service, sheets)

        updates = {
            kw["driveId"]: kw["body"]["backgroundImageFile"]["id"]
            for kw in service.kwargs_for("drives.update")
        }
        assert updates == {"0AJIT": "img-jit", "0AHR": "img-restricted"}
        assert "4 drive(s); applied 2, skipped 2, failed 0" in result
        assert "Category: Restricted (restricted=TRUE)" in result
        assert "BIR (entity=BIR) has no image" in result
        assert "'Orphan': skipped, no drive-level" in result

    @pytest.mark.asyncio
    async def test_dry_run_updates_nothing(self):
        service, sheets = self._setup()
        result = await self._run(service, sheets, dry_run=True)
        assert "drives.update" not in service.call_names()
        assert result.startswith("DRY RUN")
        assert "would apply 2" in result

    @pytest.mark.asyncio
    async def test_one_failing_drive_does_not_stop_the_run(self):
        service, sheets = self._setup()
        service.update_error["0AJIT"] = _http_error(400, "badRequest")

        result = await self._run(service, sheets)

        assert "applied 1, skipped 2, failed 1" in result
        assert "❌ 'JIT-Ops'" in result
        assert "0AHR" in [kw["driveId"] for kw in service.kwargs_for("drives.update")]

    @pytest.mark.asyncio
    async def test_bad_image_stops_the_run_before_any_drive_changes(self):
        service, sheets = self._setup()
        service.images["img-jit"] = {**PNG, "id": "img-jit", "mimeType": "image/gif"}

        with pytest.raises(UserInputError, match="JPG or PNG"):
            await self._run(service, sheets)
        assert "drives.update" not in service.call_names()

    @pytest.mark.asyncio
    async def test_each_image_is_fetched_once(self):
        service, sheets = self._setup()
        await self._run(
            service, sheets, entity_images={"JIT": "img-jit", "Restricted": "img-jit"}
        )
        fetched = [kw["fileId"] for kw in service.kwargs_for("files.get")]
        assert fetched == ["img-jit"]

    @pytest.mark.asyncio
    async def test_missing_columns_are_reported(self):
        service = FakeDrive()
        sheets = MagicMock()
        sheets.spreadsheets.return_value.values.return_value.get.return_value = (
            _request({"values": [["drive", "folder_id"], ["A", "0A"]]})
        )
        service.images["img-jit"] = {**PNG, "id": "img-jit"}
        with pytest.raises(UserInputError, match="entity"):
            await self._run(service, sheets, entity_images={"JIT": "img-jit"})

    @pytest.mark.asyncio
    async def test_missing_sheets_scope_is_actionable(self):
        service, _ = self._setup()
        with patch.object(
            theme_tools,
            "_hub_registry_service",
            new=AsyncMock(side_effect=RuntimeError("no scope")),
        ):
            with pytest.raises(UserInputError, match="Enable the 'sheets' service"):
                await set_themes_from_registry(
                    service,
                    USER,
                    registry_spreadsheet_id="reg1",
                    entity_images={"JIT": "img-jit"},
                )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

THEME_TOOLS = {
    "list_drive_themes",
    "get_shared_drive_theme",
    "set_shared_drive_theme",
    "set_shared_drive_themes_from_registry",
}


class TestRegistration:
    def test_tools_register(self):
        from core.server import server
        from core.tool_registry import get_tool_components

        assert THEME_TOOLS <= set(get_tool_components(server))

    def test_tools_load_at_the_extended_tier(self):
        from core.tool_tier_loader import ToolTierLoader

        extended = set(ToolTierLoader().get_tools_up_to_tier("extended", ["drive"]))
        assert THEME_TOOLS <= extended

    def test_tools_are_not_blocked(self):
        from core.tool_policy import BLOCKED_TOOLS

        assert not THEME_TOOLS & set(BLOCKED_TOOLS)

    def test_module_is_imported_by_both_entry_points(self):
        root = Path(__file__).resolve().parent.parent.parent
        assert "gdrive.shared_drive_theme_tools" in (root / "main.py").read_text()
        assert (
            "gdrive.shared_drive_theme_tools"
            in (root / "fastmcp_server.py").read_text()
        )

    def test_module_never_deletes_or_shares(self):
        source = Path(theme_tools.__file__).read_text()
        for forbidden in (".delete(", "permissions()", ".trash"):
            assert forbidden not in source


# ---------------------------------------------------------------------------
# Accent colour helpers
# ---------------------------------------------------------------------------

# A mocked theme list with known colours, used for nearest-colour checks.
COLOUR_THEMES = [
    {
        "id": "zulu_blue",
        "colorRgb": "#1a237e",
        "backgroundImageLink": "https://t/z.jpg",
    },
    {
        "id": "alpha_red",
        "colorRgb": "#c62828",
        "backgroundImageLink": "https://t/a.jpg",
    },
    {"id": "mid_grey", "colorRgb": "#808080", "backgroundImageLink": "https://t/m.jpg"},
    {
        "id": "broken",
        "colorRgb": "not-a-colour",
        "backgroundImageLink": "https://t/b.jpg",
    },
]


class TestAccentHelpers:
    @pytest.mark.parametrize(
        "raw, expected",
        [("#0B2545", "#0b2545"), ("0b2545", "#0b2545"), ("  #ABCDEF ", "#abcdef")],
    )
    def test_normalise_hex_accepts_common_forms(self, raw, expected):
        assert theme_tools.normalise_hex(raw) == expected

    @pytest.mark.parametrize("raw", ["navy", "#fff", "#12345g", "", None, "#1234567"])
    def test_normalise_hex_rejects_anything_else(self, raw):
        with pytest.raises(UserInputError, match="6-digit hex"):
            theme_tools.normalise_hex(raw)

    def test_rgb_distance(self):
        assert theme_tools.rgb_distance("#000000", "#000000") == 0
        assert theme_tools.rgb_distance("#000000", "#ffffff") == pytest.approx(
            441.673, abs=1e-3
        )
        assert theme_tools.rgb_distance("#000000", "#030400") == 5.0

    def test_nearest_theme_is_the_smallest_rgb_distance(self):
        nearest = theme_tools.nearest_stock_theme(COLOUR_THEMES, "#0b2545")
        assert nearest["id"] == "zulu_blue"
        assert nearest["colorRgb"] == "#1a237e"
        assert nearest["distance"] == pytest.approx(
            theme_tools.rgb_distance("#0b2545", "#1a237e")
        )
        assert (
            theme_tools.nearest_stock_theme(COLOUR_THEMES, "#c8102e")["id"]
            == "alpha_red"
        )

    def test_ties_go_to_the_first_id_alphabetically(self):
        themes = [
            {"id": "b_theme", "colorRgb": "#000010"},
            {"id": "a_theme", "colorRgb": "#100000"},
        ]
        assert theme_tools.nearest_stock_theme(themes, "#000000")["id"] == "a_theme"

    def test_themes_without_a_usable_colour_are_ignored(self):
        assert theme_tools.nearest_stock_theme([{"id": "x"}], "#000000") is None
        assert theme_tools.nearest_stock_theme(None, "#000000") is None
        assert (
            theme_tools.nearest_stock_theme(COLOUR_THEMES, "#7f7f7f")["id"]
            == "mid_grey"
        )


# ---------------------------------------------------------------------------
# list_drive_themes
# ---------------------------------------------------------------------------

list_drive_themes = _unwrap(theme_tools.list_drive_themes)


class TestListDriveThemes:
    @pytest.mark.asyncio
    async def test_lists_every_theme_sorted_by_id(self):
        service = FakeDrive()
        service.themes = list(COLOUR_THEMES)

        result = await list_drive_themes(service, USER)

        fields = service.kwargs_for("about.get")[0]["fields"]
        assert "driveThemes" in fields
        for part in ("id", "colorRgb", "backgroundImageLink"):
            assert part in fields
        lines = result.splitlines()
        assert lines[0].startswith("Shared drive themes (4)")
        ids = [line.split(" | ")[0].lstrip("- ") for line in lines[1:]]
        assert ids == ["alpha_red", "broken", "mid_grey", "zulu_blue"]
        assert (
            "- alpha_red | colorRgb: #c62828 | backgroundImageLink: https://t/a.jpg"
            in lines
        )

    @pytest.mark.asyncio
    async def test_is_read_only(self):
        service = FakeDrive()
        await list_drive_themes(service, USER)
        assert service.call_names() == ["about.get"]

    @pytest.mark.asyncio
    async def test_an_unreadable_theme_list_is_an_error_not_an_empty_list(self):
        service = FakeDrive()
        service.about_error = _http_error(403, "forbidden")
        with pytest.raises(HttpError):
            await list_drive_themes(service, USER)

    @pytest.mark.asyncio
    async def test_no_themes_is_reported(self):
        service = FakeDrive()
        service.themes = []
        assert "no shared drive themes" in await list_drive_themes(service, USER)


# ---------------------------------------------------------------------------
# accent_hex on set_shared_drive_theme
# ---------------------------------------------------------------------------


class TestAccentOnSetSharedDriveTheme:
    def _service(self):
        service = FakeDrive()
        service.themes = list(COLOUR_THEMES)
        service.member_view["d1"] = _drive()
        service.images["img1"] = PNG
        service.after_update["d1"] = {"backgroundImageLink": "https://lh3/new"}
        return service

    @pytest.mark.asyncio
    async def test_exact_colour_goes_in_the_same_update_as_the_image(self):
        service = self._service()

        result = await set_shared_drive_theme(
            service, USER, drive_id="d1", image_file_id="img1", accent_hex="0B2545"
        )

        updates = service.kwargs_for("drives.update")
        assert len(updates) == 1, "one update: no stock theme applied first"
        body = updates[0]["body"]
        assert body["colorRgb"] == "#0b2545"
        assert body["backgroundImageFile"]["id"] == "img1"
        assert "themeId" not in body
        assert "colorRgb=#0b2545" in result
        assert "Accent: #0b2545 applied exactly" in result
        expected = theme_tools.rgb_distance("#0b2545", "#1a237e")
        assert (
            f"Nearest stock theme: zulu_blue (#1a237e), RGB distance {expected:.1f}"
            in result
        )
        assert "⚠️" not in result

    @pytest.mark.asyncio
    async def test_dry_run_reports_the_accent_and_changes_nothing(self):
        service = self._service()
        result = await set_shared_drive_theme(
            service,
            USER,
            drive_id="d1",
            image_file_id="img1",
            accent_hex="#c8102e",
            dry_run=True,
        )
        assert "drives.update" not in service.call_names()
        assert "Would set" in result and "colorRgb=#c8102e" in result
        assert "Nearest stock theme: alpha_red" in result

    @pytest.mark.asyncio
    async def test_omitting_accent_keeps_the_existing_behaviour(self):
        service = self._service()
        result = await set_shared_drive_theme(
            service, USER, drive_id="d1", image_file_id="img1"
        )
        assert "colorRgb" not in service.kwargs_for("drives.update")[0]["body"]
        assert "Accent:" not in result

    @pytest.mark.asyncio
    async def test_accent_with_a_stock_theme_is_refused(self):
        service = self._service()
        with pytest.raises(UserInputError, match="only applies with image_file_id"):
            await set_shared_drive_theme(
                service, USER, drive_id="d1", theme_id="bok", accent_hex="#000000"
            )
        assert service.calls == []

    @pytest.mark.asyncio
    async def test_bad_hex_is_refused_before_any_call(self):
        service = self._service()
        with pytest.raises(UserInputError, match="6-digit hex"):
            await set_shared_drive_theme(
                service, USER, drive_id="d1", image_file_id="img1", accent_hex="navy"
            )
        assert service.calls == []

    @pytest.mark.asyncio
    async def test_a_colour_that_did_not_stick_is_flagged(self):
        service = self._service()
        service.ignore_color = True
        result = await set_shared_drive_theme(
            service, USER, drive_id="d1", image_file_id="img1", accent_hex="#0b2545"
        )
        assert "not the requested #0b2545" in result

    @pytest.mark.asyncio
    async def test_unreadable_theme_list_still_applies_the_exact_colour(self):
        service = self._service()
        service.about_error = _http_error(403, "forbidden")
        result = await set_shared_drive_theme(
            service, USER, drive_id="d1", image_file_id="img1", accent_hex="#0b2545"
        )
        assert service.kwargs_for("drives.update")[0]["body"]["colorRgb"] == "#0b2545"
        assert "Nearest stock theme: unknown" in result


# ---------------------------------------------------------------------------
# accent on set_shared_drive_themes_from_registry
# ---------------------------------------------------------------------------


class TestAccentFromRegistry(TestSetThemesFromRegistry):
    @pytest.mark.asyncio
    async def test_per_category_accent_and_default(self):
        service, sheets = self._setup()
        service.themes = list(COLOUR_THEMES)

        result = await self._run(
            service,
            sheets,
            entity_images={
                "JIT": {"image": "img-jit", "accent": "#c8102e"},
                "Restricted": "img-restricted",
            },
            accent_hex="#0b2545",
        )

        colours = {
            kw["driveId"]: kw["body"].get("colorRgb")
            for kw in service.kwargs_for("drives.update")
        }
        assert colours == {"0AJIT": "#c8102e", "0AHR": "#0b2545"}
        assert "Nearest stock theme: alpha_red" in result
        assert "Nearest stock theme: zulu_blue" in result

    @pytest.mark.asyncio
    async def test_no_accent_anywhere_leaves_colours_alone(self):
        service, sheets = self._setup()
        await self._run(service, sheets)
        for kw in service.kwargs_for("drives.update"):
            assert "colorRgb" not in kw["body"]

    @pytest.mark.asyncio
    async def test_a_bad_accent_stops_the_run_before_any_drive_changes(self):
        service, sheets = self._setup()
        with pytest.raises(UserInputError, match="6-digit hex"):
            await self._run(
                service,
                sheets,
                entity_images={"JIT": {"image": "img-jit", "accent": "blue"}},
            )
        assert "drives.update" not in service.call_names()

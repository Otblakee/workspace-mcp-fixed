"""Folder Registry hygiene: noise rules, audit, prune, and the reader guardrail.

What is pinned here:

* the noise rules are one definition (``drive_batch``) shared by
  ``walk_drive`` and the registry tools;
* ``walk_drive`` skips noise subtrees by default, can be told not to, and
  the sweep self-check does not report the skipped items as "missed";
* ``audit_folder_registry`` never writes;
* ``prune_folder_registry`` is a dry run by default, refuses a live run
  without ``confirm=True``, archives before it deletes, aborts if the
  archive append or the re-read disagrees, deletes bottom-up, and never
  touches depth 0/1 or opted-out rows;
* ``_read_registry_rows`` warns above 2 000 rows and refuses above 10 000
  unless told not to.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.utils import UserInputError  # noqa: E402
from gdrive import drive_batch  # noqa: E402
from gdrive import drive_migration_tools as mig  # noqa: E402
from gdrive import folder_registry_tools as reg  # noqa: E402
from tests.gdrive.test_drive_migration_tools import (  # noqa: E402
    FOLDER,
    USER,
    FakeTree,
    _file,
    _report_rows,
    _unwrap,
)

walk_drive = _unwrap(mig.walk_drive)
audit_folder_registry = _unwrap(reg.audit_folder_registry)
prune_folder_registry = _unwrap(reg.prune_folder_registry)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(autouse=True)
def _isolated_attachment_dir(tmp_path, monkeypatch):
    import core.attachment_storage as storage_mod

    monkeypatch.setenv("WORKSPACE_ATTACHMENT_DIR", str(tmp_path))
    monkeypatch.setattr(storage_mod, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(storage_mod, "_attachment_storage", None)
    yield


# ---------------------------------------------------------------------------
# Noise rules
# ---------------------------------------------------------------------------


class TestNoiseRules:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("JIT/app/.git/objects/ab", ".git"),
            ("JIT/app/src", None),
            ("JIT/NODE_MODULES/x", "NODE_MODULES"),
            ("JIT/.hidden/x", ".hidden"),
            ("", None),
            ("JIT/dist", "dist"),
        ],
    )
    def test_excluded_segment(self, path, expected):
        assert drive_batch.excluded_segment(path) == expected

    def test_extra_segments_are_case_insensitive(self):
        assert drive_batch.excluded_segment("A/Scratch/B", ["scratch"]) == "Scratch"
        assert drive_batch.is_excluded_path("A/Scratch/B", ["scratch"])

    def test_depth(self):
        assert drive_batch.path_depth("") == 0
        assert drive_batch.path_depth("a/b/") == 2

    def test_default_list_covers_the_known_offenders(self):
        for name in (".git", "node_modules", ".venv", "__pycache__", "dist", "build"):
            assert name in drive_batch.EXCLUDED_PATH_SEGMENTS


# ---------------------------------------------------------------------------
# walk_drive exclusions
# ---------------------------------------------------------------------------


def _drive_with_checkout():
    nodes = {
        "D": {"id": "D", "name": "JIT-Working", "mimeType": FOLDER},
        "app": _file("app", "app", mime=FOLDER, parents=["D"]),
        "git": _file("git", ".git", mime=FOLDER, parents=["app"]),
        "objects": _file("objects", "objects", mime=FOLDER, parents=["git"]),
        "blob": _file("blob", "ab12", md5="m", size="1", parents=["objects"]),
        "src": _file("src", "src", mime=FOLDER, parents=["app"]),
        "main": _file("main", "main.py", md5="m2", size="2", parents=["src"]),
        "deep": _file("deep", "deep", mime=FOLDER, parents=["src"]),
        "deeper": _file("deeper", "deeper", mime=FOLDER, parents=["deep"]),
        "leaf": _file("leaf", "leaf.txt", md5="m3", size="3", parents=["deeper"]),
    }
    children = {
        "D": ["app"],
        "app": ["git", "src"],
        "git": ["objects"],
        "objects": ["blob"],
        "src": ["main", "deep"],
        "deep": ["deeper"],
        "deeper": ["leaf"],
    }
    return FakeTree(nodes, children, drive_id="D")


class TestWalkDriveExclusions:
    @pytest.mark.asyncio
    async def test_default_walk_skips_git_and_the_sweep_agrees(self, tmp_path):
        result = await walk_drive(_drive_with_checkout(), USER, "D")
        rows = _report_rows(tmp_path, "walk_JIT-Working")
        ids = {r["id"] for r in rows}

        assert "git" not in ids and "objects" not in ids and "blob" not in ids
        assert {"app", "src", "main", "deep", "deeper", "leaf"} <= ids
        assert "excluded_items: 1" in result
        assert "excluded_subtrees: 1" in result
        assert "app/.git" in result
        assert "Self-check passed" in result
        assert "reachable only via the drive-wide sweep" not in result
        assert "Exclusion rules were on" in result

    @pytest.mark.asyncio
    async def test_full_inventory_when_excludes_are_off(self, tmp_path):
        result = await walk_drive(
            _drive_with_checkout(), USER, "D", apply_default_excludes=False
        )
        rows = _report_rows(tmp_path, "walk_JIT-Working")
        ids = {r["id"] for r in rows}

        assert {"git", "objects", "blob"} <= ids
        assert "excluded_items" not in result
        assert "Exclusion rules were on" not in result

    @pytest.mark.asyncio
    async def test_extra_exclude_names_apply_on_top(self, tmp_path):
        await walk_drive(_drive_with_checkout(), USER, "D", exclude_names=["src"])
        rows = _report_rows(tmp_path, "walk_JIT-Working")
        ids = {r["id"] for r in rows}
        assert ids == {"app"}

    @pytest.mark.asyncio
    async def test_exclude_name_with_a_slash_is_refused(self):
        with pytest.raises(UserInputError, match="single folder or file names"):
            await walk_drive(_drive_with_checkout(), USER, "D", exclude_names=["a/b"])

    @pytest.mark.asyncio
    async def test_max_depth_records_but_does_not_descend(self, tmp_path):
        result = await walk_drive(_drive_with_checkout(), USER, "D", max_depth=2)
        rows = _report_rows(tmp_path, "walk_JIT-Working")
        ids = {r["id"] for r in rows}

        # app (d1), src (d2) recorded; main/deep (d3) are below the cap.
        assert ids == {"app", "src"}
        assert "depth_capped_subtrees: 1" in result
        assert "recorded but not descended" in result
        assert "reachable only via the drive-wide sweep" not in result

    @pytest.mark.asyncio
    async def test_max_depth_below_one_is_refused(self):
        with pytest.raises(UserInputError, match="max_depth"):
            await walk_drive(_drive_with_checkout(), USER, "D", max_depth=0)

    @pytest.mark.asyncio
    async def test_reconcile_path_is_untouched_by_the_defaults(self):
        """``_full_inventory`` with no exclude_segments walks everything."""
        rows, counts, _, _, _ = await mig._full_inventory(_drive_with_checkout(), "D")
        assert {r["id"] for r in rows} >= {"git", "objects", "blob"}
        assert "excluded_items" not in counts


# ---------------------------------------------------------------------------
# Reader guardrail
# ---------------------------------------------------------------------------


def _sheets_returning(values):
    sheets = MagicMock()
    request = MagicMock()
    request.execute.return_value = {"values": values}
    sheets.spreadsheets.return_value.values.return_value.get.return_value = request
    return sheets


def _registry(n_rows):
    values = [["drive", "folder_id", "path", "depth"]]
    for i in range(n_rows):
        values.append(["JIT", f"id{i}", f"JIT/a/b{i}", "2"])
    return values


class TestReaderGuardrail:
    @pytest.mark.asyncio
    async def test_refuses_above_max_rows(self):
        sheets = _sheets_returning(_registry(mig.REGISTRY_MAX_ROWS + 1))
        with pytest.raises(UserInputError, match="audit_folder_registry"):
            await mig._read_registry_rows(sheets, "sheet1", "FolderRegistry")

    @pytest.mark.asyncio
    async def test_enforce_limit_false_reads_anyway(self):
        sheets = _sheets_returning(_registry(mig.REGISTRY_MAX_ROWS + 1))
        rows = await mig._read_registry_rows(
            sheets, "sheet1", "FolderRegistry", enforce_limit=False
        )
        assert len(rows) == mig.REGISTRY_MAX_ROWS + 1

    @pytest.mark.asyncio
    async def test_warns_above_warn_rows(self, caplog):
        sheets = _sheets_returning(_registry(mig.REGISTRY_WARN_ROWS + 1))
        with caplog.at_level(logging.WARNING, logger=mig.logger.name):
            rows = await mig._read_registry_rows(sheets, "sheet1", "FolderRegistry")
        assert len(rows) == mig.REGISTRY_WARN_ROWS + 1
        assert any("audit_folder_registry" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_small_registry_is_quiet(self, caplog):
        sheets = _sheets_returning(_registry(10))
        with caplog.at_level(logging.WARNING, logger=mig.logger.name):
            await mig._read_registry_rows(sheets, "sheet1", "FolderRegistry")
        assert not caplog.records

    def test_thresholds_are_ordered(self):
        assert 0 < mig.REGISTRY_WARN_ROWS < mig.REGISTRY_MAX_ROWS


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


HEADER = ["drive", "folder_id", "path", "depth", "entity", "hub", "exclude", "action"]


def _row(drive, fid, path, depth=None, exclude="", action=""):
    d = str(depth) if depth is not None else ""
    return [drive, fid, path, d, "JIT", "FALSE", exclude, action]


def _clogged_registry():
    values = [HEADER]
    values += [
        _row("JIT", "root", "JIT", 0),
        _row("JIT", "work", "JIT/Working", 1),
        _row("JIT", "app", "JIT/Working/app", 2),
        _row("JIT", "git", "JIT/Working/app/.git", 3),
        _row("JIT", "obj", "JIT/Working/app/.git/objects", 4),
        _row("JIT", "o1", "JIT/Working/app/.git/objects/aa", 5),
        _row("JIT", "o2", "JIT/Working/app/.git/objects/ab", 5),
        _row("JIT", "src", "JIT/Working/app/src", 3),
        _row("JIT", "deep", "JIT/Working/app/src/a/b/c", 6),
        _row("JIT", "scratch", "JIT/Working/app/scratch", 3, exclude="TRUE"),
        _row("JIT", "tmpx", "JIT/Working/app/tmp", 3, action="skip"),
        _row("tmp", "tmproot", "tmp", 0),
        _row("tmp", "tmptop", "tmp/.git", 1),
        _row("JIT", "src", "JIT/Working/app/src", 3),
        [],
    ]
    return values


class TestClassification:
    def test_split_registry_range(self):
        assert reg.split_registry_range("FolderRegistry") == ("FolderRegistry", 1)
        assert reg.split_registry_range("'Folder Registry'!A1:Z") == (
            "Folder Registry",
            1,
        )
        assert reg.split_registry_range("Tab!A3:Z") == ("Tab", 3)
        assert reg.split_registry_range("Tab!A:Z") == ("Tab", 1)
        with pytest.raises(UserInputError):
            reg.split_registry_range("")

    def test_hits_and_protections(self):
        rows = mig.registry_rows_from_values(_clogged_registry())
        audit = reg.classify_registry_rows(rows, start_row=1, max_depth=4)

        noise = {h.folder_id: h for h in audit.noise_hits}
        assert set(noise) == {"git", "obj", "o1", "o2"}
        assert noise["git"].sheet_row == 5
        assert noise["git"].reason == "noise_segment:.git"
        assert [h.folder_id for h in audit.depth_hits] == ["deep"]
        # depth 0 and 1 rows are never matched, whatever they are called
        assert "tmproot" not in noise and "tmptop" not in noise
        # opted-out rows are counted, not matched
        assert audit.opted_out_rows == 2
        assert audit.blank_rows == 1
        assert audit.rows_per_drive == {"JIT": 12, "tmp": 2}
        assert audit.duplicate_folder_ids == {"src": [9, 15]}
        assert audit.depth_histogram[5] == 2

    def test_depth_falls_back_to_the_path(self):
        rows = mig.registry_rows_from_values(
            [["drive", "folder_id", "path"], ["JIT", "x", "JIT/a/.git"]]
        )
        audit = reg.classify_registry_rows(rows)
        assert audit.noise_hits and audit.noise_hits[0].depth == 2

    def test_start_row_offsets_sheet_rows(self):
        rows = mig.registry_rows_from_values(
            [["drive", "folder_id", "path", "depth"], ["JIT", "x", "JIT/a/.git", "2"]]
        )
        audit = reg.classify_registry_rows(rows, start_row=3)
        assert audit.noise_hits[0].sheet_row == 4

    def test_wide_parents(self):
        values = [["drive", "folder_id", "path", "depth"]]
        values += [["JIT", f"c{i}", f"JIT/big/child{i}", "2"] for i in range(5)]
        rows = mig.registry_rows_from_values(values)
        audit = reg.classify_registry_rows(rows, children_budget=3)
        assert audit.wide_parents == [("JIT/big", 5)]

    def test_missing_path_column_is_refused(self):
        rows = mig.registry_rows_from_values([["drive", "folder_id"], ["JIT", "x"]])
        with pytest.raises(UserInputError, match="path column"):
            reg.classify_registry_rows(rows)

    def test_contiguous_ranges_are_bottom_up(self):
        assert reg.contiguous_row_ranges([5, 6, 7, 2, 10, 11]) == [
            (10, 11),
            (5, 7),
            (2, 2),
        ]
        reqs = reg.delete_rows_requests(42, [5, 6, 7, 2])
        assert [r["deleteDimension"]["range"]["startIndex"] for r in reqs] == [4, 1]
        assert [r["deleteDimension"]["range"]["endIndex"] for r in reqs] == [7, 2]
        assert all(r["deleteDimension"]["range"]["sheetId"] == 42 for r in reqs)


# ---------------------------------------------------------------------------
# Sheets double for the tools
# ---------------------------------------------------------------------------


class FakeSheets:
    """In-memory spreadsheet: ``tabs`` maps title -> list of rows."""

    def __init__(self, tabs, *, append_reports=None, mutate_before_reread=None):
        self.tabs = {k: [list(r) for r in v] for k, v in tabs.items()}
        self.ids = {title: 100 + i for i, title in enumerate(self.tabs)}
        self.calls = []
        self.append_reports = append_reports
        self.mutate_before_reread = mutate_before_reread
        self._reads = 0

    def _request(self, result):
        request = MagicMock()
        request.execute.return_value = result
        return request

    def spreadsheets(self):
        fake = self

        class _Values:
            def get(self, spreadsheetId, range):
                tab, start = reg.split_registry_range(range)
                fake.calls.append(("values.get", range))
                if range.endswith("!1:1"):
                    rows = fake.tabs.get(tab, [])
                    return fake._request({"values": rows[:1]} if rows else {})
                fake._reads += 1
                if fake._reads == 2 and fake.mutate_before_reread:
                    fake.mutate_before_reread(fake.tabs)
                rows = fake.tabs.get(tab, [])[start - 1 :]
                return fake._request({"values": rows})

            def update(self, spreadsheetId, range, valueInputOption, body):
                tab, _ = reg.split_registry_range(range)
                fake.calls.append(("values.update", range, body))
                fake.tabs.setdefault(tab, [])
                if not fake.tabs[tab]:
                    fake.tabs[tab].append([])
                fake.tabs[tab][0] = list(body["values"][0])
                return fake._request({})

            def append(
                self, spreadsheetId, range, valueInputOption, insertDataOption, body
            ):
                tab, _ = reg.split_registry_range(range)
                fake.calls.append(("values.append", range, body))
                fake.tabs.setdefault(tab, []).extend([list(r) for r in body["values"]])
                reported = (
                    fake.append_reports
                    if fake.append_reports is not None
                    else len(body["values"])
                )
                return fake._request({"updates": {"updatedRows": reported}})

        class _Spreadsheets:
            def values(self):
                return _Values()

            def get(self, spreadsheetId, fields=None):
                fake.calls.append(("spreadsheets.get", fields))
                return fake._request(
                    {
                        "sheets": [
                            {"properties": {"sheetId": fake.ids[t], "title": t}}
                            for t in fake.tabs
                        ]
                    }
                )

            def batchUpdate(self, spreadsheetId, body):
                fake.calls.append(("batchUpdate", body))
                for req in body["requests"]:
                    if "addSheet" in req:
                        title = req["addSheet"]["properties"]["title"]
                        fake.tabs.setdefault(title, [])
                        fake.ids[title] = 100 + len(fake.ids)
                    elif "deleteDimension" in req:
                        rng = req["deleteDimension"]["range"]
                        title = next(
                            t for t, i in fake.ids.items() if i == rng["sheetId"]
                        )
                        del fake.tabs[title][rng["startIndex"] : rng["endIndex"]]
                return fake._request({"replies": [{}] * len(body["requests"])})

        return _Spreadsheets()

    def names(self):
        return [c[0] for c in self.calls]


# ---------------------------------------------------------------------------
# audit_folder_registry
# ---------------------------------------------------------------------------


class TestAuditTool:
    @pytest.mark.asyncio
    async def test_reports_and_writes_hit_list_without_writing_to_the_sheet(
        self, tmp_path
    ):
        sheets = FakeSheets({"FolderRegistry": _clogged_registry()})
        result = await audit_folder_registry(sheets, USER, "sheet1")

        assert "noise_rows: 4" in result
        assert "over_depth_rows: 1" in result
        assert ".git=4" in result
        assert "duplicate_folder_ids: 1" in result
        assert "rows_opted_out: 2" in result
        assert "Hit list (JSONL)" in result
        hits = _report_rows(tmp_path, "registry_audit")
        assert {h["folder_id"] for h in hits} == {"git", "obj", "o1", "o2", "deep"}
        assert set(sheets.names()) == {"values.get"}

    @pytest.mark.asyncio
    async def test_clean_registry_says_so(self):
        sheets = FakeSheets(
            {
                "FolderRegistry": [
                    HEADER,
                    _row("JIT", "r", "JIT", 0),
                    _row("JIT", "a", "JIT/A", 1),
                ]
            }
        )
        result = await audit_folder_registry(sheets, USER, "sheet1")
        assert "No noise rows found" in result

    @pytest.mark.asyncio
    async def test_input_validation(self):
        sheets = FakeSheets({"FolderRegistry": _clogged_registry()})
        with pytest.raises(UserInputError):
            await audit_folder_registry(sheets, USER, "")
        with pytest.raises(UserInputError, match="max_depth"):
            await audit_folder_registry(sheets, USER, "sheet1", max_depth=1)
        with pytest.raises(UserInputError, match="single folder names"):
            await audit_folder_registry(sheets, USER, "sheet1", exclude_names=["a/b"])


# ---------------------------------------------------------------------------
# prune_folder_registry
# ---------------------------------------------------------------------------


class TestPruneTool:
    @pytest.mark.asyncio
    async def test_dry_run_by_default_writes_nothing(self, tmp_path):
        sheets = FakeSheets({"FolderRegistry": _clogged_registry()})
        result = await prune_folder_registry(sheets, USER, "sheet1")

        assert "DRY RUN" in result
        assert "Would archive 4 row(s)" in result
        assert "prune_over_depth=True" in result
        assert set(sheets.names()) == {"values.get"}
        assert len(sheets.tabs["FolderRegistry"]) == len(_clogged_registry())
        plan = _report_rows(tmp_path, "registry_prune")
        assert [h["sheet_row"] for h in plan] == [5, 6, 7, 8]

    @pytest.mark.asyncio
    async def test_live_without_confirm_is_refused_before_any_read(self):
        sheets = FakeSheets({"FolderRegistry": _clogged_registry()})
        with pytest.raises(UserInputError, match="confirm=True"):
            await prune_folder_registry(sheets, USER, "sheet1", dry_run=False)
        assert sheets.calls == []

    @pytest.mark.asyncio
    async def test_live_archives_then_deletes_bottom_up(self, tmp_path):
        original = _clogged_registry()
        sheets = FakeSheets({"FolderRegistry": original})
        result = await prune_folder_registry(
            sheets, USER, "sheet1", dry_run=False, confirm=True
        )

        assert "Archived 4 row(s)" in result
        names = sheets.names()
        # archive tab created, header written, rows appended, then one delete
        assert names.index("values.append") < names.index(
            "batchUpdate", names.index("values.append")
        )
        archive = sheets.tabs["RegistryArchive"]
        assert archive[0] == HEADER + reg.ARCHIVE_EXTRA_COLUMNS
        assert [r[1] for r in archive[1:]] == ["git", "obj", "o1", "o2"]
        assert [r[-1] for r in archive[1:]] == [5, 6, 7, 8]
        assert all(r[-3] == USER for r in archive[1:])
        assert archive[1][-2] == "prune_folder_registry:noise_segment:.git"

        remaining = [r[1] for r in sheets.tabs["FolderRegistry"][1:] if r]
        assert "git" not in remaining and "o2" not in remaining
        assert {
            "root",
            "work",
            "app",
            "src",
            "deep",
            "scratch",
            "tmpx",
            "tmproot",
            "tmptop",
        } <= set(remaining)

        delete_bodies = [
            c[1]
            for c in sheets.calls
            if c[0] == "batchUpdate" and "deleteDimension" in c[1]["requests"][0]
        ]
        assert len(delete_bodies) == 1
        ranges = [r["deleteDimension"]["range"] for r in delete_bodies[0]["requests"]]
        assert ranges == [
            {
                "sheetId": sheets.ids["FolderRegistry"],
                "dimension": "ROWS",
                "startIndex": 4,
                "endIndex": 8,
            }
        ]
        removed = _report_rows(tmp_path, "registry_prune")
        assert len(removed) == 4

    @pytest.mark.asyncio
    async def test_prune_over_depth_includes_depth_hits(self):
        sheets = FakeSheets({"FolderRegistry": _clogged_registry()})
        result = await prune_folder_registry(
            sheets, USER, "sheet1", dry_run=False, confirm=True, prune_over_depth=True
        )
        assert "Archived 5 row(s)" in result
        assert "deep" in [r[1] for r in sheets.tabs["RegistryArchive"][1:]]

    @pytest.mark.asyncio
    async def test_existing_archive_with_wrong_header_is_refused(self):
        sheets = FakeSheets(
            {
                "FolderRegistry": _clogged_registry(),
                "RegistryArchive": [["something", "else"]],
            }
        )
        with pytest.raises(UserInputError, match="header does not match"):
            await prune_folder_registry(
                sheets, USER, "sheet1", dry_run=False, confirm=True
            )
        assert "values.append" not in sheets.names()
        assert len(sheets.tabs["FolderRegistry"]) == len(_clogged_registry())

    @pytest.mark.asyncio
    async def test_short_append_aborts_before_delete(self):
        sheets = FakeSheets({"FolderRegistry": _clogged_registry()}, append_reports=2)
        with pytest.raises(UserInputError, match="nothing has been removed"):
            await prune_folder_registry(
                sheets, USER, "sheet1", dry_run=False, confirm=True
            )
        assert not any("deleteDimension" in str(c) for c in sheets.calls)
        assert len(sheets.tabs["FolderRegistry"]) == len(_clogged_registry())

    @pytest.mark.asyncio
    async def test_registry_changing_under_us_aborts_before_delete(self):
        def mutate(tabs):
            tabs["FolderRegistry"].insert(3, _row("JIT", "new", "JIT/Working/new", 2))

        sheets = FakeSheets(
            {"FolderRegistry": _clogged_registry()}, mutate_before_reread=mutate
        )
        with pytest.raises(
            UserInputError, match="changed between the read and the delete"
        ):
            await prune_folder_registry(
                sheets, USER, "sheet1", dry_run=False, confirm=True
            )
        assert not any("deleteDimension" in str(c) for c in sheets.calls)
        assert "new" in [r[1] for r in sheets.tabs["FolderRegistry"][1:] if r]
        assert "git" in [r[1] for r in sheets.tabs["FolderRegistry"][1:] if r]

    @pytest.mark.asyncio
    async def test_max_rows_refuses_a_bulk_misfire(self):
        sheets = FakeSheets({"FolderRegistry": _clogged_registry()})
        with pytest.raises(UserInputError, match="max_rows=2"):
            await prune_folder_registry(sheets, USER, "sheet1", max_rows=2)
        assert set(sheets.names()) == {"values.get"}

    @pytest.mark.asyncio
    async def test_nothing_to_prune(self):
        sheets = FakeSheets({"FolderRegistry": [HEADER, _row("JIT", "r", "JIT", 0)]})
        result = await prune_folder_registry(
            sheets, USER, "sheet1", dry_run=False, confirm=True
        )
        assert "Nothing to prune" in result
        assert set(sheets.names()) == {"values.get"}

    @pytest.mark.asyncio
    async def test_archive_tab_must_differ_from_registry_tab(self):
        sheets = FakeSheets({"FolderRegistry": _clogged_registry()})
        with pytest.raises(UserInputError, match="must not be the registry tab"):
            await prune_folder_registry(
                sheets, USER, "sheet1", archive_tab="FolderRegistry"
            )

    @pytest.mark.asyncio
    async def test_offset_range_deletes_the_right_sheet_rows(self):
        # Two banner rows above the header: header is sheet row 3.
        values = [["banner"], [], *_clogged_registry()]
        sheets = FakeSheets({"FolderRegistry": values})
        await prune_folder_registry(
            sheets,
            USER,
            "sheet1",
            registry_range="FolderRegistry!A3:Z",
            dry_run=False,
            confirm=True,
        )
        remaining = [r[1] for r in sheets.tabs["FolderRegistry"][3:] if r]
        assert "git" not in remaining and "app" in remaining
        assert [r[-1] for r in sheets.tabs["RegistryArchive"][1:]] == [7, 8, 9, 10]


# ---------------------------------------------------------------------------
# Registration and policy
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_both_tools_are_in_the_drive_extended_tier(self):
        config = yaml.safe_load((REPO_ROOT / "core" / "tool_tiers.yaml").read_text())
        assert "audit_folder_registry" in config["drive"]["extended"]
        assert "prune_folder_registry" in config["drive"]["extended"]

    def test_module_is_imported_by_both_entrypoints(self):
        for name in ("main.py", "fastmcp_server.py"):
            assert "gdrive.folder_registry_tools" in (REPO_ROOT / name).read_text()

    def test_module_never_touches_drive_files(self):
        source = (REPO_ROOT / "gdrive" / "folder_registry_tools.py").read_text()
        assert ".files()" not in source
        assert ".delete(" not in source
        assert "trashed" not in source

    def test_prune_requires_write_scope_and_audit_does_not(self):
        from auth.scopes import SHEETS_READONLY_SCOPE, SHEETS_WRITE_SCOPE

        prune_fn = (
            reg.prune_folder_registry.fn
            if hasattr(reg.prune_folder_registry, "fn")
            else reg.prune_folder_registry
        )
        audit_fn = (
            reg.audit_folder_registry.fn
            if hasattr(reg.audit_folder_registry, "fn")
            else reg.audit_folder_registry
        )
        assert SHEETS_WRITE_SCOPE in getattr(prune_fn, "_required_google_scopes", [])
        assert SHEETS_READONLY_SCOPE in getattr(audit_fn, "_required_google_scopes", [])
        assert SHEETS_WRITE_SCOPE not in getattr(
            audit_fn, "_required_google_scopes", []
        )

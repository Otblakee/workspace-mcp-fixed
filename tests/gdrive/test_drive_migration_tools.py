"""Unit tests for the Drive migration engine.

Acceptance criteria from the gap analysis, exercised here:

* ``walk_drive``: two consecutive walks of a static drive return identical
  rows, and a folder the parent-walk cannot reach is still found via the
  drive-wide sweep;
* ``batch_copy_from_manifest``: every copy is provenance-stamped, and a re-run
  of the same manifest copies nothing;
* ``reconcile_folders``: a clean pilot reports zero blocking discrepancies, and
  native Google files are reported as unverifiable rather than assumed-good;
* ``create_folder_tree``: an existing path is reused, not duplicated;
* ``rebuild_hub``: the hub is diffed against the registry, and orphan removal
  soft-deletes rather than trashing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.utils import UserInputError  # noqa: E402
from gdrive import drive_migration_tools as mig  # noqa: E402

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
USER = "oliver@otbgroup.co.uk"


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


walk_drive = _unwrap(mig.walk_drive)
get_drive_file_metadata = _unwrap(mig.get_drive_file_metadata)
create_folder_tree = _unwrap(mig.create_folder_tree)
batch_copy_from_manifest = _unwrap(mig.batch_copy_from_manifest)
reconcile_folders = _unwrap(mig.reconcile_folders)
rebuild_hub = _unwrap(mig.rebuild_hub)


def _request(result):
    request = MagicMock()
    request.execute.return_value = result
    return request


class FakeTree:
    """Drive double backed by an in-memory tree.

    ``nodes`` maps item id -> dict. ``children`` maps parent id -> [child ids].
    ``unreachable`` lists ids that exist in the drive but that the parent walk
    cannot see — the hidden-folder case the sweep has to catch.
    """

    def __init__(self, nodes, children, *, drive_id=None, unreachable=()):
        self.nodes = nodes
        self.children = children
        self.drive_id = drive_id
        self.unreachable = set(unreachable)
        self.calls = []
        self.created = []
        self.copies = {}
        self.copy_counter = 0
        # id -> appProperties recorded on copies, used for the idempotency check
        self.provenance = {}

    def files(self):
        parent = self

        class _Files:
            def get(self, **kwargs):
                parent.calls.append(("files.get", kwargs))
                node = dict(parent.nodes[kwargs["fileId"]])
                if parent.drive_id:
                    node.setdefault("driveId", parent.drive_id)
                return _request(node)

            def list(self, **kwargs):
                parent.calls.append(("files.list", kwargs))
                return _request({"files": parent._resolve_query(kwargs)})

            def create(self, **kwargs):
                parent.calls.append(("files.create", kwargs))
                body = kwargs["body"]
                new_id = f"new{len(parent.created) + 1}"
                parent.created.append(body)
                node = {"id": new_id, **body}
                parent.nodes[new_id] = node
                for p in body.get("parents", []):
                    parent.children.setdefault(p, []).append(new_id)
                return _request(node)

            def copy(self, **kwargs):
                parent.calls.append(("files.copy", kwargs))
                parent.copy_counter += 1
                new_id = f"copy{parent.copy_counter}"
                body = kwargs["body"]
                parent.provenance[new_id] = {
                    "parents": body.get("parents", []),
                    "appProperties": body.get("appProperties", {}),
                }
                return _request(
                    {
                        "id": new_id,
                        "name": body.get("name"),
                        "appProperties": body.get("appProperties", {}),
                        "md5Checksum": "abc",
                    }
                )

            def update(self, **kwargs):
                parent.calls.append(("files.update", kwargs))
                return _request({"id": kwargs["fileId"]})

            def delete(self, **kwargs):  # pragma: no cover - must never be used
                parent.calls.append(("files.delete", kwargs))
                return _request({})

        return _Files()

    # -- query resolution ------------------------------------------------
    def _resolve_query(self, kwargs):
        query = kwargs.get("q") or ""
        if kwargs.get("corpora") == "drive":
            # Drive-wide sweep: everything in that drive, parents
            # notwithstanding. Nodes may carry an explicit driveId so a single
            # double can host two drives (source and destination).
            wanted_drive = kwargs.get("driveId")
            return [
                dict(node, driveId=node.get("driveId", self.drive_id))
                for node_id, node in self.nodes.items()
                if node_id != wanted_drive
                and node.get("driveId", self.drive_id) == wanted_drive
            ]

        if "appProperties has" in query:
            source_id = query.split("value='")[1].split("'")[0]
            dest = query.split("'")[1]
            return [
                {"id": cid, "name": meta["appProperties"].get("name", cid)}
                for cid, meta in self.provenance.items()
                if meta["appProperties"].get(mig.PROV_SOURCE_ID) == source_id
                and dest in meta["parents"]
            ]

        parent_id = query.split("'")[1]
        child_ids = [
            cid
            for cid in self.children.get(parent_id, [])
            if cid not in self.unreachable
        ]
        results = [dict(self.nodes[cid]) for cid in child_ids]

        if f"mimeType='{SHORTCUT}'" in query:
            results = [r for r in results if r.get("mimeType") == SHORTCUT]
        elif f"mimeType='{FOLDER}'" in query:
            results = [r for r in results if r.get("mimeType") == FOLDER]
        if "name='" in query:
            wanted = query.split("name='")[1].split("'")[0]
            results = [r for r in results if r.get("name") == wanted]
        return results

    def call_names(self):
        return [n for n, _ in self.calls]

    def kwargs_for(self, name):
        return [kw for n, kw in self.calls if n == name]


def _file(fid, name, *, mime="text/plain", md5=None, size=None, parents=()):
    node = {"id": fid, "name": name, "mimeType": mime, "parents": list(parents)}
    if md5:
        node["md5Checksum"] = md5
    if size:
        node["size"] = size
    return node


@pytest.fixture(autouse=True)
def _isolated_attachment_dir(tmp_path, monkeypatch):
    """Keep JSONL reports inside the test's tmp dir."""
    import core.attachment_storage as storage_mod

    monkeypatch.setenv("WORKSPACE_ATTACHMENT_DIR", str(tmp_path))
    monkeypatch.setattr(storage_mod, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(storage_mod, "_attachment_storage", None)
    yield


def _report_rows(tmp_path, stem):
    matches = sorted(tmp_path.glob(f"{stem}*"))
    assert matches, f"no report matching {stem}* in {list(tmp_path.iterdir())}"
    text = matches[-1].read_text().strip()
    return [json.loads(line) for line in text.splitlines() if line]


# ---------------------------------------------------------------------------
# 6. walk_drive
# ---------------------------------------------------------------------------


def _sample_drive(unreachable=()):
    nodes = {
        "D": {"id": "D", "name": "BIR-Projects", "mimeType": FOLDER},
        "f1": _file("f1", "01_Design", mime=FOLDER, parents=["D"]),
        "f2": _file("f2", "02_Build", mime=FOLDER, parents=["D"]),
        "a": _file("a", "plan.pdf", md5="m1", size="10", parents=["f1"]),
        "b": _file("b", "spec.pdf", md5="m2", size="20", parents=["f2"]),
        "hidden": _file("hidden", "99_Hidden", mime=FOLDER, parents=["D"]),
    }
    children = {"D": ["f1", "f2", "hidden"], "f1": ["a"], "f2": ["b"], "hidden": []}
    return FakeTree(nodes, children, drive_id="D", unreachable=unreachable)


class TestWalkDrive:
    @pytest.mark.asyncio
    async def test_two_walks_of_a_static_drive_are_identical(self, tmp_path):
        first = await walk_drive(_sample_drive(), USER, "D")
        rows_first = _report_rows(tmp_path, "walk_BIR-Projects")

        for path in tmp_path.glob("walk_BIR-Projects*"):
            path.unlink()

        second = await walk_drive(_sample_drive(), USER, "D")
        rows_second = _report_rows(tmp_path, "walk_BIR-Projects")

        assert rows_first == rows_second
        assert first.splitlines()[1:4] == second.splitlines()[1:4]

    @pytest.mark.asyncio
    async def test_hidden_folder_is_found_by_the_sweep(self, tmp_path):
        """The parent walk cannot see 'hidden'; the drive-wide sweep must."""
        service = _sample_drive(unreachable=["hidden"])

        result = await walk_drive(service, USER, "D")

        assert "reachable only via the drive-wide sweep" in result
        assert "99_Hidden" in result
        rows = _report_rows(tmp_path, "walk_BIR-Projects")
        hidden = [r for r in rows if r["id"] == "hidden"]
        assert hidden and hidden[0]["discovered_by"] == "sweep"

    @pytest.mark.asyncio
    async def test_clean_self_check_says_so(self):
        result = await walk_drive(_sample_drive(), USER, "D")
        assert "Self-check passed" in result

    @pytest.mark.asyncio
    async def test_manifest_carries_the_documented_fields(self, tmp_path):
        await walk_drive(_sample_drive(), USER, "D")
        rows = _report_rows(tmp_path, "walk_BIR-Projects")
        row = next(r for r in rows if r["id"] == "a")
        for key in (
            "id",
            "name",
            "path",
            "mimeType",
            "size",
            "md5Checksum",
            "owners",
            "modifiedTime",
            "parents",
        ):
            assert key in row
        assert row["path"] == "01_Design/plan.pdf"

    @pytest.mark.asyncio
    async def test_rejects_a_non_folder_root(self):
        service = _sample_drive()
        with pytest.raises(UserInputError, match="not a folder"):
            await walk_drive(service, USER, "a")

    @pytest.mark.asyncio
    async def test_self_check_off_skips_the_sweep(self):
        service = _sample_drive(unreachable=["hidden"])
        result = await walk_drive(service, USER, "D", self_check=False)
        assert "sweep" not in result.lower() or "Self-check" not in result
        assert not any(
            kw.get("corpora") == "drive" for kw in service.kwargs_for("files.list")
        )


# ---------------------------------------------------------------------------
# 8. get_drive_file_metadata
# ---------------------------------------------------------------------------


class TestGetDriveFileMetadata:
    @pytest.mark.asyncio
    async def test_returns_checksums_for_binary_files(self):
        service = _sample_drive()
        result = await get_drive_file_metadata(service, USER, "a")
        assert '"md5Checksum": "m1"' in result
        assert "native Google file" not in result

    @pytest.mark.asyncio
    async def test_flags_native_google_files_as_checksumless(self):
        service = _sample_drive()
        service.nodes["doc"] = _file(
            "doc", "Policy", mime="application/vnd.google-apps.document"
        )
        result = await get_drive_file_metadata(service, USER, "doc")
        assert "no md5/sha checksum" in result

    @pytest.mark.asyncio
    async def test_requires_a_file_id(self):
        with pytest.raises(UserInputError):
            await get_drive_file_metadata(_sample_drive(), USER, "  ")


# ---------------------------------------------------------------------------
# 10. create_folder_tree
# ---------------------------------------------------------------------------


class TestCreateFolderTree:
    @pytest.mark.asyncio
    async def test_creates_nested_paths_once(self):
        nodes = {"root": {"id": "root", "name": "Root", "mimeType": FOLDER}}
        service = FakeTree(nodes, {"root": []})

        with patch(
            "gdrive.drive_migration_tools.resolve_folder_id",
            new_callable=AsyncMock,
            return_value="root",
        ):
            result = await create_folder_tree(
                service,
                USER,
                "root",
                paths=["01_Governance", "01_Governance/01_Policies"],
            )

        created_names = [b["name"] for b in service.created]
        # "01_Governance" is a prefix of the second path and must be created once.
        assert created_names == ["01_Governance", "01_Policies"]
        assert "01_Governance/01_Policies" in result

    @pytest.mark.asyncio
    async def test_existing_path_is_reused(self):
        nodes = {
            "root": {"id": "root", "name": "Root", "mimeType": FOLDER},
            "g": _file("g", "01_Governance", mime=FOLDER, parents=["root"]),
        }
        service = FakeTree(nodes, {"root": ["g"], "g": []})

        with patch(
            "gdrive.drive_migration_tools.resolve_folder_id",
            new_callable=AsyncMock,
            return_value="root",
        ):
            result = await create_folder_tree(
                service, USER, "root", paths=["01_Governance"]
            )

        assert service.created == []
        assert "01_Governance → g" in result

    @pytest.mark.asyncio
    async def test_accepts_the_xlsx_tab_02_manifest_shape(self):
        nodes = {"root": {"id": "root", "name": "Root", "mimeType": FOLDER}}
        service = FakeTree(nodes, {"root": []})
        manifest = json.dumps(
            [
                {"drive": "OTB-Hub", "folder_path": "01_A", "action": "create"},
                {"drive": "OTB-Hub", "folder_path": "02_B", "action": "skip"},
            ]
        )

        with patch(
            "gdrive.drive_migration_tools.resolve_folder_id",
            new_callable=AsyncMock,
            return_value="root",
        ):
            result = await create_folder_tree(
                service, USER, "root", manifest_json=manifest
            )

        assert [b["name"] for b in service.created] == ["01_A"]
        assert "02_B" not in result

    @pytest.mark.asyncio
    async def test_dry_run_creates_nothing(self):
        nodes = {"root": {"id": "root", "name": "Root", "mimeType": FOLDER}}
        service = FakeTree(nodes, {"root": []})

        with patch(
            "gdrive.drive_migration_tools.resolve_folder_id",
            new_callable=AsyncMock,
            return_value="root",
        ):
            result = await create_folder_tree(
                service, USER, "root", paths=["01_A/02_B"], dry_run=True
            )

        assert "DRY RUN" in result
        assert service.created == []

    @pytest.mark.asyncio
    async def test_rejects_paths_and_manifest_together(self):
        with pytest.raises(UserInputError, match="not both"):
            await create_folder_tree(
                FakeTree({}, {}), USER, "root", paths=["a"], manifest_json="[]"
            )


# ---------------------------------------------------------------------------
# 7. batch_copy_from_manifest
# ---------------------------------------------------------------------------


def _copy_service():
    nodes = {
        "s1": _file("s1", "plan.pdf", md5="m1", size="10"),
        "s2": _file("s2", "spec.pdf", md5="m2", size="20"),
        "dest": {"id": "dest", "name": "Dest", "mimeType": FOLDER},
    }
    return FakeTree(nodes, {"dest": []}, drive_id="SRC")


MANIFEST = json.dumps(
    [
        {"source_id": "s1", "dest_folder_id": "dest"},
        {"source_id": "s2", "dest_folder_id": "dest", "new_name": "spec-v2.pdf"},
    ]
)


class TestBatchCopyFromManifest:
    @pytest.mark.asyncio
    async def test_copies_every_row_with_provenance(self, tmp_path):
        service = _copy_service()

        result = await batch_copy_from_manifest(
            service, USER, manifest_json=MANIFEST, migration_batch="BIR-001"
        )

        assert "copied: 2" in result
        assert "unstamped_copies: 0" in result
        for kwargs in service.kwargs_for("files.copy"):
            body = kwargs["body"]
            assert body["appProperties"][mig.PROV_SOURCE_ID] == kwargs["fileId"]
            assert body["appProperties"][mig.PROV_BATCH] == "BIR-001"
            assert body["properties"]["sourceFileId"] == kwargs["fileId"]
            assert body["properties"]["migrationBatch"] == "BIR-001"
            assert body["properties"]["sourceDrive"] == "SRC"
        rows = _report_rows(tmp_path, "copy_results_BIR-001")
        assert {r["status"] for r in rows} == {"copied"}

    @pytest.mark.asyncio
    async def test_rerunning_the_same_manifest_copies_nothing(self):
        service = _copy_service()
        await batch_copy_from_manifest(
            service, USER, manifest_json=MANIFEST, migration_batch="BIR-001"
        )
        first_copies = len(service.kwargs_for("files.copy"))

        result = await batch_copy_from_manifest(
            service, USER, manifest_json=MANIFEST, migration_batch="BIR-001"
        )

        assert len(service.kwargs_for("files.copy")) == first_copies
        assert "skipped_already_copied: 2" in result

    @pytest.mark.asyncio
    async def test_new_name_is_honoured(self):
        service = _copy_service()
        await batch_copy_from_manifest(service, USER, manifest_json=MANIFEST)
        names = [kw["body"]["name"] for kw in service.kwargs_for("files.copy")]
        assert set(names) == {"plan.pdf", "spec-v2.pdf"}

    @pytest.mark.asyncio
    async def test_a_failing_row_does_not_stop_the_run(self, tmp_path):
        service = _copy_service()
        real_files = service.files

        def flaky_files():
            handle = real_files()
            original_copy = handle.copy

            def copy(**kwargs):
                if kwargs["fileId"] == "s1":
                    raise RuntimeError("boom")
                return original_copy(**kwargs)

            handle.copy = copy
            return handle

        service.files = flaky_files

        result = await batch_copy_from_manifest(
            service, USER, manifest_json=MANIFEST, migration_batch="B"
        )

        assert "copied: 1" in result
        assert "failed: 1" in result
        rows = _report_rows(tmp_path, "copy_results_B")
        failed = next(r for r in rows if r["status"] == "failed")
        assert "RuntimeError" in failed["error"]

    @pytest.mark.asyncio
    async def test_dry_run_copies_nothing(self):
        service = _copy_service()
        result = await batch_copy_from_manifest(
            service, USER, manifest_json=MANIFEST, dry_run=True
        )
        assert "DRY RUN" in result
        assert "would_copy: 2" in result
        assert "files.copy" not in service.call_names()

    @pytest.mark.asyncio
    async def test_manifest_missing_required_keys_is_rejected(self):
        with pytest.raises(UserInputError, match="missing"):
            await batch_copy_from_manifest(
                _copy_service(), USER, manifest_json='[{"source_id": "s1"}]'
            )

    @pytest.mark.asyncio
    async def test_max_rows_caps_the_run(self):
        service = _copy_service()
        result = await batch_copy_from_manifest(
            service, USER, manifest_json=MANIFEST, max_rows=1
        )
        assert len(service.kwargs_for("files.copy")) == 1
        assert "Capped at max_rows=1" in result


# ---------------------------------------------------------------------------
# 9. reconcile_folders
# ---------------------------------------------------------------------------


class TestReconcileFolders:
    def _pair(self, dest_files):
        nodes = {
            "src": {"id": "src", "name": "Source", "mimeType": FOLDER},
            "dst": {"id": "dst", "name": "Dest", "mimeType": FOLDER},
            "s_a": _file("s_a", "plan.pdf", md5="m1", size="10", parents=["src"]),
        }
        children = {"src": ["s_a"], "dst": []}
        for node in dest_files:
            nodes[node["id"]] = node
            children["dst"].append(node["id"])
            children.setdefault(node["id"], [])
        return FakeTree(nodes, children)

    @pytest.mark.asyncio
    async def test_clean_pilot_reports_zero_blocking_discrepancies(self):
        service = self._pair(
            [_file("d_a", "plan.pdf", md5="m1", size="10", parents=["dst"])]
        )
        result = await reconcile_folders(service, USER, "src", "dst")
        assert "Reconciliation clean" in result
        assert "matched: 1" in result

    @pytest.mark.asyncio
    async def test_checksum_mismatch_blocks(self, tmp_path):
        service = self._pair(
            [_file("d_a", "plan.pdf", md5="DIFFERENT", size="10", parents=["dst"])]
        )
        result = await reconcile_folders(service, USER, "src", "dst")
        assert "blocking discrepancy" in result
        rows = _report_rows(tmp_path, "reconcile_report")
        assert rows[0]["kind"] == "checksum_mismatch"

    @pytest.mark.asyncio
    async def test_missing_and_extra_are_both_reported(self, tmp_path):
        service = self._pair(
            [_file("d_x", "other.pdf", md5="m9", size="1", parents=["dst"])]
        )
        await reconcile_folders(service, USER, "src", "dst")
        kinds = {r["kind"] for r in _report_rows(tmp_path, "reconcile_report")}
        assert kinds == {"missing_in_dest", "extra_in_dest"}

    @pytest.mark.asyncio
    async def test_native_google_files_are_unverifiable_not_matched(self, tmp_path):
        nodes = {
            "src": {"id": "src", "name": "Source", "mimeType": FOLDER},
            "dst": {"id": "dst", "name": "Dest", "mimeType": FOLDER},
            "s_d": _file(
                "s_d",
                "Policy",
                mime="application/vnd.google-apps.document",
                parents=["src"],
            ),
            "d_d": _file(
                "d_d",
                "Policy",
                mime="application/vnd.google-apps.document",
                parents=["dst"],
            ),
        }
        service = FakeTree(nodes, {"src": ["s_d"], "dst": ["d_d"]})

        result = await reconcile_folders(service, USER, "src", "dst")

        assert "unverifiable: 1" in result
        # Unverifiable is not blocking — the run may proceed, informed.
        assert "Reconciliation clean" in result
        rows = _report_rows(tmp_path, "reconcile_report")
        assert rows[0]["kind"] == "unverifiable_native"

    @pytest.mark.asyncio
    async def test_requires_both_ids(self):
        with pytest.raises(UserInputError):
            await reconcile_folders(FakeTree({}, {}), USER, "src", "")


# ---------------------------------------------------------------------------
# 11. rebuild_hub
# ---------------------------------------------------------------------------


class TestRebuildHub:
    def _hub(self, existing_shortcuts=()):
        nodes = {
            "hub": {"id": "hub", "name": "OTB-Hub", "mimeType": FOLDER},
            "sec": _file("sec", "Premises", mime=FOLDER, parents=["hub"]),
            "t1": _file("t1", "08_Alterations", mime=FOLDER),
            "t2": _file("t2", "09_Leases", mime=FOLDER),
        }
        children = {"hub": ["sec"], "sec": []}
        for shortcut in existing_shortcuts:
            nodes[shortcut["id"]] = shortcut
            children["sec"].append(shortcut["id"])
        return FakeTree(nodes, children)

    def _sheets(self, rows):
        sheets = MagicMock()
        sheets.spreadsheets.return_value.values.return_value.get.return_value = (
            _request({"values": rows})
        )
        return sheets

    @pytest.mark.asyncio
    async def test_creates_missing_shortcuts_from_the_registry(self):
        service = self._hub()
        sheets = self._sheets(
            [
                ["folder_id", "folder_name", "hub_section"],
                ["t1", "08_Alterations", "Premises"],
                ["t2", "09_Leases", "Premises"],
            ]
        )

        with (
            patch.object(
                mig,
                "_hub_registry_service",
                new_callable=AsyncMock,
                return_value=sheets,
            ),
            patch.object(
                mig, "resolve_folder_id", new_callable=AsyncMock, return_value="hub"
            ),
        ):
            result = await rebuild_hub(service, USER, "sheet1", "hub")

        created = [b for b in service.created if b.get("mimeType") == SHORTCUT]
        assert {b["shortcutDetails"]["targetId"] for b in created} == {"t1", "t2"}
        assert "shortcuts_created: 2" in result

    @pytest.mark.asyncio
    async def test_existing_shortcut_is_kept_not_duplicated(self):
        service = self._hub(
            existing_shortcuts=[
                {
                    "id": "sc1",
                    "name": "08_Alterations",
                    "mimeType": SHORTCUT,
                    "parents": ["sec"],
                    "shortcutDetails": {"targetId": "t1"},
                }
            ]
        )
        sheets = self._sheets(
            [
                ["folder_id", "folder_name", "hub_section"],
                ["t1", "08_Alterations", "Premises"],
            ]
        )

        with (
            patch.object(
                mig,
                "_hub_registry_service",
                new_callable=AsyncMock,
                return_value=sheets,
            ),
            patch.object(
                mig, "resolve_folder_id", new_callable=AsyncMock, return_value="hub"
            ),
        ):
            result = await rebuild_hub(service, USER, "sheet1", "hub")

        assert service.created == []
        assert "shortcuts_already_correct: 1" in result

    @pytest.mark.asyncio
    async def test_orphans_are_reported_but_not_removed_by_default(self):
        service = self._hub(
            existing_shortcuts=[
                {
                    "id": "sc9",
                    "name": "Retired",
                    "mimeType": SHORTCUT,
                    "parents": ["sec"],
                    "shortcutDetails": {"targetId": "gone"},
                }
            ]
        )
        sheets = self._sheets(
            [["folder_id", "folder_name", "hub_section"], ["t1", "A", "Premises"]]
        )

        with (
            patch.object(
                mig,
                "_hub_registry_service",
                new_callable=AsyncMock,
                return_value=sheets,
            ),
            patch.object(
                mig, "resolve_folder_id", new_callable=AsyncMock, return_value="hub"
            ),
        ):
            result = await rebuild_hub(service, USER, "sheet1", "hub")

        assert "orphan_shortcuts: 1" in result
        assert "orphans_removed: 0" in result
        assert "files.delete" not in service.call_names()

    @pytest.mark.asyncio
    async def test_orphan_removal_soft_deletes_and_never_trashes(self, monkeypatch):
        monkeypatch.setenv("DRIVE_HOLDING_FOLDER_ID", "holding")
        service = self._hub(
            existing_shortcuts=[
                {
                    "id": "sc9",
                    "name": "Retired",
                    "mimeType": SHORTCUT,
                    "parents": ["sec"],
                    "shortcutDetails": {"targetId": "gone"},
                }
            ]
        )
        sheets = self._sheets(
            [["folder_id", "folder_name", "hub_section"], ["t1", "A", "Premises"]]
        )

        async def fake_resolve(_service, folder_id, **_kwargs):
            return "holding" if folder_id == "holding" else "hub"

        with (
            patch.object(
                mig,
                "_hub_registry_service",
                new_callable=AsyncMock,
                return_value=sheets,
            ),
            patch.object(mig, "resolve_folder_id", side_effect=fake_resolve),
        ):
            result = await rebuild_hub(
                service, USER, "sheet1", "hub", remove_orphans=True
            )

        assert "files.delete" not in service.call_names()
        update = service.kwargs_for("files.update")[0]
        assert update["fileId"] == "sc9"
        assert update["addParents"] == "holding"
        assert update["removeParents"] == "sec"
        assert update["body"]["appProperties"]["mcp_softdeleted"] == "true"
        assert "orphans_removed: 1" in result

    @pytest.mark.asyncio
    async def test_orphan_removal_fails_closed_without_a_holding_folder(
        self, monkeypatch
    ):
        monkeypatch.delenv("DRIVE_HOLDING_FOLDER_ID", raising=False)
        service = self._hub()
        sheets = self._sheets(
            [["folder_id", "folder_name", "hub_section"], ["t1", "A", "Premises"]]
        )

        with patch.object(
            mig, "_hub_registry_service", new_callable=AsyncMock, return_value=sheets
        ):
            with pytest.raises(Exception, match="DRIVE_HOLDING_FOLDER_ID"):
                await rebuild_hub(service, USER, "sheet1", "hub", remove_orphans=True)

    @pytest.mark.asyncio
    async def test_dry_run_touches_nothing(self):
        service = self._hub()
        sheets = self._sheets(
            [["folder_id", "folder_name", "hub_section"], ["t1", "A", "Premises"]]
        )

        with (
            patch.object(
                mig,
                "_hub_registry_service",
                new_callable=AsyncMock,
                return_value=sheets,
            ),
            patch.object(
                mig, "resolve_folder_id", new_callable=AsyncMock, return_value="hub"
            ),
        ):
            result = await rebuild_hub(service, USER, "sheet1", "hub", dry_run=True)

        assert "DRY RUN" in result
        assert service.created == []

    @pytest.mark.asyncio
    async def test_registry_without_hub_section_column_is_rejected(self):
        service = self._hub()
        sheets = self._sheets([["folder_id", "folder_name"], ["t1", "A"]])

        with patch.object(
            mig, "_hub_registry_service", new_callable=AsyncMock, return_value=sheets
        ):
            with pytest.raises(UserInputError, match="hub_section"):
                await rebuild_hub(service, USER, "sheet1", "hub")


# ---------------------------------------------------------------------------
# Review-round regressions (Codex review of ebcd25d)
# ---------------------------------------------------------------------------


class TestSweepCompleteness:
    def _drive_with_hidden_subtree(self):
        """'hidden' is unreachable from the parent walk but holds a child."""
        nodes = {
            "D": {"id": "D", "name": "BIR-Projects", "mimeType": FOLDER},
            "f1": _file("f1", "01_Design", mime=FOLDER, parents=["D"]),
            "hidden": _file("hidden", "99_Hidden", mime=FOLDER, parents=["D"]),
            "buried": _file(
                "buried", "buried.pdf", md5="m9", size="9", parents=["hidden"]
            ),
        }
        children = {"D": ["f1", "hidden"], "f1": [], "hidden": ["buried"]}
        return FakeTree(nodes, children, drive_id="D", unreachable=["hidden"])

    @pytest.mark.asyncio
    async def test_sweep_only_subtree_keeps_its_shape(self, tmp_path):
        """A child of a sweep-only folder must resolve through its ancestor,
        not collapse to <unreached>/child — otherwise unrelated subtrees
        flatten onto colliding paths and path-keyed reconciliation breaks."""
        await walk_drive(self._drive_with_hidden_subtree(), USER, "D")

        rows = {r["id"]: r for r in _report_rows(tmp_path, "walk_BIR-Projects")}
        # 'hidden' hangs directly off the root, so its path resolves fully.
        assert rows["hidden"]["path"] == "99_Hidden"
        # The point of the fix: 'buried' resolves *through* its sweep-only
        # ancestor rather than collapsing to a bare name at an unknown depth.
        assert rows["buried"]["path"] == "99_Hidden/buried.pdf"
        assert rows["buried"]["discovered_by"] == "sweep"

    @pytest.mark.asyncio
    async def test_unresolvable_ancestor_falls_back_to_unreached(self, tmp_path):
        """When the chain runs off the end of the swept corpus we say so
        rather than inventing a path."""
        nodes = {
            "D": {"id": "D", "name": "BIR-Projects", "mimeType": FOLDER},
            "f1": _file("f1", "01_Design", mime=FOLDER, parents=["D"]),
            "waif": _file("waif", "waif.pdf", md5="m4", parents=["vanished"]),
        }
        service = FakeTree(
            nodes, {"D": ["f1"], "f1": []}, drive_id="D", unreachable=["waif"]
        )

        await walk_drive(service, USER, "D")

        rows = {r["id"]: r for r in _report_rows(tmp_path, "walk_BIR-Projects")}
        assert rows["waif"]["path"] == "<unreached>/waif.pdf"

    @pytest.mark.asyncio
    async def test_sweep_honours_max_items(self, tmp_path):
        """The cap is a safety limit on the whole inventory, so the sweep
        cannot append past it after the walk has already filled up."""
        service = self._drive_with_hidden_subtree()

        result = await walk_drive(service, USER, "D", max_items=2)

        rows = _report_rows(tmp_path, "walk_BIR-Projects")
        assert len(rows) <= 2
        assert "truncated: 1" in result

    @pytest.mark.asyncio
    async def test_sweep_truncation_is_deterministic(self, tmp_path):
        first = await walk_drive(
            self._drive_with_hidden_subtree(), USER, "D", max_items=3
        )
        rows_first = _report_rows(tmp_path, "walk_BIR-Projects")
        for path in tmp_path.glob("walk_BIR-Projects*"):
            path.unlink()
        second = await walk_drive(
            self._drive_with_hidden_subtree(), USER, "D", max_items=3
        )
        assert rows_first == _report_rows(tmp_path, "walk_BIR-Projects")
        assert first.splitlines()[1:4] == second.splitlines()[1:4]


class TestReconcileUsesTheSweep:
    def _two_drives(self, *, unreachable=()):
        """Source and destination shared drives in one double."""
        nodes = {
            "src": {
                "id": "src",
                "name": "Source",
                "mimeType": FOLDER,
                "driveId": "src",
            },
            "dst": {"id": "dst", "name": "Dest", "mimeType": FOLDER, "driveId": "dst"},
            "s_a": dict(
                _file("s_a", "plan.pdf", md5="m1", size="10", parents=["src"]),
                driveId="src",
            ),
            "s_lost": dict(
                _file("s_lost", "lost.pdf", md5="m2", size="20", parents=["src"]),
                driveId="src",
            ),
            "d_a": dict(
                _file("d_a", "plan.pdf", md5="m1", size="10", parents=["dst"]),
                driveId="dst",
            ),
        }
        children = {"src": ["s_a", "s_lost"], "dst": ["d_a"]}
        return FakeTree(nodes, children, unreachable=unreachable)

    @pytest.mark.asyncio
    async def test_source_item_unreachable_by_walk_still_blocks(self, tmp_path):
        """An item missing from the destination AND unreachable from the
        source's parent graph must not vanish from both sides and yield a
        clean verdict — that would sign off on lost content."""
        service = self._two_drives(unreachable=["s_lost"])

        result = await reconcile_folders(service, USER, "src", "dst")

        assert "blocking discrepancy" in result
        kinds = {r["kind"] for r in _report_rows(tmp_path, "reconcile_report")}
        assert "missing_in_dest" in kinds
        assert "sweep contributed" in result

    @pytest.mark.asyncio
    async def test_fully_migrated_pair_is_still_clean(self):
        nodes = {
            "src": {"id": "src", "name": "S", "mimeType": FOLDER, "driveId": "src"},
            "dst": {"id": "dst", "name": "D", "mimeType": FOLDER, "driveId": "dst"},
            "s_a": dict(
                _file("s_a", "plan.pdf", md5="m1", size="10", parents=["src"]),
                driveId="src",
            ),
            "d_a": dict(
                _file("d_a", "plan.pdf", md5="m1", size="10", parents=["dst"]),
                driveId="dst",
            ),
        }
        service = FakeTree(nodes, {"src": ["s_a"], "dst": ["d_a"]})

        result = await reconcile_folders(service, USER, "src", "dst")

        assert "Reconciliation clean" in result


class TestReconcileDuplicateSiblings:
    @pytest.mark.asyncio
    async def test_two_sources_one_dest_is_not_clean(self, tmp_path):
        """Drive allows same-named siblings. Collapsing them by path would
        report a clean migration while one file was never copied."""
        nodes = {
            "src": {"id": "src", "name": "S", "mimeType": FOLDER},
            "dst": {"id": "dst", "name": "D", "mimeType": FOLDER},
            "s1": _file("s1", "foo.pdf", md5="m1", size="10", parents=["src"]),
            "s2": _file("s2", "foo.pdf", md5="m2", size="20", parents=["src"]),
            "d1": _file("d1", "foo.pdf", md5="m1", size="10", parents=["dst"]),
        }
        service = FakeTree(nodes, {"src": ["s1", "s2"], "dst": ["d1"]})

        result = await reconcile_folders(service, USER, "src", "dst")

        assert "blocking discrepancy" in result
        rows = _report_rows(tmp_path, "reconcile_report")
        missing = [r for r in rows if r["kind"] == "missing_in_dest"]
        assert len(missing) == 1
        assert missing[0]["source_id"] == "s2"

    @pytest.mark.asyncio
    async def test_matching_duplicate_siblings_are_clean(self):
        nodes = {
            "src": {"id": "src", "name": "S", "mimeType": FOLDER},
            "dst": {"id": "dst", "name": "D", "mimeType": FOLDER},
            "s1": _file("s1", "foo.pdf", md5="m1", size="10", parents=["src"]),
            "s2": _file("s2", "foo.pdf", md5="m2", size="20", parents=["src"]),
            "d1": _file("d1", "foo.pdf", md5="m1", size="10", parents=["dst"]),
            "d2": _file("d2", "foo.pdf", md5="m2", size="20", parents=["dst"]),
        }
        service = FakeTree(nodes, {"src": ["s1", "s2"], "dst": ["d1", "d2"]})

        result = await reconcile_folders(service, USER, "src", "dst")

        assert "Reconciliation clean" in result
        assert "matched: 2" in result


class TestFolderTreeMultiDrive:
    MULTI = json.dumps(
        [
            {"drive": "OTB-Hub", "folder_path": "01_A", "action": "create"},
            {"drive": "OTB-Knowledge Base", "folder_path": "02_B", "action": "create"},
        ]
    )

    def _root(self):
        nodes = {"root": {"id": "root", "name": "Root", "mimeType": FOLDER}}
        return FakeTree(nodes, {"root": []})

    @pytest.mark.asyncio
    async def test_multi_drive_manifest_is_refused_without_a_selector(self):
        """root_id names one drive; building every drive's folders under it
        would be silent and expensive to unpick."""
        with pytest.raises(UserInputError, match="spans multiple drives"):
            await create_folder_tree(
                self._root(), USER, "root", manifest_json=self.MULTI
            )

    @pytest.mark.asyncio
    async def test_drive_selector_filters_the_manifest(self):
        service = self._root()

        with patch(
            "gdrive.drive_migration_tools.resolve_folder_id",
            new_callable=AsyncMock,
            return_value="root",
        ):
            result = await create_folder_tree(
                service, USER, "root", manifest_json=self.MULTI, drive="OTB-Hub"
            )

        assert [b["name"] for b in service.created] == ["01_A"]
        assert "02_B" not in result

    @pytest.mark.asyncio
    async def test_unknown_drive_selector_is_rejected(self):
        with pytest.raises(UserInputError, match="No manifest rows for drive"):
            await create_folder_tree(
                self._root(), USER, "root", manifest_json=self.MULTI, drive="Nope"
            )

    @pytest.mark.asyncio
    async def test_single_drive_manifest_needs_no_selector(self):
        service = self._root()
        single = json.dumps([{"drive": "OTB-Hub", "folder_path": "01_A"}])

        with patch(
            "gdrive.drive_migration_tools.resolve_folder_id",
            new_callable=AsyncMock,
            return_value="root",
        ):
            await create_folder_tree(service, USER, "root", manifest_json=single)

        assert [b["name"] for b in service.created] == ["01_A"]


class TestFolderTreePartialFailure:
    @pytest.mark.asyncio
    async def test_one_failing_path_does_not_abort_the_batch(self):
        """A resumable batch must not need a manual manifest edit to make
        progress past one unwritable location."""
        nodes = {"root": {"id": "root", "name": "Root", "mimeType": FOLDER}}
        service = FakeTree(nodes, {"root": []})
        real_files = service.files

        def flaky_files():
            handle = real_files()
            original_create = handle.create

            def create(**kwargs):
                if kwargs["body"].get("name") == "02_Bad":
                    raise RuntimeError("insufficientFilePermissions")
                return original_create(**kwargs)

            handle.create = create
            return handle

        service.files = flaky_files

        with patch(
            "gdrive.drive_migration_tools.resolve_folder_id",
            new_callable=AsyncMock,
            return_value="root",
        ):
            result = await create_folder_tree(
                service, USER, "root", paths=["01_Good", "02_Bad", "03_Also_Good"]
            )

        created = [b["name"] for b in service.created]
        assert "01_Good" in created and "03_Also_Good" in created
        assert "paths_failed: 1" in result
        assert "RuntimeError" in result


class TestBatchCopyDeduplication:
    @pytest.mark.asyncio
    async def test_repeated_key_in_one_manifest_copies_once(self, tmp_path):
        """The provenance pre-check cannot see a copy that does not exist yet,
        so a concatenated manifest naming the same pair twice must be collapsed
        before any work starts."""
        service = _copy_service()
        manifest = json.dumps(
            [
                {"source_id": "s1", "dest_folder_id": "dest"},
                {"source_id": "s1", "dest_folder_id": "dest"},
                {"source_id": "s2", "dest_folder_id": "dest"},
            ]
        )

        result = await batch_copy_from_manifest(
            service, USER, manifest_json=manifest, migration_batch="DEDUP"
        )

        copied_sources = [kw["fileId"] for kw in service.kwargs_for("files.copy")]
        assert sorted(copied_sources) == ["s1", "s2"]
        assert "duplicate_rows_collapsed: 1" in result
        rows = _report_rows(tmp_path, "copy_results_DEDUP")
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_same_source_to_two_destinations_is_not_deduped(self):
        service = _copy_service()
        service.nodes["dest2"] = {"id": "dest2", "name": "D2", "mimeType": FOLDER}
        service.children["dest2"] = []
        manifest = json.dumps(
            [
                {"source_id": "s1", "dest_folder_id": "dest"},
                {"source_id": "s1", "dest_folder_id": "dest2"},
            ]
        )

        await batch_copy_from_manifest(service, USER, manifest_json=manifest)

        assert len(service.kwargs_for("files.copy")) == 2


class TestRebuildHubFullDiff:
    def _hub_with(self, section_folders, shortcuts):
        nodes = {"hub": {"id": "hub", "name": "OTB-Hub", "mimeType": FOLDER}}
        children = {"hub": []}
        for section_id, name in section_folders:
            nodes[section_id] = _file(section_id, name, mime=FOLDER, parents=["hub"])
            children["hub"].append(section_id)
            children[section_id] = []
        for shortcut in shortcuts:
            nodes[shortcut["id"]] = shortcut
            children[shortcut["parents"][0]].append(shortcut["id"])
        nodes.setdefault("t1", _file("t1", "08_Alterations", mime=FOLDER))
        return FakeTree(nodes, children)

    def _sheets(self, rows):
        sheets = MagicMock()
        sheets.spreadsheets.return_value.values.return_value.get.return_value = (
            _request({"values": rows})
        )
        return sheets

    def _shortcut(self, sid, name, parent, target):
        return {
            "id": sid,
            "name": name,
            "mimeType": SHORTCUT,
            "parents": [parent],
            "shortcutDetails": {"targetId": target},
        }

    @pytest.mark.asyncio
    async def test_duplicate_shortcuts_to_one_target_are_orphans(self):
        """One shortcut per target per section is the promised state, so the
        extras must show up in the diff."""
        service = self._hub_with(
            [("sec", "Premises")],
            [
                self._shortcut("sc1", "08_Alterations", "sec", "t1"),
                self._shortcut("sc2", "08_Alterations (copy)", "sec", "t1"),
            ],
        )
        sheets = self._sheets(
            [
                ["folder_id", "folder_name", "hub_section"],
                ["t1", "08_Alterations", "Premises"],
            ]
        )

        with (
            patch.object(
                mig,
                "_hub_registry_service",
                new_callable=AsyncMock,
                return_value=sheets,
            ),
            patch.object(
                mig, "resolve_folder_id", new_callable=AsyncMock, return_value="hub"
            ),
        ):
            result = await rebuild_hub(service, USER, "sheet1", "hub")

        assert "shortcuts_already_correct: 1" in result
        assert "orphan_shortcuts: 1" in result
        assert "duplicate shortcut" in result

    @pytest.mark.asyncio
    async def test_section_dropped_from_registry_is_scanned(self):
        """Removing a section's last registry row used to strand its whole
        shortcut set: never reported, never removed."""
        service = self._hub_with(
            [("sec", "Premises"), ("old", "Retired Section")],
            [self._shortcut("sc9", "Stale", "old", "gone")],
        )
        sheets = self._sheets(
            [
                ["folder_id", "folder_name", "hub_section"],
                ["t1", "08_Alterations", "Premises"],
            ]
        )

        with (
            patch.object(
                mig,
                "_hub_registry_service",
                new_callable=AsyncMock,
                return_value=sheets,
            ),
            patch.object(
                mig, "resolve_folder_id", new_callable=AsyncMock, return_value="hub"
            ),
        ):
            result = await rebuild_hub(service, USER, "sheet1", "hub")

        assert "stale_sections_scanned: 1" in result
        assert "orphan_shortcuts: 1" in result
        assert "section no longer in registry" in result

    @pytest.mark.asyncio
    async def test_empty_registry_still_reports_the_whole_hub(self):
        """An empty registry means every shortcut in the hub is an orphan —
        returning early would leave exactly the drift this tool prevents."""
        service = self._hub_with(
            [("sec", "Premises")],
            [self._shortcut("sc1", "Stale", "sec", "t1")],
        )
        sheets = self._sheets(
            [["folder_id", "folder_name", "hub_section"], ["t1", "A", ""]]
        )

        with (
            patch.object(
                mig,
                "_hub_registry_service",
                new_callable=AsyncMock,
                return_value=sheets,
            ),
            patch.object(
                mig, "resolve_folder_id", new_callable=AsyncMock, return_value="hub"
            ),
        ):
            result = await rebuild_hub(service, USER, "sheet1", "hub")

        assert "orphan_shortcuts: 1" in result

    @pytest.mark.asyncio
    async def test_stale_section_orphans_soft_delete_not_trash(self, monkeypatch):
        monkeypatch.setenv("DRIVE_HOLDING_FOLDER_ID", "holding")
        service = self._hub_with(
            [("sec", "Premises"), ("old", "Retired")],
            [self._shortcut("sc9", "Stale", "old", "gone")],
        )
        sheets = self._sheets(
            [
                ["folder_id", "folder_name", "hub_section"],
                ["t1", "08_Alterations", "Premises"],
            ]
        )

        async def fake_resolve(_service, folder_id, **_kwargs):
            return "holding" if folder_id == "holding" else "hub"

        with (
            patch.object(
                mig,
                "_hub_registry_service",
                new_callable=AsyncMock,
                return_value=sheets,
            ),
            patch.object(mig, "resolve_folder_id", side_effect=fake_resolve),
        ):
            result = await rebuild_hub(
                service, USER, "sheet1", "hub", remove_orphans=True
            )

        assert "files.delete" not in service.call_names()
        update = next(
            kw for kw in service.kwargs_for("files.update") if kw["fileId"] == "sc9"
        )
        assert update["addParents"] == "holding"
        assert update["removeParents"] == "old"
        assert "orphans_removed: 1" in result


class TestRebuildHubSheetsScope:
    @pytest.mark.asyncio
    async def test_missing_sheets_scope_gives_actionable_error(self):
        """With only the drive service enabled, the consent flow never
        requested a Sheets scope; the failure must say so."""
        nodes = {"hub": {"id": "hub", "name": "OTB-Hub", "mimeType": FOLDER}}
        service = FakeTree(nodes, {"hub": []})

        async def boom(**_kwargs):
            raise Exception("insufficient authentication scopes")

        with patch.object(mig, "_hub_registry_service", side_effect=boom):
            with pytest.raises(UserInputError, match="'sheets' service"):
                await rebuild_hub(service, USER, "sheet1", "hub")


class TestReportingAccuracy:
    """Second review pass: the summary must not misreport what happened."""

    @pytest.mark.asyncio
    async def test_max_rows_notice_survives_deduplication(self):
        """The cap notice is recorded when the cap bites, not inferred from
        the final row count — dedup runs afterwards and can shrink the list
        back below max_rows, suppressing the notice."""
        service = _copy_service()
        manifest = json.dumps(
            [
                {"source_id": "s1", "dest_folder_id": "dest"},
                {"source_id": "s1", "dest_folder_id": "dest"},
                {"source_id": "s2", "dest_folder_id": "dest"},
            ]
        )

        result = await batch_copy_from_manifest(
            service, USER, manifest_json=manifest, max_rows=2
        )

        # 2 rows survive the cap, dedup collapses them to 1 — the notice must
        # still appear because a row really was dropped by the cap.
        assert "Capped at max_rows=2" in result

    @pytest.mark.asyncio
    async def test_truncated_sweep_does_not_claim_a_clean_self_check(self):
        """A truncated sweep that happened to find nothing extra proves
        nothing, so it must not print the pass line."""
        nodes = {
            "D": {"id": "D", "name": "Big", "mimeType": FOLDER},
            "f1": _file("f1", "a.pdf", md5="m1", parents=["D"]),
            "f2": _file("f2", "b.pdf", md5="m2", parents=["D"]),
            "f3": _file("f3", "c.pdf", md5="m3", parents=["D"]),
        }
        service = FakeTree(nodes, {"D": ["f1", "f2", "f3"]}, drive_id="D")

        result = await walk_drive(service, USER, "D", max_items=2)

        assert "Self-check passed" not in result
        assert "truncated: 1" in result

    @pytest.mark.asyncio
    async def test_drive_selector_is_ignored_when_manifest_has_no_drive_column(self):
        """Filtering on an absent column would discard every row and report
        the misleading 'no folder paths to create'."""
        nodes = {"root": {"id": "root", "name": "Root", "mimeType": FOLDER}}
        service = FakeTree(nodes, {"root": []})
        manifest = json.dumps([{"folder_path": "01_A"}, {"folder_path": "02_B"}])

        with patch(
            "gdrive.drive_migration_tools.resolve_folder_id",
            new_callable=AsyncMock,
            return_value="root",
        ):
            result = await create_folder_tree(
                service, USER, "root", manifest_json=manifest, drive="OTB-Hub"
            )

        assert [b["name"] for b in service.created] == ["01_A", "02_B"]
        assert "01_A" in result


class TestTruncatedReconciliationIsInconclusive:
    @pytest.mark.asyncio
    async def test_truncated_run_is_not_reported_clean(self, tmp_path):
        """A truncated inventory compared only a prefix. Whatever that prefix
        showed, the run proves nothing about the items never examined, so it
        must not yield a green verdict automation could act on."""
        nodes = {
            "src": {"id": "src", "name": "S", "mimeType": FOLDER},
            "dst": {"id": "dst", "name": "D", "mimeType": FOLDER},
        }
        children = {"src": [], "dst": []}
        for i in range(4):
            nodes[f"s{i}"] = _file(
                f"s{i}", f"f{i}.pdf", md5=f"m{i}", size="1", parents=["src"]
            )
            nodes[f"d{i}"] = _file(
                f"d{i}", f"f{i}.pdf", md5=f"m{i}", size="1", parents=["dst"]
            )
            children["src"].append(f"s{i}")
            children["dst"].append(f"d{i}")
        service = FakeTree(nodes, children)

        result = await reconcile_folders(service, USER, "src", "dst", max_items=2)

        assert "Inconclusive" in result
        assert "Reconciliation clean" not in result
        rows = _report_rows(tmp_path, "reconcile_report")
        kinds = {r["kind"] for r in rows}
        assert "inconclusive_truncated" in kinds
        # The clean marker must never appear alongside a truncated run.
        assert "clean" not in kinds


class TestShortcutReconciliation:
    def _pair(self, source_target, dest_target):
        nodes = {
            "src": {"id": "src", "name": "S", "mimeType": FOLDER},
            "dst": {"id": "dst", "name": "D", "mimeType": FOLDER},
            "s_sc": dict(
                _file("s_sc", "Link", mime=SHORTCUT, parents=["src"]),
                shortcutDetails={"targetId": source_target},
            ),
            "d_sc": dict(
                _file("d_sc", "Link", mime=SHORTCUT, parents=["dst"]),
                shortcutDetails={"targetId": dest_target},
            ),
        }
        return FakeTree(nodes, {"src": ["s_sc"], "dst": ["d_sc"]})

    @pytest.mark.asyncio
    async def test_matching_targets_count_as_matched(self):
        result = await reconcile_folders(self._pair("t1", "t1"), USER, "src", "dst")
        assert "matched: 1" in result
        assert "Reconciliation clean" in result

    @pytest.mark.asyncio
    async def test_differing_targets_are_reported_not_dismissed_as_native(
        self, tmp_path
    ):
        """A shortcut's mimeType is under the google-apps namespace, so it used
        to be written off as an unmeasurable native document even though the
        inventory records its target."""
        result = await reconcile_folders(self._pair("t1", "t9"), USER, "src", "dst")

        rows = _report_rows(tmp_path, "reconcile_report")
        kinds = {r["kind"] for r in rows}
        assert kinds == {"shortcut_target_differs"}
        assert "unverifiable_native" not in kinds
        row = rows[0]
        assert row["source_target_id"] == "t1"
        assert row["dest_target_id"] == "t9"
        # Non-blocking: a copy-based migration legitimately re-points targets.
        assert "Reconciliation clean" in result
        assert "point at different targets" in result


class TestHubLabelDrift:
    @pytest.mark.asyncio
    async def test_registry_label_change_renames_the_shortcut(self):
        """The registry is the source of truth for the label too; a renamed
        row otherwise leaves the hub showing the old navigation label."""
        nodes = {
            "hub": {"id": "hub", "name": "OTB-Hub", "mimeType": FOLDER},
            "sec": _file("sec", "Premises", mime=FOLDER, parents=["hub"]),
            "t1": _file("t1", "08_Alterations", mime=FOLDER),
            "sc1": {
                "id": "sc1",
                "name": "08_Alterations (old label)",
                "mimeType": SHORTCUT,
                "parents": ["sec"],
                "shortcutDetails": {"targetId": "t1"},
            },
        }
        service = FakeTree(nodes, {"hub": ["sec"], "sec": ["sc1"]})
        sheets = MagicMock()
        sheets.spreadsheets.return_value.values.return_value.get.return_value = (
            _request(
                {
                    "values": [
                        ["folder_id", "folder_name", "hub_section"],
                        ["t1", "08_Alterations", "Premises"],
                    ]
                }
            )
        )

        with (
            patch.object(
                mig,
                "_hub_registry_service",
                new_callable=AsyncMock,
                return_value=sheets,
            ),
            patch.object(
                mig, "resolve_folder_id", new_callable=AsyncMock, return_value="hub"
            ),
        ):
            result = await rebuild_hub(service, USER, "sheet1", "hub")

        update = service.kwargs_for("files.update")[0]
        assert update["fileId"] == "sc1"
        assert update["body"]["name"] == "08_Alterations"
        assert "shortcuts_renamed: 1" in result
        assert "orphan_shortcuts: 0" in result

    @pytest.mark.asyncio
    async def test_matching_label_is_left_alone(self):
        nodes = {
            "hub": {"id": "hub", "name": "OTB-Hub", "mimeType": FOLDER},
            "sec": _file("sec", "Premises", mime=FOLDER, parents=["hub"]),
            "t1": _file("t1", "08_Alterations", mime=FOLDER),
            "sc1": {
                "id": "sc1",
                "name": "08_Alterations",
                "mimeType": SHORTCUT,
                "parents": ["sec"],
                "shortcutDetails": {"targetId": "t1"},
            },
        }
        service = FakeTree(nodes, {"hub": ["sec"], "sec": ["sc1"]})
        sheets = MagicMock()
        sheets.spreadsheets.return_value.values.return_value.get.return_value = (
            _request(
                {
                    "values": [
                        ["folder_id", "folder_name", "hub_section"],
                        ["t1", "08_Alterations", "Premises"],
                    ]
                }
            )
        )

        with (
            patch.object(
                mig,
                "_hub_registry_service",
                new_callable=AsyncMock,
                return_value=sheets,
            ),
            patch.object(
                mig, "resolve_folder_id", new_callable=AsyncMock, return_value="hub"
            ),
        ):
            result = await rebuild_hub(service, USER, "sheet1", "hub")

        assert "files.update" not in service.call_names()
        assert "shortcuts_already_correct: 1" in result


class TestNoHardDeleteAnywhere:
    def test_migration_module_never_calls_files_delete(self):
        """The soft-delete invariant: this server never trashes or hard-deletes
        a Drive file, and a shortcut is a Drive file."""
        source = (
            Path(__file__).resolve().parent.parent.parent
            / "gdrive"
            / "drive_migration_tools.py"
        ).read_text()
        assert ".delete(" not in source
        assert '"trashed": True' not in source

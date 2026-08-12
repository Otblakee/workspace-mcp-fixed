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
            # Drive-wide sweep: everything, parents notwithstanding.
            return [
                dict(node, driveId=self.drive_id)
                for node_id, node in self.nodes.items()
                if node_id != self.drive_id
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

        with patch.object(
            mig, "_hub_registry_service", new_callable=AsyncMock, return_value=sheets
        ), patch.object(
            mig, "resolve_folder_id", new_callable=AsyncMock, return_value="hub"
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

        with patch.object(
            mig, "_hub_registry_service", new_callable=AsyncMock, return_value=sheets
        ), patch.object(
            mig, "resolve_folder_id", new_callable=AsyncMock, return_value="hub"
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

        with patch.object(
            mig, "_hub_registry_service", new_callable=AsyncMock, return_value=sheets
        ), patch.object(
            mig, "resolve_folder_id", new_callable=AsyncMock, return_value="hub"
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

        with patch.object(
            mig, "_hub_registry_service", new_callable=AsyncMock, return_value=sheets
        ), patch.object(mig, "resolve_folder_id", side_effect=fake_resolve):
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
                await rebuild_hub(
                    service, USER, "sheet1", "hub", remove_orphans=True
                )

    @pytest.mark.asyncio
    async def test_dry_run_touches_nothing(self):
        service = self._hub()
        sheets = self._sheets(
            [["folder_id", "folder_name", "hub_section"], ["t1", "A", "Premises"]]
        )

        with patch.object(
            mig, "_hub_registry_service", new_callable=AsyncMock, return_value=sheets
        ), patch.object(
            mig, "resolve_folder_id", new_callable=AsyncMock, return_value="hub"
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

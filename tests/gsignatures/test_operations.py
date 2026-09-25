"""Unit tests for ``gsignatures.operations`` (the transport-agnostic layer).

Covers:

* ``plan_user`` wiring: Directory read, Gmail client for the primary address,
  send-as list, engine plan;
* ``apply_user``: dry run by default with no patch; a live run refused
  without ``confirm`` before any Gmail or ledger call; the confirm path
  patches, reads back and appends a ledger row whose ``readback_hash`` is the
  hash of the read-back and whose ``previous_signature_html`` is the old
  signature; unchanged detection against the ledger; ``force``;
  ``include_aliases=False``; one failing send-as does not stop the others;
  a live run refused when the ledger is unreadable, before any write;
* ``apply_scope``: exactly one scope, ``max_users`` refusal without
  truncation, the group path, per-user isolation, the JSONL report;
* ``audit_scope``: every drift status, exactly one scope, ``all_users``,
  per-user isolation.

No Google client is ever built: every service is a fake from ``fakes.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.utils import UserInputError  # noqa: E402
from gsignatures import operations  # noqa: E402
from gsignatures.engine import load_config, signature_hash  # noqa: E402
from gsignatures.ledger import AUDIT_HEADER, LEDGER_HEADER, LedgerError  # noqa: E402
from tests.gsignatures.fakes import (  # noqa: E402
    FakeDirectory,
    FakeGmailPool,
    FakeSheets,
    http_error,
    send_as,
    user,
)

SHEET_ID = "ledger-sheet"
ACTOR = "oliver@otbgroup.co.uk"
RUN = "run0001"

ALICE = "alice@otbgroup.co.uk"
ALICE_JIT = "alice@jit-logistics.com"
ALICE_HOME = "alice@blakefamily.uk"
BOB = "bob@jit-logistics.com"
CAROL = "carol@otbgroup.co.uk"
DAVE = "dave@valeautomotive.co.uk"

OLD_PRIMARY = "<div>old primary<!-- c --></div>"
OLD_ALIAS = "<div>old alias</div>"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Retries on injected 5xx errors must not sleep for real."""
    monkeypatch.setattr("gdrive.drive_batch.asyncio.sleep", AsyncMock())


@pytest.fixture(autouse=True)
def _isolated_attachment_dir(tmp_path, monkeypatch):
    """Keep JSONL reports inside the test's tmp dir."""
    import core.attachment_storage as storage_mod

    monkeypatch.setenv("WORKSPACE_ATTACHMENT_DIR", str(tmp_path))
    monkeypatch.setattr(storage_mod, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(storage_mod, "_attachment_storage", None)
    yield


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture
def directory():
    return FakeDirectory(
        users=[
            user(ALICE, "/01 OTB/Exec", full="Alice Able", title="Director"),
            user(BOB, "/02 JIT", full="Bob Baker", title="Operations Manager"),
            # No title: the engine reports an error row for every address.
            user(CAROL, "/01 OTB", full="Carol Cole", title=None),
            user(DAVE, "/03 VALE", full="Dave Dunn", title="Technician"),
        ],
        groups={
            "leads@otbgroup.co.uk": [
                {"email": ALICE, "type": "USER"},
                {"email": BOB, "type": "USER"},
                {"email": "nested@otbgroup.co.uk", "type": "GROUP"},
            ]
        },
    )


@pytest.fixture
def pool():
    pool = FakeGmailPool()
    pool.add(
        ALICE,
        [
            send_as(ALICE, primary=True, signature=OLD_PRIMARY),
            send_as(ALICE_JIT, signature=OLD_ALIAS),
            send_as(ALICE_HOME, signature=""),
        ],
    )
    pool.add(BOB, [send_as(BOB, primary=True, signature="")])
    pool.add(CAROL, [send_as(CAROL, primary=True, signature="")])
    pool.add(DAVE, [send_as(DAVE, primary=True, signature="")])
    return pool


def _rows_by_send_as(rows):
    return {r.send_as_email: r for r in rows}


async def _apply_alice(config, directory, pool, **kw):
    kw.setdefault("actor", ACTOR)
    kw.setdefault("run_id", RUN)
    kw.setdefault("gmail_factory", pool.factory)
    return await operations.apply_user(config, directory, ALICE, **kw)


def _ledger_with_rows(rows):
    return FakeSheets({"Ledger": [LEDGER_HEADER] + rows})


async def _ledger_row_for_current_state(config, directory, pool, send_as_email):
    """A ledger row that says the current Gmail signature was our last apply."""
    _, send_as_list, planned = await operations.plan_user(
        config, directory, ALICE, gmail_factory=pool.factory
    )
    plan = next(p for p in planned if p.send_as_email == send_as_email)
    current = next(s for s in send_as_list if s["sendAsEmail"] == send_as_email)
    return [
        "2026-09-20T09:00:00+00:00",
        ACTOR,
        ALICE,
        send_as_email,
        plan.entity,
        plan.template_version,
        plan.statutory_version,
        plan.rendered_hash,
        signature_hash(current["signature"]),
        "",
        "<div>even older</div>",
        "run-old",
    ]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_run_id_and_timestamp_shape(self):
        run_id = operations.new_run_id()
        assert len(run_id) == 10
        assert operations.new_run_id() != run_id
        stamp = operations.utc_now_iso()
        assert stamp.endswith("+00:00")
        assert len(stamp) == len("2026-09-25T10:00:00+00:00")

    def test_scope_label_requires_exactly_one(self):
        assert operations.scope_label(ou_path="/01 OTB") == "OU /01 OTB"
        assert operations.scope_label(domain="jit-logistics.com") == (
            "domain jit-logistics.com"
        )
        assert operations.scope_label(group_email="leads@otbgroup.co.uk") == (
            "group leads@otbgroup.co.uk"
        )
        assert operations.scope_label(all_users=True) == "all active users"
        with pytest.raises(UserInputError):
            operations.scope_label()
        with pytest.raises(UserInputError):
            operations.scope_label(ou_path="/01 OTB", domain="otbgroup.co.uk")
        with pytest.raises(UserInputError):
            operations.scope_label(group_email="x@otbgroup.co.uk", all_users=True)


# ---------------------------------------------------------------------------
# plan_user
# ---------------------------------------------------------------------------


class TestPlanUser:
    @pytest.mark.asyncio
    async def test_reads_directory_then_gmail_for_primary(
        self, config, directory, pool
    ):
        found, send_as_list, planned = await operations.plan_user(
            config, directory, ALICE, gmail_factory=pool.factory
        )
        assert found["primaryEmail"] == ALICE
        assert directory.calls == [
            ("users.get", {"userKey": ALICE, "projection": "full"})
        ]
        assert pool.factory_calls == [ALICE]
        assert [s["sendAsEmail"] for s in send_as_list] == [
            ALICE,
            ALICE_JIT,
            ALICE_HOME,
        ]
        by = {p.send_as_email: p for p in planned}
        assert by[ALICE].status == "planned" and by[ALICE].entity == "OTB"
        assert by[ALICE_JIT].status == "planned" and by[ALICE_JIT].entity == "JIT"
        assert by[ALICE_HOME].status == "skipped"
        assert "blakefamily.uk" in by[ALICE_HOME].reason
        assert pool.mailboxes[ALICE].call_names() == ["sendAs.list"]

    @pytest.mark.asyncio
    async def test_unknown_user_raises(self, config, directory, pool):
        from googleapiclient.errors import HttpError

        with pytest.raises(HttpError):
            await operations.plan_user(
                config, directory, "nobody@otbgroup.co.uk", gmail_factory=pool.factory
            )
        assert pool.factory_calls == []


# ---------------------------------------------------------------------------
# apply_user
# ---------------------------------------------------------------------------


class TestApplyUserDryRun:
    @pytest.mark.asyncio
    async def test_default_is_dry_run_and_never_patches(self, config, directory, pool):
        rows = await _apply_alice(config, directory, pool)
        by = _rows_by_send_as(rows)
        assert by[ALICE].action == "would_apply"
        assert by[ALICE].before_hash == signature_hash(OLD_PRIMARY)
        assert by[ALICE].after_hash and by[ALICE].after_hash != by[ALICE].before_hash
        assert by[ALICE_JIT].action == "would_apply"
        assert by[ALICE_JIT].entity == "JIT"
        assert by[ALICE_HOME].action == "skipped"
        assert "blakefamily.uk" in by[ALICE_HOME].reason
        assert pool.patch_calls() == []
        assert "sendAs.get" not in pool.mailboxes[ALICE].call_names()

    @pytest.mark.asyncio
    async def test_dry_run_reads_ledger_when_sheets_given(
        self, config, directory, pool
    ):
        row = await _ledger_row_for_current_state(config, directory, pool, ALICE)
        sheets = _ledger_with_rows([row])
        rows = await _apply_alice(
            config, directory, pool, sheets=sheets, sheet_id=SHEET_ID
        )
        by = _rows_by_send_as(rows)
        assert by[ALICE].action == "unchanged"
        assert by[ALICE_JIT].action == "would_apply"
        assert "values.append" not in sheets.names()
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_dry_run_tolerates_unreadable_ledger(self, config, directory, pool):
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        sheets.fail_reads = http_error(500, "backendError")
        rows = await _apply_alice(
            config, directory, pool, sheets=sheets, sheet_id=SHEET_ID
        )
        assert {r.action for r in rows} == {"would_apply", "skipped"}
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_include_aliases_false_skips_non_primary(
        self, config, directory, pool
    ):
        rows = await _apply_alice(config, directory, pool, include_aliases=False)
        by = _rows_by_send_as(rows)
        assert by[ALICE].action == "would_apply"
        assert by[ALICE_JIT].action == "skipped"
        assert by[ALICE_JIT].reason == "aliases not included"
        # The engine's own skip reason wins for a personal-domain alias.
        assert by[ALICE_HOME].action == "skipped"
        assert "blakefamily.uk" in by[ALICE_HOME].reason

    @pytest.mark.asyncio
    async def test_engine_error_rows_are_reported_not_raised(
        self, config, directory, pool
    ):
        rows = await operations.apply_user(
            config,
            directory,
            CAROL,
            actor=ACTOR,
            run_id=RUN,
            gmail_factory=pool.factory,
        )
        assert len(rows) == 1
        assert rows[0].action == "error"
        assert "title" in rows[0].reason

    @pytest.mark.asyncio
    async def test_only_send_as_limits_to_that_address(self, config, directory, pool):
        rows = await _apply_alice(
            config, directory, pool, only_send_as=ALICE_JIT.upper()
        )
        assert [r.send_as_email for r in rows] == [ALICE_JIT]
        with pytest.raises(UserInputError) as excinfo:
            await _apply_alice(
                config, directory, pool, only_send_as="nope@otbgroup.co.uk"
            )
        assert ALICE_JIT in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_primary_only(self, config, directory, pool):
        rows = await _apply_alice(config, directory, pool, primary_only=True)
        assert [r.send_as_email for r in rows] == [ALICE]


class TestApplyUserLiveGate:
    @pytest.mark.asyncio
    async def test_live_without_confirm_refuses_before_any_call(
        self, config, directory, pool
    ):
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        with pytest.raises(UserInputError) as excinfo:
            await _apply_alice(
                config,
                directory,
                pool,
                dry_run=False,
                confirm=False,
                sheets=sheets,
                sheet_id=SHEET_ID,
            )
        assert "confirm=True" in str(excinfo.value)
        assert "dry_run=False" in str(excinfo.value)
        assert directory.calls == []
        assert pool.factory_calls == []
        assert sheets.calls == []

    @pytest.mark.asyncio
    async def test_live_without_ledger_client_refuses_before_gmail(
        self, config, directory, pool
    ):
        with pytest.raises(LedgerError):
            await _apply_alice(config, directory, pool, dry_run=False, confirm=True)
        with pytest.raises(LedgerError):
            await _apply_alice(
                config,
                directory,
                pool,
                dry_run=False,
                confirm=True,
                sheets=FakeSheets(),
                sheet_id="",
            )
        assert pool.factory_calls == []
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_live_with_unreadable_ledger_refuses_before_gmail(
        self, config, directory, pool
    ):
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        sheets.fail_reads = http_error(500, "backendError")
        with pytest.raises(LedgerError) as excinfo:
            await _apply_alice(
                config,
                directory,
                pool,
                dry_run=False,
                confirm=True,
                sheets=sheets,
                sheet_id=SHEET_ID,
            )
        assert "ledger" in str(excinfo.value).lower()
        assert pool.factory_calls == []
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_live_with_unwritable_ledger_refuses_before_gmail(
        self, config, directory, pool
    ):
        # No Ledger tab yet, and the tab cannot be created: refuse.
        sheets = FakeSheets({"Other": [["x"]]})
        sheets.fail_writes = http_error(403, "forbidden")
        with pytest.raises(LedgerError):
            await _apply_alice(
                config,
                directory,
                pool,
                dry_run=False,
                confirm=True,
                sheets=sheets,
                sheet_id=SHEET_ID,
            )
        assert pool.patch_calls() == []


class TestApplyUserLive:
    @pytest.mark.asyncio
    async def test_confirm_path_patches_reads_back_and_records(
        self, config, directory, pool
    ):
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        # Real Gmail rewrites what it stores (it adds dir="ltr", drops
        # attributes it dislikes). Mimic that so the read-back differs.
        pool.mailboxes[ALICE].sanitise = lambda h: h.replace(
            "<table", '<table dir="ltr"', 1
        )
        rows = await _apply_alice(
            config,
            directory,
            pool,
            dry_run=False,
            confirm=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
        )
        by = _rows_by_send_as(rows)
        assert by[ALICE].action == "applied"
        assert by[ALICE_JIT].action == "applied"
        assert by[ALICE_HOME].action == "skipped"
        assert set(pool.patch_calls()) == {(ALICE, ALICE), (ALICE, ALICE_JIT)}

        box = pool.mailboxes[ALICE]
        stored_primary = box.send_as[ALICE]["signature"]
        stored_alias = box.send_as[ALICE_JIT]["signature"]
        # The read-back hash is of what Gmail stored, not what we sent.
        assert by[ALICE].after_hash == signature_hash(stored_primary)
        assert by[ALICE].before_hash == signature_hash(OLD_PRIMARY)
        assert by[ALICE_JIT].after_hash == signature_hash(stored_alias)

        ledger = {r["send_as_email"]: r for r in sheets.ledger_rows()}
        assert set(ledger) == {ALICE, ALICE_JIT}
        primary = ledger[ALICE]
        assert primary["actor"] == ACTOR
        assert primary["run_id"] == RUN
        assert primary["user_email"] == ALICE
        assert primary["entity"] == "OTB"
        assert primary["template_version"] == "1.0.0"
        assert primary["statutory_version"] == "1.0.0"
        assert primary["readback_hash"] == signature_hash(stored_primary)
        assert primary["previous_hash"] == signature_hash(OLD_PRIMARY)
        assert primary["previous_signature_html"] == OLD_PRIMARY
        assert primary["applied_at"].endswith("+00:00")
        assert ledger[ALICE_JIT]["entity"] == "JIT"
        assert ledger[ALICE_JIT]["previous_signature_html"] == OLD_ALIAS
        # rendered_hash is of what we sent; readback_hash is of what Gmail
        # kept. They differ, and the ledger must carry both.
        assert primary["rendered_hash"] != primary["readback_hash"]
        assert 'dir="ltr"' in stored_primary

    @pytest.mark.asyncio
    async def test_live_creates_the_ledger_tab_when_missing(
        self, config, directory, pool
    ):
        sheets = FakeSheets({"Other": [["x"]]})
        rows = await _apply_alice(
            config,
            directory,
            pool,
            dry_run=False,
            confirm=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
            include_aliases=False,
        )
        assert _rows_by_send_as(rows)[ALICE].action == "applied"
        assert sheets.tabs["Ledger"][0] == LEDGER_HEADER
        assert len(sheets.ledger_rows()) == 1

    @pytest.mark.asyncio
    async def test_unchanged_detection_skips_the_patch(self, config, directory, pool):
        row = await _ledger_row_for_current_state(config, directory, pool, ALICE)
        sheets = _ledger_with_rows([row])
        rows = await _apply_alice(
            config,
            directory,
            pool,
            dry_run=False,
            confirm=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
        )
        by = _rows_by_send_as(rows)
        assert by[ALICE].action == "unchanged"
        assert by[ALICE].before_hash == by[ALICE].after_hash
        assert by[ALICE_JIT].action == "applied"
        assert pool.patch_calls() == [(ALICE, ALICE_JIT)]
        assert [r["send_as_email"] for r in sheets.ledger_rows()] == [
            ALICE,
            ALICE_JIT,
        ]

    @pytest.mark.asyncio
    async def test_gmail_change_since_ledger_means_reapply(
        self, config, directory, pool
    ):
        row = await _ledger_row_for_current_state(config, directory, pool, ALICE)
        # Someone edited the signature in Gmail after our last apply.
        pool.mailboxes[ALICE].send_as[ALICE]["signature"] = "<div>edited</div>"
        rows = await _apply_alice(
            config,
            directory,
            pool,
            sheets=_ledger_with_rows([row]),
            sheet_id=SHEET_ID,
        )
        alice = _rows_by_send_as(rows)[ALICE]
        assert alice.action == "would_apply"
        assert "differs" in alice.reason

    @pytest.mark.asyncio
    async def test_version_bump_since_ledger_means_reapply(
        self, config, directory, pool
    ):
        row = await _ledger_row_for_current_state(config, directory, pool, ALICE)
        row[LEDGER_HEADER.index("template_version")] = "0.9.0"
        rows = await _apply_alice(
            config,
            directory,
            pool,
            sheets=_ledger_with_rows([row]),
            sheet_id=SHEET_ID,
        )
        alice = _rows_by_send_as(rows)[ALICE]
        assert alice.action == "would_apply"
        assert "0.9.0" in alice.reason

    @pytest.mark.asyncio
    async def test_force_overrides_unchanged(self, config, directory, pool):
        row = await _ledger_row_for_current_state(config, directory, pool, ALICE)
        sheets = _ledger_with_rows([row])
        rows = await _apply_alice(
            config,
            directory,
            pool,
            dry_run=False,
            confirm=True,
            force=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
        )
        by = _rows_by_send_as(rows)
        assert by[ALICE].action == "applied"
        assert "force" in by[ALICE].reason
        assert (ALICE, ALICE) in pool.patch_calls()

    @pytest.mark.asyncio
    async def test_one_failing_send_as_does_not_stop_the_others(
        self, config, directory, pool
    ):
        pool.mailboxes[ALICE].fail_patch_for[ALICE] = http_error(
            403, "insufficientPermissions"
        )
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        rows = await _apply_alice(
            config,
            directory,
            pool,
            dry_run=False,
            confirm=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
        )
        by = _rows_by_send_as(rows)
        assert by[ALICE].action == "error"
        assert by[ALICE].reason.startswith("HttpError:")
        assert by[ALICE_JIT].action == "applied"
        assert [r["send_as_email"] for r in sheets.ledger_rows()] == [ALICE_JIT]

    @pytest.mark.asyncio
    async def test_ledger_append_failure_after_patch_is_an_error_row(
        self, config, directory, pool
    ):
        # Reads and the tab check work; only the append fails.
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        sheets.fail_append = http_error(500, "backendError")
        rows = await _apply_alice(
            config,
            directory,
            pool,
            dry_run=False,
            confirm=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
            include_aliases=False,
        )
        alice = _rows_by_send_as(rows)[ALICE]
        assert alice.action == "error"
        assert "ledger" in alice.reason.lower()
        assert "applied" in alice.reason.lower()
        assert alice.after_hash == signature_hash(
            pool.mailboxes[ALICE].send_as[ALICE]["signature"]
        )
        assert (ALICE, ALICE) in pool.patch_calls()


# ---------------------------------------------------------------------------
# apply_scope
# ---------------------------------------------------------------------------


async def _apply_scope(config, directory, pool, **kw):
    kw.setdefault("actor", ACTOR)
    kw.setdefault("run_id", RUN)
    kw.setdefault("gmail_factory", pool.factory)
    return await operations.apply_scope(config, directory, **kw)


class TestApplyScope:
    @pytest.mark.asyncio
    async def test_exactly_one_scope(self, config, directory, pool):
        with pytest.raises(UserInputError):
            await _apply_scope(config, directory, pool)
        with pytest.raises(UserInputError):
            await _apply_scope(
                config, directory, pool, ou_path="/01 OTB", domain="otbgroup.co.uk"
            )
        assert directory.calls == []

    @pytest.mark.asyncio
    async def test_ou_dry_run_covers_the_ou_only(self, config, directory, pool):
        rows, meta = await _apply_scope(config, directory, pool, ou_path="/01 OTB")
        users = {r.user_email for r in rows}
        assert users == {ALICE, CAROL}
        assert meta["scope"] == "OU /01 OTB"
        assert meta["run_id"] == RUN
        assert meta["dry_run"] is True
        assert meta["actor"] == ACTOR
        assert meta["user_count"] == 2
        assert meta["access_line"]
        assert meta["report_filename"] == f"signatures-dryrun-{RUN}.jsonl"
        assert pool.patch_calls() == []
        # Active users only.
        assert "isSuspended=false" in directory.calls[0][1]["query"]

    @pytest.mark.asyncio
    async def test_jsonl_report_holds_every_row(self, config, directory, pool):
        rows, meta = await _apply_scope(config, directory, pool, ou_path="/01 OTB")
        lines = Path(meta["report_path"]).read_text().strip().splitlines()
        parsed = [json.loads(line) for line in lines]
        assert len(parsed) == len(rows)
        assert {p["send_as_email"] for p in parsed} == {r.send_as_email for r in rows}
        assert set(parsed[0]) >= {"user_email", "action", "before_hash", "after_hash"}

    @pytest.mark.asyncio
    async def test_domain_scope(self, config, directory, pool):
        rows, _ = await _apply_scope(
            config, directory, pool, domain="jit-logistics.com"
        )
        assert {r.user_email for r in rows} == {BOB}
        assert directory.calls[0][1]["domain"] == "jit-logistics.com"

    @pytest.mark.asyncio
    async def test_group_scope_resolves_each_member(self, config, directory, pool):
        rows, meta = await _apply_scope(
            config, directory, pool, group_email="leads@otbgroup.co.uk"
        )
        assert {r.user_email for r in rows} == {ALICE, BOB}
        names = directory.call_names()
        assert names[0] == "members.list"
        assert names.count("users.get") == 2
        assert meta["user_count"] == 2

    @pytest.mark.asyncio
    async def test_max_users_refuses_without_truncating(self, config, directory, pool):
        with pytest.raises(UserInputError) as excinfo:
            await _apply_scope(
                config, directory, pool, domain="otbgroup.co.uk", max_users=1
            )
        message = str(excinfo.value)
        assert "2" in message and "max_users" in message
        assert pool.factory_calls == []
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_live_without_confirm_refuses_before_resolving_users(
        self, config, directory, pool
    ):
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        with pytest.raises(UserInputError):
            await _apply_scope(
                config,
                directory,
                pool,
                ou_path="/01 OTB",
                dry_run=False,
                sheets=sheets,
                sheet_id=SHEET_ID,
            )
        assert directory.calls == []
        assert sheets.calls == []

    @pytest.mark.asyncio
    async def test_live_with_unreadable_ledger_refuses_before_any_write(
        self, config, directory, pool
    ):
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        sheets.fail_reads = http_error(500, "backendError")
        with pytest.raises(LedgerError):
            await _apply_scope(
                config,
                directory,
                pool,
                ou_path="/01 OTB",
                dry_run=False,
                confirm=True,
                sheets=sheets,
                sheet_id=SHEET_ID,
            )
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_live_applies_across_the_scope(self, config, directory, pool):
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        rows, meta = await _apply_scope(
            config,
            directory,
            pool,
            ou_path="/01 OTB",
            dry_run=False,
            confirm=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
        )
        by = _rows_by_send_as(rows)
        assert by[ALICE].action == "applied"
        assert by[ALICE_JIT].action == "applied"
        assert by[CAROL].action == "error"
        assert meta["dry_run"] is False
        assert meta["report_filename"] == f"signatures-apply-{RUN}.jsonl"
        assert len(sheets.ledger_rows()) == 2
        assert meta["counts"]["applied"] == 2

    @pytest.mark.asyncio
    async def test_one_failing_user_does_not_stop_the_scope(
        self, config, directory, pool
    ):
        pool.fail_for[BOB] = RuntimeError("delegation refused")
        rows, _ = await _apply_scope(
            config, directory, pool, group_email="leads@otbgroup.co.uk"
        )
        bob_rows = [r for r in rows if r.user_email == BOB]
        assert len(bob_rows) == 1
        assert bob_rows[0].action == "error"
        assert bob_rows[0].send_as_email == "*"
        assert "RuntimeError" in bob_rows[0].reason
        assert "delegation refused" in bob_rows[0].reason
        assert _rows_by_send_as(rows)[ALICE].action == "would_apply"

    @pytest.mark.asyncio
    async def test_group_member_lookup_failure_is_isolated(
        self, config, directory, pool
    ):
        directory.fail_get_for[BOB] = http_error(503, "backendError")
        rows, _ = await _apply_scope(
            config, directory, pool, group_email="leads@otbgroup.co.uk"
        )
        bob_rows = [r for r in rows if r.user_email == BOB]
        assert bob_rows[0].action == "error"
        assert "HttpError" in bob_rows[0].reason
        assert _rows_by_send_as(rows)[ALICE].action == "would_apply"


# ---------------------------------------------------------------------------
# audit_scope
# ---------------------------------------------------------------------------


class TestAuditScope:
    @pytest.mark.asyncio
    async def test_exactly_one_scope(self, config, directory, pool):
        with pytest.raises(UserInputError):
            await operations.audit_scope(
                config, directory, ledger_latest={}, gmail_factory=pool.factory
            )
        with pytest.raises(UserInputError):
            await operations.audit_scope(
                config,
                directory,
                ou_path="/01 OTB",
                all_users=True,
                ledger_latest={},
                gmail_factory=pool.factory,
            )

    @pytest.mark.asyncio
    async def test_statuses_and_row_shape(self, config, directory, pool):
        row = await _ledger_row_for_current_state(config, directory, pool, ALICE)
        ledger_latest = {(ALICE, ALICE): dict(zip(LEDGER_HEADER, row))}
        rows = await operations.audit_scope(
            config,
            directory,
            ou_path="/01 OTB",
            ledger_latest=ledger_latest,
            gmail_factory=pool.factory,
            audited_at="2026-09-25T07:00:00+00:00",
        )
        assert all(set(r) == set(AUDIT_HEADER) for r in rows)
        by = {r["send_as_email"]: r for r in rows}
        assert by[ALICE]["status"] == "in_sync"
        assert by[ALICE]["ledger_template_version"] == "1.0.0"
        assert by[ALICE]["expected_template_version"] == "1.0.0"
        assert by[ALICE]["current_hash"] == signature_hash(OLD_PRIMARY)
        assert by[ALICE_JIT]["status"] == "never_applied"
        assert by[ALICE_HOME]["status"] == "unmanaged"
        assert by[CAROL]["status"] == "error"
        assert all(r["audited_at"] == "2026-09-25T07:00:00+00:00" for r in rows)
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_stale_and_changed(self, config, directory, pool):
        row = await _ledger_row_for_current_state(config, directory, pool, ALICE)
        stale = dict(zip(LEDGER_HEADER, row))
        stale["statutory_version"] = "0.1.0"
        alias_row = await _ledger_row_for_current_state(
            config, directory, pool, ALICE_JIT
        )
        changed = dict(zip(LEDGER_HEADER, alias_row))
        changed["readback_hash"] = "not-what-gmail-holds"
        rows = await operations.audit_scope(
            config,
            directory,
            domain="otbgroup.co.uk",
            ledger_latest={(ALICE, ALICE): stale, (ALICE, ALICE_JIT): changed},
            gmail_factory=pool.factory,
        )
        by = {r["send_as_email"]: r for r in rows}
        assert by[ALICE]["status"] == "stale_template"
        assert by[ALICE]["ledger_statutory_version"] == "0.1.0"
        assert by[ALICE_JIT]["status"] == "changed_since_apply"

    @pytest.mark.asyncio
    async def test_all_users_is_customer_wide(self, config, directory, pool):
        rows = await operations.audit_scope(
            config,
            directory,
            all_users=True,
            ledger_latest={},
            gmail_factory=pool.factory,
        )
        assert {r["user_email"] for r in rows} == {ALICE, BOB, CAROL, DAVE}
        params = directory.calls[0][1]
        assert params["customer"] == "my_customer"
        assert "domain" not in params
        assert params["query"] == "isSuspended=false"

    @pytest.mark.asyncio
    async def test_group_scope(self, config, directory, pool):
        rows = await operations.audit_scope(
            config,
            directory,
            group_email="leads@otbgroup.co.uk",
            ledger_latest={},
            gmail_factory=pool.factory,
        )
        assert {r["user_email"] for r in rows} == {ALICE, BOB}

    @pytest.mark.asyncio
    async def test_one_failing_user_is_isolated(self, config, directory, pool):
        pool.fail_for[DAVE] = RuntimeError("no mailbox")
        rows = await operations.audit_scope(
            config,
            directory,
            all_users=True,
            ledger_latest={},
            gmail_factory=pool.factory,
        )
        dave = [r for r in rows if r["user_email"] == DAVE]
        assert len(dave) == 1
        assert dave[0]["status"] == "error"
        assert dave[0]["send_as_email"] == "*"
        assert "RuntimeError" in dave[0]["reason"]
        assert len([r for r in rows if r["user_email"] == ALICE]) == 3


# ---------------------------------------------------------------------------
# prepare_ledger / build_runtime
# ---------------------------------------------------------------------------


class TestLedgerAndRuntime:
    @pytest.mark.asyncio
    async def test_prepare_ledger_creates_tab_and_reads(self):
        sheets = FakeSheets()
        latest = await operations.prepare_ledger(sheets, SHEET_ID)
        assert latest == {}
        assert sheets.tabs["Ledger"] == [LEDGER_HEADER]

    @pytest.mark.asyncio
    async def test_prepare_ledger_wraps_failures(self):
        sheets = FakeSheets()
        sheets.fail_reads = http_error(500, "backendError")
        with pytest.raises(LedgerError) as excinfo:
            await operations.prepare_ledger(sheets, SHEET_ID)
        assert "HttpError" in str(excinfo.value)
        with pytest.raises(LedgerError):
            await operations.prepare_ledger(None, SHEET_ID)
        with pytest.raises(LedgerError):
            await operations.prepare_ledger(sheets, "")

    def test_build_runtime_uses_sa_auth_builders(self, monkeypatch):
        from gsignatures import sa_auth

        monkeypatch.setattr(sa_auth, "build_directory_as_admin", lambda: "DIR")
        monkeypatch.setattr(sa_auth, "build_sheets_as_service_account", lambda: "SH")
        monkeypatch.setenv("SIGNATURE_LEDGER_SHEET_ID", "sheet-x")
        runtime = operations.build_runtime()
        assert runtime.directory == "DIR"
        assert runtime.sheets == "SH"
        assert runtime.sheet_id == "sheet-x"
        assert runtime.gmail_factory is sa_auth.build_gmail_for_user
        assert runtime.config.entities

    def test_build_runtime_ledger_required_by_default(self, monkeypatch):
        from gsignatures import sa_auth

        monkeypatch.setattr(sa_auth, "build_directory_as_admin", lambda: "DIR")
        monkeypatch.setattr(sa_auth, "build_sheets_as_service_account", lambda: "SH")
        monkeypatch.delenv("SIGNATURE_LEDGER_SHEET_ID", raising=False)
        with pytest.raises(LedgerError):
            operations.build_runtime()
        runtime = operations.build_runtime(need_ledger=False)
        assert runtime.sheets is None and runtime.sheet_id is None

    def test_build_runtime_auth_error_propagates(self, monkeypatch):
        from gsignatures import sa_auth

        def boom():
            raise sa_auth.SignatureAuthError("no key")

        monkeypatch.setattr(sa_auth, "build_directory_as_admin", boom)
        with pytest.raises(sa_auth.SignatureAuthError):
            operations.build_runtime(need_ledger=False)

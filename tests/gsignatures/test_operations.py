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
  per-user isolation;
* the run-wide ledger rule: after one failed ledger append nothing further
  is patched, in that user or any later one;
* the live write order per address: pending ledger row (rollback record,
  readback_hash "pending"), then the Gmail patch, then the completed row; a
  failed pending append means no patch; a pending row with no completed row
  audits as apply_interrupted and can be restored from;
* ``force`` never touches an address the engine skipped;
* ``restore_user``: the previous signature from a ledger row goes back,
  under the same dry-run and confirm rule, and is itself recorded.

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
        # The table must not claim "no ledger row" when the ledger could not
        # be read: every would_apply reason says the ledger was unavailable.
        for row in rows:
            if row.action == "would_apply":
                assert row.reason.startswith(
                    operations.LEDGER_UNAVAILABLE_NOTE_PREFIX
                ), row.reason
                assert "HttpError" in row.reason
                assert "no ledger row for this address" in row.reason

    @pytest.mark.asyncio
    async def test_dry_run_without_a_ledger_client_says_not_configured(
        self, config, directory, pool
    ):
        rows = await _apply_alice(config, directory, pool)
        alice = _rows_by_send_as(rows)[ALICE]
        assert alice.action == "would_apply"
        assert alice.reason.startswith(operations.LEDGER_NOT_CONFIGURED_NOTE + "; ")

    @pytest.mark.asyncio
    async def test_dry_run_with_no_ledger_tab_is_an_empty_ledger(
        self, config, directory, pool
    ):
        """A shared but never-written Sheet has no Ledger tab. That is an
        empty ledger, not an unavailable one: the reason is the plain
        "no ledger row" and no tab is created by a dry run."""
        sheets = FakeSheets({"Sheet1": [[]]})
        rows = await _apply_alice(
            config, directory, pool, sheets=sheets, sheet_id=SHEET_ID
        )
        alice = _rows_by_send_as(rows)[ALICE]
        assert alice.action == "would_apply"
        assert alice.reason == "no ledger row for this address"
        assert "Ledger" not in sheets.tabs
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_confirm_alone_is_still_a_dry_run(self, config, directory, pool):
        """The rule is dry_run=False AND confirm=True. confirm on its own
        must not go live."""
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        rows = await _apply_alice(
            config, directory, pool, confirm=True, sheets=sheets, sheet_id=SHEET_ID
        )
        assert rows
        assert {r.action for r in rows} <= {"would_apply", "skipped", "unchanged"}
        assert pool.patch_calls() == []
        assert "values.append" not in sheets.names()
        assert "values.update" not in sheets.names()

    @pytest.mark.asyncio
    async def test_include_aliases_false_hides_alias_errors(
        self, config, directory, pool
    ):
        """An alias the caller excluded is 'skipped' even when the engine
        would have reported an error for it; the primary's error stays."""
        pool.mailboxes[CAROL].send_as["carol@jit-logistics.com"] = send_as(
            "carol@jit-logistics.com", signature=""
        )
        rows = await operations.apply_user(
            config,
            directory,
            CAROL,
            actor=ACTOR,
            run_id=RUN,
            include_aliases=False,
            gmail_factory=pool.factory,
        )
        by = _rows_by_send_as(rows)
        assert by[CAROL].action == "error"
        assert by["carol@jit-logistics.com"].action == "skipped"
        assert by["carol@jit-logistics.com"].reason == "aliases not included"
        # With aliases included the same alias is an error row.
        rows = await operations.apply_user(
            config,
            directory,
            CAROL,
            actor=ACTOR,
            run_id=RUN,
            gmail_factory=pool.factory,
        )
        assert _rows_by_send_as(rows)["carol@jit-logistics.com"].action == "error"

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
        # Ledger before Directory before Gmail: nothing else was touched.
        assert directory.calls == []
        assert pool.factory_calls == []
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_live_with_read_only_ledger_share_refuses_before_gmail(
        self, config, directory, pool
    ):
        """The steady state: the Ledger tab exists with the right header, but
        the Sheet is shared with the service account as Viewer. Every read
        works; only writes fail. The run must be refused before any patch,
        not after it at the append."""
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        sheets.fail_writes = http_error(403, "forbidden")
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
        assert "HttpError" in str(excinfo.value)
        assert directory.calls == []
        assert pool.factory_calls == []
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_write_probe_leaves_the_ledger_unchanged(
        self, config, directory, pool
    ):
        row = await _ledger_row_for_current_state(config, directory, pool, ALICE)
        sheets = _ledger_with_rows([row])
        before = [list(r) for r in sheets.tabs["Ledger"]]
        await _apply_alice(
            config,
            directory,
            pool,
            dry_run=False,
            confirm=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
            primary_only=True,
        )
        # Alice's primary is unchanged, so the only write was the probe,
        # and the probe rewrote the header byte for byte.
        assert sheets.tabs["Ledger"] == before
        assert sheets.names().count("values.update") == 1
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_gate_live_is_public_and_verbatim(self):
        operations.gate_live(True, False)
        operations.gate_live(True, True)
        operations.gate_live(False, True)
        with pytest.raises(UserInputError) as excinfo:
            operations.gate_live(False, False)
        assert str(excinfo.value) == operations.LIVE_CONFIRM_MESSAGE

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
        # One address applied: its pending row, then its completed row.
        assert len(sheets.ledger_rows()) == 2
        assert len(sheets.completed_ledger_rows()) == 1

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
        # The seeded row, then the alias's pending and completed rows.
        assert [r["send_as_email"] for r in sheets.ledger_rows()] == [
            ALICE,
            ALICE_JIT,
            ALICE_JIT,
        ]
        assert [r["send_as_email"] for r in sheets.completed_ledger_rows()] == [
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
    async def test_force_never_patches_a_skipped_alias(self, config, directory, pool):
        """force re-applies unchanged addresses; it must never override the
        engine's skip, or a personal blakefamily.uk alias would get a company
        signature."""
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
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
        assert by[ALICE_HOME].action == "skipped"
        assert "blakefamily.uk" in by[ALICE_HOME].reason
        assert "force" not in by[ALICE_HOME].reason
        assert (ALICE, ALICE_HOME) not in pool.patch_calls()
        assert set(pool.patch_calls()) == {(ALICE, ALICE), (ALICE, ALICE_JIT)}
        assert all(r["send_as_email"] != ALICE_HOME for r in sheets.ledger_rows())
        assert pool.mailboxes[ALICE].send_as[ALICE_HOME]["signature"] == ""

    @pytest.mark.asyncio
    async def test_only_send_as_personal_alias_live_is_skipped(
        self, config, directory, pool
    ):
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        rows = await _apply_alice(
            config,
            directory,
            pool,
            dry_run=False,
            confirm=True,
            force=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
            only_send_as=ALICE_HOME,
        )
        assert len(rows) == 1
        assert rows[0].action == "skipped"
        assert rows[0].send_as_email == ALICE_HOME
        assert pool.patch_calls() == []
        assert sheets.ledger_rows() == []

    @pytest.mark.asyncio
    async def test_force_never_patches_a_suspended_user(self, config, directory, pool):
        erin = "erin@otbgroup.co.uk"
        directory.users_by_email[erin] = user(
            erin, "/01 OTB", full="Erin Eve", title="Analyst", suspended=True
        )
        pool.add(erin, [send_as(erin, primary=True, signature="<div>old</div>")])
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        rows = await operations.apply_user(
            config,
            directory,
            erin,
            actor=ACTOR,
            run_id=RUN,
            dry_run=False,
            confirm=True,
            force=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
            gmail_factory=pool.factory,
        )
        assert [r.action for r in rows] == ["skipped"]
        assert "suspended" in rows[0].reason
        assert pool.patch_calls() == []
        assert sheets.ledger_rows() == []

    @pytest.mark.asyncio
    async def test_ledger_row_for_another_user_does_not_count_as_unchanged(
        self, config, directory, pool
    ):
        """A ledger row keyed (BOB, BOB) carrying Alice's hashes must not make
        Alice's primary read unchanged: the key is the mailbox and address."""
        row = await _ledger_row_for_current_state(config, directory, pool, ALICE)
        row[LEDGER_HEADER.index("user_email")] = BOB
        row[LEDGER_HEADER.index("send_as_email")] = BOB
        rows = await _apply_alice(
            config, directory, pool, sheets=_ledger_with_rows([row]), sheet_id=SHEET_ID
        )
        alice = _rows_by_send_as(rows)[ALICE]
        assert alice.action == "would_apply"
        assert "no ledger row" in alice.reason

    @pytest.mark.asyncio
    async def test_ledger_append_failure_stops_further_patches_in_the_run(
        self, config, directory, pool
    ):
        """Two planned addresses, every append fails: no patch at all. The
        pending row for the first address cannot be written, so it is not
        patched; the second address is an error row that says it was not
        attempted. No write happens without its ledger row."""
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
        )
        by = _rows_by_send_as(rows)
        assert by[ALICE].action == "error"
        assert by[ALICE].reason.startswith(operations.LEDGER_PENDING_FAILED_REASON)
        assert "HttpError" in by[ALICE].reason
        assert by[ALICE].after_hash is None
        assert by[ALICE_JIT].action == "error"
        assert by[ALICE_JIT].reason.startswith(operations.LEDGER_FAILED_REASON)
        assert "HttpError" in by[ALICE_JIT].reason
        assert by[ALICE_JIT].after_hash is None
        assert by[ALICE_HOME].action == "skipped"
        assert pool.patch_calls() == []
        assert pool.mailboxes[ALICE].send_as[ALICE]["signature"] == OLD_PRIMARY
        assert pool.mailboxes[ALICE].send_as[ALICE_JIT]["signature"] == OLD_ALIAS
        # Exactly one append was attempted (the first pending row).
        assert sheets.append_calls == 1

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
        # The failed patch leaves the pending row on record and says so.
        assert "pending ledger row" in by[ALICE].reason
        assert "apply_interrupted" in by[ALICE].reason
        assert by[ALICE_JIT].action == "applied"
        assert [r["send_as_email"] for r in sheets.ledger_rows()] == [
            ALICE,
            ALICE_JIT,
            ALICE_JIT,
        ]
        assert [r["send_as_email"] for r in sheets.pending_ledger_rows()] == [
            ALICE,
            ALICE_JIT,
        ]
        assert [r["send_as_email"] for r in sheets.completed_ledger_rows()] == [
            ALICE_JIT
        ]

    @pytest.mark.asyncio
    async def test_ledger_append_failure_after_patch_is_an_error_row(
        self, config, directory, pool
    ):
        # Reads, the tab check and the pending append work; only the second
        # append (the completed row) fails.
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        sheets.fail_append_on_calls = {2: http_error(500, "backendError")}
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
        assert operations.LEDGER_APPEND_FAILED_REASON in alice.reason
        assert alice.after_hash == signature_hash(
            pool.mailboxes[ALICE].send_as[ALICE]["signature"]
        )
        assert (ALICE, ALICE) in pool.patch_calls()
        # The pending row is still there: the rollback record survived.
        pending = sheets.pending_ledger_rows()
        assert [r["send_as_email"] for r in pending] == [ALICE]
        assert pending[0]["previous_signature_html"] == OLD_PRIMARY
        assert sheets.completed_ledger_rows() == []


# ---------------------------------------------------------------------------
# the pending ledger row: written before the patch, completed after
# ---------------------------------------------------------------------------


def _pending_row(send_as_email, previous_html, applied_at, run_id, entity="OTB"):
    """A pending row as an interrupted apply leaves it."""
    from gsignatures.ledger import PENDING_READBACK

    return [
        applied_at,
        ACTOR,
        ALICE,
        send_as_email,
        entity,
        "1.0.0",
        "1.0.0",
        "rendered-" + run_id,
        PENDING_READBACK,
        signature_hash(previous_html),
        previous_html,
        run_id,
    ]


class TestPendingLedgerRow:
    @pytest.mark.asyncio
    async def test_pending_row_is_appended_before_the_patch(
        self, config, directory, pool
    ):
        """One shared timeline across the Sheets and Gmail fakes: for each
        address the order is append(pending), patch, append(completed)."""
        from gsignatures.ledger import PENDING_READBACK

        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        timeline: list = []
        sheets.timeline = timeline
        pool.mailboxes[ALICE].timeline = timeline
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

        # Reduce each event to (kind, address, readback marker or None).
        events = []
        for name, payload in timeline:
            if name == "sendAs.patch":
                events.append(("patch", payload, None))
            else:
                ((send_as, readback),) = payload
                events.append(
                    (
                        "append",
                        send_as,
                        "pending" if readback == PENDING_READBACK else "completed",
                    )
                )
        assert events == [
            ("append", ALICE, "pending"),
            ("patch", ALICE, None),
            ("append", ALICE, "completed"),
            ("append", ALICE_JIT, "pending"),
            ("patch", ALICE_JIT, None),
            ("append", ALICE_JIT, "completed"),
        ]
        assert operations.LIVE_APPLY_ORDER == (
            "ledger_pending",
            "gmail_patch",
            "ledger_completed",
        )

    @pytest.mark.asyncio
    async def test_pending_row_carries_the_full_rollback_record(
        self, config, directory, pool
    ):
        """The pending row is a complete rollback record on its own: the
        planned hash, the previous hash and HTML, the run_id; only the
        read-back is the marker. The completed row that follows has the
        same run_id and the real read-back hash."""
        from gsignatures.ledger import PENDING_READBACK

        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        _, _, planned = await operations.plan_user(
            config, directory, ALICE, gmail_factory=pool.factory
        )
        plan = next(p for p in planned if p.send_as_email == ALICE)
        await _apply_alice(
            config,
            directory,
            pool,
            dry_run=False,
            confirm=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
            include_aliases=False,
        )
        pending, completed = sheets.ledger_rows()
        assert pending["readback_hash"] == PENDING_READBACK
        assert pending["rendered_hash"] == plan.rendered_hash
        assert pending["previous_hash"] == signature_hash(OLD_PRIMARY)
        assert pending["previous_signature_html"] == OLD_PRIMARY
        assert pending["run_id"] == RUN
        assert pending["actor"] == ACTOR
        assert pending["entity"] == "OTB"
        assert pending["template_version"] == "1.0.0"
        assert pending["statutory_version"] == "1.0.0"
        assert pending["applied_at"].endswith("+00:00")

        stored = pool.mailboxes[ALICE].send_as[ALICE]["signature"]
        assert completed["readback_hash"] == signature_hash(stored)
        assert completed["readback_hash"] != PENDING_READBACK
        assert completed["rendered_hash"] == plan.rendered_hash
        assert completed["previous_signature_html"] == OLD_PRIMARY
        assert completed["run_id"] == RUN
        assert completed["applied_at"] >= pending["applied_at"]
        # Every field but the read-back and the stamp is identical.
        for key in LEDGER_HEADER:
            if key not in ("readback_hash", "applied_at"):
                assert pending[key] == completed[key], key

    @pytest.mark.asyncio
    async def test_failed_pending_append_means_no_patch(self, config, directory, pool):
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        sheets.fail_append_on_calls = {1: http_error(503, "backendError")}
        timeline: list = []
        sheets.timeline = timeline
        pool.mailboxes[ALICE].timeline = timeline
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
        assert alice.reason.startswith(operations.LEDGER_PENDING_FAILED_REASON)
        assert alice.after_hash is None
        assert pool.patch_calls() == []
        assert pool.mailboxes[ALICE].send_as[ALICE]["signature"] == OLD_PRIMARY
        assert [name for name, _ in timeline] == ["values.append"]
        assert sheets.ledger_rows() == []

    @pytest.mark.asyncio
    async def test_completed_row_is_read_as_latest_after_a_full_apply(
        self, config, directory, pool
    ):
        """After apply the next apply reads the completed row: unchanged."""
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        await _apply_alice(
            config,
            directory,
            pool,
            dry_run=False,
            confirm=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
        )
        rows = await _apply_alice(
            config, directory, pool, sheets=sheets, sheet_id=SHEET_ID
        )
        by = _rows_by_send_as(rows)
        assert by[ALICE].action == "unchanged"
        assert by[ALICE_JIT].action == "unchanged"
        latest = await operations.prepare_ledger(sheets, SHEET_ID)
        audit = await operations.audit_scope(
            config,
            directory,
            ou_path="/01 OTB",
            ledger_latest=latest,
            gmail_factory=pool.factory,
        )
        statuses = {r["send_as_email"]: r["status"] for r in audit}
        assert statuses[ALICE] == "in_sync"
        assert statuses[ALICE_JIT] == "in_sync"

    @pytest.mark.asyncio
    async def test_pending_only_row_audits_as_apply_interrupted(
        self, config, directory, pool
    ):
        sheets = _ledger_with_rows(
            [_pending_row(ALICE, OLD_PRIMARY, "2026-09-20T09:00:00+00:00", "run-x")]
        )
        latest = await operations.prepare_ledger(sheets, SHEET_ID)
        audit = await operations.audit_scope(
            config,
            directory,
            ou_path="/01 OTB",
            ledger_latest=latest,
            gmail_factory=pool.factory,
        )
        alice = {r["send_as_email"]: r for r in audit}[ALICE]
        assert alice["status"] == "apply_interrupted"
        assert "run-x" in alice["reason"]
        assert alice["status"] in operations.AUDIT_STATUSES
        assert alice["status"] in operations.DRIFT_STATUSES
        assert operations.has_drift(audit)
        assert "apply_interrupted: 1" in operations.format_audit_counts(audit)

    @pytest.mark.asyncio
    async def test_pending_only_row_means_the_next_apply_reapplies(
        self, config, directory, pool
    ):
        """A pending row never counts as 'unchanged', whatever Gmail holds."""
        row = await _ledger_row_for_current_state(config, directory, pool, ALICE)
        from gsignatures.ledger import PENDING_READBACK

        row[LEDGER_HEADER.index("readback_hash")] = PENDING_READBACK
        rows = await _apply_alice(
            config,
            directory,
            pool,
            sheets=_ledger_with_rows([row]),
            sheet_id=SHEET_ID,
            primary_only=True,
        )
        assert rows[0].action == "would_apply"
        assert "pending" in rows[0].reason

    @pytest.mark.asyncio
    async def test_restore_from_a_pending_row(self, pool):
        """An interrupted apply left only the pending row: its
        previous_signature_html goes back and the restore is recorded."""
        before = "<div>what was there before the interrupted apply</div>"
        sheets = _ledger_with_rows(
            [_pending_row(ALICE, before, "2026-09-20T09:00:00+00:00", "run-x")]
        )
        rows = await operations.restore_user(
            ALICE,
            None,
            actor=ACTOR,
            run_id="restore-1",
            sheets=sheets,
            sheet_id=SHEET_ID,
            gmail_factory=pool.factory,
        )
        assert rows[0].action == "would_apply"
        assert "run-x" in rows[0].reason
        assert rows[0].after_hash == signature_hash(before)

        rows = await operations.restore_user(
            ALICE,
            None,
            actor=ACTOR,
            run_id="restore-1",
            dry_run=False,
            confirm=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
            gmail_factory=pool.factory,
        )
        assert rows[0].action == "applied"
        assert pool.mailboxes[ALICE].send_as[ALICE]["signature"] == before
        new = sheets.ledger_rows()[-1]
        assert new["run_id"] == "restore-1"
        assert new["template_version"] == operations.RESTORED_VERSION
        assert new["previous_signature_html"] == OLD_PRIMARY

    @pytest.mark.asyncio
    async def test_restore_prefers_the_completed_row_of_a_run(self, pool):
        """Both rows of run-y exist; by run_id or by latest, the completed
        row is the source (same previous HTML either way, but the reason
        must not name a pending row)."""
        older = "<div>older</div>"
        prev = "<div>previous</div>"
        sheets = _ledger_with_rows(
            [
                _pending_row(ALICE, older, "2026-09-19T09:00:00+00:00", "run-x"),
                _pending_row(ALICE, prev, "2026-09-20T09:00:00+00:00", "run-y"),
                _applied_row(ALICE, prev, "2026-09-20T09:00:00+00:00", "run-y"),
            ]
        )
        rows = await operations.restore_user(
            ALICE,
            None,
            actor=ACTOR,
            run_id="restore-2",
            from_run_id="run-y",
            sheets=sheets,
            sheet_id=SHEET_ID,
            gmail_factory=pool.factory,
        )
        assert rows[0].after_hash == signature_hash(prev)
        rows = await operations.restore_user(
            ALICE,
            None,
            actor=ACTOR,
            run_id="restore-2",
            sheets=sheets,
            sheet_id=SHEET_ID,
            gmail_factory=pool.factory,
        )
        assert rows[0].after_hash == signature_hash(prev)
        assert "run-y" in rows[0].reason
        rows = await operations.restore_user(
            ALICE,
            None,
            actor=ACTOR,
            run_id="restore-2",
            from_run_id="run-x",
            sheets=sheets,
            sheet_id=SHEET_ID,
            gmail_factory=pool.factory,
        )
        assert rows[0].after_hash == signature_hash(older)

    @pytest.mark.asyncio
    async def test_pending_row_from_a_failed_patch_audits_as_interrupted(
        self, config, directory, pool
    ):
        """Patch fails after the pending row: the row stays, the audit says
        apply_interrupted, and restore can still use it."""
        pool.mailboxes[ALICE].fail_patch_for[ALICE] = http_error(403, "forbidden")
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        await _apply_alice(
            config,
            directory,
            pool,
            dry_run=False,
            confirm=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
            include_aliases=False,
        )
        latest = await operations.prepare_ledger(sheets, SHEET_ID)
        audit = await operations.audit_scope(
            config,
            directory,
            ou_path="/01 OTB",
            ledger_latest=latest,
            gmail_factory=pool.factory,
        )
        alice = {r["send_as_email"]: r for r in audit}[ALICE]
        assert alice["status"] == "apply_interrupted"
        assert RUN in alice["reason"]


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
        assert len(sheets.ledger_rows()) == 4
        assert len(sheets.completed_ledger_rows()) == 2
        assert meta["counts"]["applied"] == 2

    @pytest.mark.asyncio
    async def test_live_scope_patches_each_mailbox_with_its_own_client(
        self, config, directory, pool
    ):
        """Two users applied live: each patch lands in its own mailbox, one
        Gmail client per user, and every ledger row names the mailbox its
        send-as belongs to."""
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        rows, meta = await _apply_scope(
            config,
            directory,
            pool,
            group_email="leads@otbgroup.co.uk",
            dry_run=False,
            confirm=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
        )
        assert set(pool.patch_calls()) == {
            (ALICE, ALICE),
            (ALICE, ALICE_JIT),
            (BOB, BOB),
        }
        assert sorted(pool.factory_calls) == [ALICE, BOB]
        owner_of = {ALICE: ALICE, ALICE_JIT: ALICE, BOB: BOB}
        ledger = sheets.ledger_rows()
        assert len(ledger) == 6
        assert len(sheets.completed_ledger_rows()) == 3
        for entry in ledger:
            assert entry["user_email"] == owner_of[entry["send_as_email"]]
            assert entry["run_id"] == RUN
            assert entry["actor"] == ACTOR
        by = _rows_by_send_as(rows)
        assert by[BOB].user_email == BOB and by[BOB].action == "applied"
        assert by[ALICE_JIT].user_email == ALICE
        assert meta["counts"]["applied"] == 3
        assert meta["ledger_failed"] is None

    @pytest.mark.asyncio
    async def test_ledger_append_failure_stops_later_users_in_the_scope(
        self, config, directory, pool
    ):
        """Alice's first (pending) append fails; Alice is not patched, and
        Alice's alias and Bob are then not attempted. No patch for the whole
        scope."""
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        sheets.fail_append = http_error(500, "backendError")
        rows, meta = await _apply_scope(
            config,
            directory,
            pool,
            group_email="leads@otbgroup.co.uk",
            dry_run=False,
            confirm=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
        )
        assert pool.patch_calls() == []
        by = _rows_by_send_as(rows)
        assert by[ALICE].action == "error"
        assert by[ALICE].reason.startswith(operations.LEDGER_PENDING_FAILED_REASON)
        assert by[ALICE_JIT].action == "error"
        assert by[ALICE_JIT].reason.startswith(operations.LEDGER_FAILED_REASON)
        assert by[BOB].action == "error"
        assert by[BOB].reason.startswith(operations.LEDGER_FAILED_REASON)
        assert meta["ledger_failed"] and "HttpError" in meta["ledger_failed"]
        assert meta["counts"].get("applied", 0) == 0
        assert pool.mailboxes[BOB].send_as[BOB]["signature"] == ""

    @pytest.mark.asyncio
    async def test_dry_run_scope_reports_the_ledger_note(self, config, directory, pool):
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        sheets.fail_reads = http_error(500, "backendError")
        rows, meta = await _apply_scope(
            config, directory, pool, ou_path="/01 OTB", sheets=sheets, sheet_id=SHEET_ID
        )
        assert meta["ledger_note"].startswith(operations.LEDGER_UNAVAILABLE_NOTE_PREFIX)
        for row in rows:
            if row.action == "would_apply":
                assert row.reason.startswith(operations.LEDGER_UNAVAILABLE_NOTE_PREFIX)
        rows, meta = await _apply_scope(
            config,
            directory,
            pool,
            ou_path="/01 OTB",
            sheets=FakeSheets({"Ledger": [LEDGER_HEADER]}),
            sheet_id=SHEET_ID,
        )
        assert meta["ledger_note"] is None

    @pytest.mark.asyncio
    async def test_live_with_read_only_ledger_share_refuses_before_any_gmail(
        self, config, directory, pool
    ):
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        sheets.fail_writes = http_error(403, "forbidden")
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
        assert directory.calls == []
        assert pool.factory_calls == []

    @pytest.mark.asyncio
    async def test_setup_error_propagates_instead_of_n_error_rows(
        self, config, directory, pool
    ):
        """A missing or bad key is not one user's fault: SignatureAuthError
        from the Gmail factory aborts the scope so the tool reports an error
        (audit status=error) instead of a clean run full of error rows."""
        from gsignatures import sa_auth

        def broken_factory(email):
            raise sa_auth.SignatureAuthError("key file could not be read")

        with pytest.raises(sa_auth.SignatureAuthError):
            await _apply_scope(
                config,
                directory,
                pool,
                ou_path="/01 OTB",
                gmail_factory=broken_factory,
            )

    @pytest.mark.asyncio
    async def test_unknown_group_is_a_user_input_error(self, config, directory, pool):
        with pytest.raises(UserInputError) as excinfo:
            await _apply_scope(
                config, directory, pool, group_email="nope@otbgroup.co.uk"
            )
        message = str(excinfo.value)
        assert "nope@otbgroup.co.uk" in message
        assert "Nothing was changed" in message
        assert pool.factory_calls == []

    @pytest.mark.asyncio
    async def test_group_scope_suspended_member_is_skipped_not_patched(
        self, config, directory, pool
    ):
        """members.list does not filter suspended users, so the engine's skip
        must carry it: no patch, no ledger row, a skipped row."""
        erin = "erin@otbgroup.co.uk"
        directory.users_by_email[erin] = user(
            erin, "/01 OTB", full="Erin Eve", title="Analyst", suspended=True
        )
        directory.groups["leads@otbgroup.co.uk"].append({"email": erin, "type": "USER"})
        pool.add(erin, [send_as(erin, primary=True, signature="")])
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        rows, _ = await _apply_scope(
            config,
            directory,
            pool,
            group_email="leads@otbgroup.co.uk",
            dry_run=False,
            confirm=True,
            sheets=sheets,
            sheet_id=SHEET_ID,
        )
        by = _rows_by_send_as(rows)
        assert by[erin].action == "skipped"
        assert "suspended" in by[erin].reason
        assert (erin, erin) not in pool.patch_calls()
        assert all(r["user_email"] != erin for r in sheets.ledger_rows())

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
    async def test_stale_directory_when_directory_data_changed(
        self, config, directory, pool
    ):
        """Gmail still holds exactly what was applied, but Alice's job title
        changed in the Directory since: the audit must say so, as apply
        would say would_apply for the same address."""
        row = await _ledger_row_for_current_state(config, directory, pool, ALICE)
        ledger_latest = {(ALICE, ALICE): dict(zip(LEDGER_HEADER, row))}
        directory.users_by_email[ALICE]["organizations"][0]["title"] = "Chair"
        rows = await operations.audit_scope(
            config,
            directory,
            ou_path="/01 OTB",
            ledger_latest=ledger_latest,
            gmail_factory=pool.factory,
        )
        alice = {r["send_as_email"]: r for r in rows}[ALICE]
        assert alice["status"] == "stale_directory"
        assert "Directory" in alice["reason"]
        assert operations.has_drift([alice])
        assert operations.audit_counts(rows)["stale_directory"] == 1
        assert "stale_directory: 1" in operations.format_audit_counts(rows)

    @pytest.mark.asyncio
    async def test_setup_error_propagates_instead_of_n_error_rows(
        self, config, directory, pool
    ):
        from gsignatures import sa_auth

        def broken_factory(email):
            raise sa_auth.SignatureAuthError("key file could not be read")

        with pytest.raises(sa_auth.SignatureAuthError):
            await operations.audit_scope(
                config,
                directory,
                all_users=True,
                ledger_latest={},
                gmail_factory=broken_factory,
            )

    @pytest.mark.asyncio
    async def test_unknown_group_is_a_user_input_error(self, config, directory, pool):
        with pytest.raises(UserInputError) as excinfo:
            await operations.audit_scope(
                config,
                directory,
                group_email="nope@otbgroup.co.uk",
                ledger_latest={},
                gmail_factory=pool.factory,
            )
        assert "nope@otbgroup.co.uk" in str(excinfo.value)

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
    async def test_prepare_ledger_probe_is_opt_in(self):
        """Audits read only: no probe write on an existing, correct tab.
        A write path asks for the probe and a Viewer share is refused."""
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        await operations.prepare_ledger(sheets, SHEET_ID)
        assert "values.update" not in sheets.names()
        await operations.prepare_ledger(sheets, SHEET_ID, probe_write=True)
        assert sheets.names().count("values.update") == 1
        assert sheets.tabs["Ledger"] == [LEDGER_HEADER]
        sheets.fail_writes = http_error(403, "forbidden")
        await operations.prepare_ledger(sheets, SHEET_ID)
        with pytest.raises(LedgerError) as excinfo:
            await operations.prepare_ledger(sheets, SHEET_ID, probe_write=True)
        assert "HttpError" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_read_ledger_best_effort_treats_missing_tab_as_empty(self):
        sheets = FakeSheets({"Sheet1": [[]]})
        assert await operations.read_ledger_best_effort(sheets, SHEET_ID) == ({}, None)
        assert "Ledger" not in sheets.tabs
        assert await operations.read_ledger_best_effort(None, SHEET_ID) == (
            None,
            operations.LEDGER_NOT_CONFIGURED_NOTE,
        )
        sheets.fail_reads = http_error(500, "backendError")
        rows, note = await operations.read_ledger_best_effort(sheets, SHEET_ID)
        assert rows is None
        assert note.startswith(operations.LEDGER_UNAVAILABLE_NOTE_PREFIX)

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


# ---------------------------------------------------------------------------
# restore_user
# ---------------------------------------------------------------------------


PREVIOUS = "<div>hand-written before the rollout</div>"
RESTORE_RUN = "restore01"


async def _restore(pool, **kw):
    kw.setdefault("actor", ACTOR)
    kw.setdefault("run_id", RESTORE_RUN)
    kw.setdefault("gmail_factory", pool.factory)
    kw.setdefault("sheet_id", SHEET_ID)
    return await operations.restore_user(ALICE, kw.pop("send_as_email", None), **kw)


def _applied_row(send_as_email, previous_html, applied_at, run_id, entity="OTB"):
    return [
        applied_at,
        ACTOR,
        ALICE,
        send_as_email,
        entity,
        "1.0.0",
        "1.0.0",
        "rendered-" + run_id,
        "readback-" + run_id,
        signature_hash(previous_html),
        previous_html,
        run_id,
    ]


class TestRestoreUser:
    @pytest.mark.asyncio
    async def test_live_gate_before_any_call(self, directory, pool):
        sheets = _ledger_with_rows(
            [_applied_row(ALICE, PREVIOUS, "2026-09-20T09:00:00+00:00", "run-a")]
        )
        with pytest.raises(UserInputError) as excinfo:
            await _restore(pool, sheets=sheets, dry_run=False)
        assert str(excinfo.value) == operations.LIVE_CONFIRM_MESSAGE
        assert sheets.calls == []
        assert pool.factory_calls == []

    @pytest.mark.asyncio
    async def test_dry_run_needs_a_readable_ledger(self, pool):
        with pytest.raises(LedgerError):
            await _restore(pool, sheets=None)
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        sheets.fail_reads = http_error(500, "backendError")
        with pytest.raises(LedgerError):
            await _restore(pool, sheets=sheets)
        assert pool.factory_calls == []

    @pytest.mark.asyncio
    async def test_no_ledger_row_is_a_user_input_error(self, pool):
        sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
        with pytest.raises(UserInputError) as excinfo:
            await _restore(pool, sheets=sheets)
        assert "no row" in str(excinfo.value)
        assert "Nothing was changed" in str(excinfo.value)
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_dry_run_reports_the_row_it_would_restore(self, pool):
        sheets = _ledger_with_rows(
            [_applied_row(ALICE, PREVIOUS, "2026-09-20T09:00:00+00:00", "run-a")]
        )
        rows = await _restore(pool, sheets=sheets)
        assert len(rows) == 1
        row = rows[0]
        assert row.action == "would_apply"
        assert row.user_email == ALICE and row.send_as_email == ALICE
        assert row.entity == "OTB"
        assert row.template_version == operations.RESTORED_VERSION
        assert row.before_hash == signature_hash(OLD_PRIMARY)
        assert row.after_hash == signature_hash(PREVIOUS)
        assert "run-a" in row.reason and "2026-09-20" in row.reason
        assert pool.patch_calls() == []
        assert "values.append" not in sheets.names()
        assert "values.update" not in sheets.names()  # no probe on a dry run

    @pytest.mark.asyncio
    async def test_confirm_alone_is_still_a_dry_run(self, pool):
        sheets = _ledger_with_rows(
            [_applied_row(ALICE, PREVIOUS, "2026-09-20T09:00:00+00:00", "run-a")]
        )
        rows = await _restore(pool, sheets=sheets, confirm=True)
        assert rows[0].action == "would_apply"
        assert pool.patch_calls() == []
        assert sheets.ledger_rows()[-1]["run_id"] == "run-a"

    @pytest.mark.asyncio
    async def test_live_restores_the_previous_html_and_records_it(self, pool):
        sheets = _ledger_with_rows(
            [_applied_row(ALICE, PREVIOUS, "2026-09-20T09:00:00+00:00", "run-a")]
        )
        rows = await _restore(pool, sheets=sheets, dry_run=False, confirm=True)
        row = rows[0]
        assert row.action == "applied"
        assert pool.patch_calls() == [(ALICE, ALICE)]
        box = pool.mailboxes[ALICE]
        patched = [c for c in box.calls if c[0] == "sendAs.patch"][0][1]
        assert patched["body"] == {"signature": PREVIOUS}
        stored = box.send_as[ALICE]["signature"]
        assert row.after_hash == signature_hash(stored)
        assert row.before_hash == signature_hash(OLD_PRIMARY)
        ledger = sheets.ledger_rows()
        assert len(ledger) == 2
        new = ledger[-1]
        assert new["run_id"] == RESTORE_RUN
        assert new["actor"] == ACTOR
        assert new["user_email"] == ALICE and new["send_as_email"] == ALICE
        assert new["entity"] == "OTB"
        assert new["template_version"] == operations.RESTORED_VERSION
        assert new["statutory_version"] == operations.RESTORED_VERSION
        assert new["rendered_hash"] == signature_hash(PREVIOUS)
        assert new["readback_hash"] == signature_hash(stored)
        assert new["previous_hash"] == signature_hash(OLD_PRIMARY)
        assert new["previous_signature_html"] == OLD_PRIMARY

    @pytest.mark.asyncio
    async def test_restored_address_reads_as_not_managed_afterwards(
        self, config, directory, pool
    ):
        """After a restore the next apply re-applies (the ledger says
        'restored', not the pinned versions) and the audit reports
        stale_template: the managed signature is not in place."""
        sheets = _ledger_with_rows(
            [_applied_row(ALICE, PREVIOUS, "2026-09-20T09:00:00+00:00", "run-a")]
        )
        await _restore(pool, sheets=sheets, dry_run=False, confirm=True)
        rows = await _apply_alice(
            config, directory, pool, sheets=sheets, sheet_id=SHEET_ID, primary_only=True
        )
        assert rows[0].action == "would_apply"
        assert operations.RESTORED_VERSION in rows[0].reason
        latest = await operations.prepare_ledger(sheets, SHEET_ID)
        audit = await operations.audit_scope(
            config,
            directory,
            ou_path="/01 OTB",
            ledger_latest=latest,
            gmail_factory=pool.factory,
        )
        alice = {r["send_as_email"]: r for r in audit}[ALICE]
        assert alice["status"] == "stale_template"
        assert alice["ledger_template_version"] == operations.RESTORED_VERSION

    @pytest.mark.asyncio
    async def test_latest_row_wins_unless_a_run_id_is_named(self, pool):
        older = "<div>older</div>"
        sheets = _ledger_with_rows(
            [
                _applied_row(ALICE, older, "2026-09-20T09:00:00+00:00", "run-a"),
                _applied_row(ALICE, PREVIOUS, "2026-09-21T09:00:00+00:00", "run-b"),
            ]
        )
        rows = await _restore(pool, sheets=sheets)
        assert rows[0].after_hash == signature_hash(PREVIOUS)
        assert "run-b" in rows[0].reason
        rows = await _restore(pool, sheets=sheets, from_run_id="run-a")
        assert rows[0].after_hash == signature_hash(older)
        assert "run-a" in rows[0].reason
        with pytest.raises(UserInputError) as excinfo:
            await _restore(pool, sheets=sheets, from_run_id="run-zzz")
        message = str(excinfo.value)
        assert "run-zzz" in message and "run-b" in message and "run-a" in message
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_alias_and_unknown_send_as(self, pool):
        sheets = _ledger_with_rows(
            [
                _applied_row(
                    ALICE_JIT, OLD_ALIAS, "2026-09-20T09:00:00+00:00", "run-a", "JIT"
                )
            ]
        )
        rows = await _restore(pool, sheets=sheets, send_as_email=ALICE_JIT.upper())
        # Gmail already holds the previous alias signature: nothing to do.
        assert rows[0].action == "unchanged"
        assert rows[0].send_as_email == ALICE_JIT
        assert rows[0].entity == "JIT"
        with pytest.raises(UserInputError) as excinfo:
            await _restore(pool, sheets=sheets, send_as_email="x@otbgroup.co.uk")
        assert ALICE_JIT in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_empty_previous_signature_clears_the_address(self, pool):
        sheets = _ledger_with_rows(
            [_applied_row(ALICE, "", "2026-09-20T09:00:00+00:00", "run-a")]
        )
        rows = await _restore(pool, sheets=sheets, dry_run=False, confirm=True)
        assert rows[0].action == "applied"
        assert "clears" in rows[0].reason
        assert rows[0].after_hash == ""
        assert pool.mailboxes[ALICE].send_as[ALICE]["signature"] == ""
        assert sheets.ledger_rows()[-1]["rendered_hash"] == ""

    @pytest.mark.asyncio
    async def test_live_with_read_only_ledger_share_refuses_before_gmail(self, pool):
        sheets = _ledger_with_rows(
            [_applied_row(ALICE, PREVIOUS, "2026-09-20T09:00:00+00:00", "run-a")]
        )
        sheets.fail_writes = http_error(403, "forbidden")
        with pytest.raises(LedgerError):
            await _restore(pool, sheets=sheets, dry_run=False, confirm=True)
        assert pool.factory_calls == []
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_patch_failure_and_append_failure_are_error_rows(self, pool):
        sheets = _ledger_with_rows(
            [_applied_row(ALICE, PREVIOUS, "2026-09-20T09:00:00+00:00", "run-a")]
        )
        pool.mailboxes[ALICE].fail_patch_for[ALICE] = http_error(403, "forbidden")
        rows = await _restore(pool, sheets=sheets, dry_run=False, confirm=True)
        assert rows[0].action == "error"
        assert rows[0].reason.startswith("HttpError")
        assert len(sheets.ledger_rows()) == 1

        pool.mailboxes[ALICE].fail_patch_for.clear()
        sheets.fail_append = http_error(500, "backendError")
        rows = await _restore(pool, sheets=sheets, dry_run=False, confirm=True)
        assert rows[0].action == "error"
        assert operations.LEDGER_APPEND_FAILED_REASON in rows[0].reason
        assert pool.mailboxes[ALICE].send_as[ALICE]["signature"] == PREVIOUS

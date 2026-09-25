"""Tests for ``python -m gsignatures.audit_cli`` (the cron audit).

Covers exit codes (0 all in sync or unmanaged, 2 any drift, 1 fatal), the
one-scope rule, ``--no-report`` and ``--tab-prefix``, and that the CLI never
patches a signature. Clients are injected by replacing ``build_runtime``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gsignatures import audit_cli, operations, sa_auth  # noqa: E402
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
ALICE = "alice@otbgroup.co.uk"
ALICE_HOME = "alice@blakefamily.uk"
BOB = "bob@jit-logistics.com"
SIG = "<div>current</div>"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    monkeypatch.setattr("gdrive.drive_batch.asyncio.sleep", AsyncMock())


@pytest.fixture(scope="module")
def config():
    return load_config()


def _ledger_row(user_email, send_as_email, entity, readback):
    return [
        "2026-09-20T09:00:00+00:00",
        "oliver@otbgroup.co.uk",
        user_email,
        send_as_email,
        entity,
        "1.0.0",
        "1.0.0",
        "rendered",
        readback,
        "",
        "",
        "run-old",
    ]


@pytest.fixture
def world(config, monkeypatch):
    """A tenant where every managed address is in sync unless a test moves it."""
    pool = FakeGmailPool()
    pool.add(
        ALICE,
        [
            send_as(ALICE, primary=True, signature=SIG),
            send_as(ALICE_HOME, signature=""),
        ],
    )
    pool.add(BOB, [send_as(BOB, primary=True, signature=SIG)])
    directory = FakeDirectory(
        users=[
            user(ALICE, "/01 OTB", full="Alice Able", title="Director"),
            user(BOB, "/02 JIT", full="Bob Baker", title="Manager"),
        ],
        groups={"leads@otbgroup.co.uk": [{"email": BOB, "type": "USER"}]},
    )
    sheets = FakeSheets(
        {
            "Ledger": [
                LEDGER_HEADER,
                _ledger_row(ALICE, ALICE, "OTB", signature_hash(SIG)),
                _ledger_row(BOB, BOB, "JIT", signature_hash(SIG)),
            ]
        }
    )
    rt = operations.Runtime(
        config=config,
        directory=directory,
        sheets=sheets,
        sheet_id=SHEET_ID,
        gmail_factory=pool.factory,
    )
    monkeypatch.setattr(audit_cli, "build_runtime", lambda **kw: rt)
    return rt, pool, directory, sheets


def _today_tab(prefix="Audit"):
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"


class TestExitCodes:
    def test_all_in_sync_exits_zero_and_writes_report(self, world, capsys):
        rt, pool, directory, sheets = world
        code = audit_cli.main(["--all"])
        out = capsys.readouterr()
        assert code == 0
        assert "in_sync: 2" in out.out
        assert "unmanaged: 1" in out.out
        assert ALICE in out.out
        assert _today_tab() in sheets.tabs
        assert sheets.tabs[_today_tab()][0] == AUDIT_HEADER
        assert len(sheets.tabs[_today_tab()]) == 4
        assert pool.patch_calls() == []

    @pytest.mark.parametrize(
        "mutate",
        [
            # Gmail changed since the last apply.
            lambda rt, pool, sheets: (
                pool.mailboxes[ALICE]
                .send_as[ALICE]
                .__setitem__("signature", "<div>edited</div>")
            ),
            # Never applied: drop Bob's ledger row.
            lambda rt, pool, sheets: sheets.tabs["Ledger"].pop(2),
            # Stale template: the ledger has an older version.
            lambda rt, pool, sheets: sheets.tabs["Ledger"][1].__setitem__(
                LEDGER_HEADER.index("template_version"), "0.9.0"
            ),
            # Error: Bob's mailbox cannot be reached.
            lambda rt, pool, sheets: pool.fail_for.__setitem__(
                BOB, RuntimeError("delegation refused")
            ),
        ],
        ids=["changed_since_apply", "never_applied", "stale_template", "error"],
    )
    def test_any_drift_exits_two(self, world, capsys, mutate):
        rt, pool, directory, sheets = world
        mutate(rt, pool, sheets)
        code = audit_cli.main(["--all"])
        out = capsys.readouterr()
        assert code == 2
        assert "drift" in out.out.lower() or "drift" in out.err.lower()
        assert pool.patch_calls() == []
        # The report is still written so the drift is on record.
        assert _today_tab() in sheets.tabs

    def test_fatal_auth_error_exits_one_with_one_line(self, monkeypatch, capsys):
        def boom(**kwargs):
            raise sa_auth.SignatureAuthError("No signature service account configured.")

        monkeypatch.setattr(audit_cli, "build_runtime", boom)
        code = audit_cli.main(["--all"])
        out = capsys.readouterr()
        assert code == 1
        assert out.err.strip().count("\n") == 0
        assert "service account" in out.err

    def test_fatal_ledger_error_exits_one(self, monkeypatch, capsys):
        def boom(**kwargs):
            raise LedgerError("SIGNATURE_LEDGER_SHEET_ID is not set.")

        monkeypatch.setattr(audit_cli, "build_runtime", boom)
        assert audit_cli.main(["--all"]) == 1
        assert "SIGNATURE_LEDGER_SHEET_ID" in capsys.readouterr().err

    def test_unreadable_ledger_exits_one_before_any_gmail_call(self, world, capsys):
        rt, pool, directory, sheets = world
        sheets.fail_reads = http_error(500, "backendError")
        assert audit_cli.main(["--all"]) == 1
        assert "ledger" in capsys.readouterr().err.lower()
        assert pool.factory_calls == []

    def test_report_write_failure_exits_one(self, world, capsys):
        rt, pool, directory, sheets = world
        sheets.fail_writes = http_error(403, "forbidden")
        assert audit_cli.main(["--all"]) == 1
        assert "report" in capsys.readouterr().err.lower()


class TestArguments:
    def test_exactly_one_scope_required(self, world, capsys):
        assert audit_cli.main([]) == 1
        assert "scope" in capsys.readouterr().err.lower()
        assert audit_cli.main(["--all", "--ou", "/01 OTB"]) == 1

    def test_ou_scope(self, world, capsys):
        rt, pool, directory, sheets = world
        assert audit_cli.main(["--ou", "/02 JIT"]) == 0
        out = capsys.readouterr().out
        assert BOB in out and ALICE not in out
        assert "orgUnitPath='/02 JIT'" in directory.calls[0][1]["query"]

    def test_domain_scope(self, world, capsys):
        rt, pool, directory, sheets = world
        assert audit_cli.main(["--domain", "otbgroup.co.uk"]) == 0
        assert directory.calls[0][1]["domain"] == "otbgroup.co.uk"

    def test_group_scope(self, world, capsys):
        rt, pool, directory, sheets = world
        assert audit_cli.main(["--group", "leads@otbgroup.co.uk"]) == 0
        out = capsys.readouterr().out
        assert BOB in out and ALICE not in out

    def test_no_report_writes_nothing(self, world, capsys):
        rt, pool, directory, sheets = world
        assert audit_cli.main(["--all", "--no-report"]) == 0
        assert set(sheets.tabs) == {"Ledger"}
        assert "values.append" not in sheets.names()

    def test_tab_prefix(self, world, capsys):
        rt, pool, directory, sheets = world
        assert audit_cli.main(["--all", "--tab-prefix", "Weekly"]) == 0
        assert _today_tab("Weekly") in sheets.tabs

    def test_main_is_the_module_entrypoint(self):
        src = Path(audit_cli.__file__).read_text()
        assert 'if __name__ == "__main__":' in src
        assert "sys.exit(main())" in src

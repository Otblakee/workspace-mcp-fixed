"""Unit tests for the five MCP tools in ``gsignatures.signature_tools``.

Covers:

* the caller gate on every tool: no FastMCP context is refused, a caller off
  the allowlist is refused, an allowlisted caller proceeds, and no Google
  client is built before the gate passes;
* ``preview_email_signature``: entity, versions, person fields with the
  mobile shown as ``(none)``, the unverified-statutory warning, the HTML;
* ``get_email_signatures``: one block per send-as, drift from the ledger,
  and ``ledger unavailable`` (not a failure) when the ledger cannot be read;
* ``set_email_signature``: dry run by default with no patch, the live gate,
  the confirm path, one address at a time;
* ``apply_email_signatures``: header, table, JSONL access line, reminder;
* ``audit_email_signatures``: counts, table, the ``Audit_<date>`` tab;
* ``SignatureAuthError`` / ``LedgerError`` / ``SignatureConfigError`` reach
  the client as ``UserInputError``.

The tools are exercised through their innermost implementation (decorators
peeled), exactly as the other tool test modules do, so the gate and the error
mapping must live in the function body, not in a decorator.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.utils import UserInputError  # noqa: E402
from gsignatures import operations, sa_auth, signature_tools  # noqa: E402
from gsignatures.engine import SignatureConfigError, load_config, signature_hash  # noqa: E402
from gsignatures.ledger import AUDIT_HEADER, LEDGER_HEADER, LedgerError  # noqa: E402
from tests.gsignatures.fakes import (  # noqa: E402
    FakeDirectory,
    FakeGmailPool,
    FakeSheets,
    http_error,
    send_as,
    user,
)


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


preview_email_signature = _unwrap(signature_tools.preview_email_signature)
get_email_signatures = _unwrap(signature_tools.get_email_signatures)
set_email_signature = _unwrap(signature_tools.set_email_signature)
apply_email_signatures = _unwrap(signature_tools.apply_email_signatures)
audit_email_signatures = _unwrap(signature_tools.audit_email_signatures)

SHEET_ID = "ledger-sheet"
OWNER = "oliver@otbgroup.co.uk"
ALICE = "alice@otbgroup.co.uk"
ALICE_JIT = "alice@jit-logistics.com"
ALICE_HOME = "alice@blakefamily.uk"
BOB = "bob@jit-logistics.com"
CAROL = "carol@otbgroup.co.uk"
OLD_PRIMARY = "<div>old primary</div>"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    monkeypatch.setattr("gdrive.drive_batch.asyncio.sleep", AsyncMock())


@pytest.fixture(autouse=True)
def _isolated_attachment_dir(tmp_path, monkeypatch):
    import core.attachment_storage as storage_mod

    monkeypatch.setenv("WORKSPACE_ATTACHMENT_DIR", str(tmp_path))
    monkeypatch.setattr(storage_mod, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(storage_mod, "_attachment_storage", None)
    yield


@pytest.fixture(autouse=True)
def _default_allowlist(monkeypatch):
    monkeypatch.delenv(sa_auth.ENV_ADMIN_EMAILS, raising=False)


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture
def pool():
    pool = FakeGmailPool()
    pool.add(
        ALICE,
        [
            send_as(ALICE, primary=True, signature=OLD_PRIMARY),
            send_as(ALICE_JIT, signature=""),
            send_as(ALICE_HOME, signature=""),
        ],
    )
    pool.add(BOB, [send_as(BOB, primary=True, signature="")])
    pool.add(CAROL, [send_as(CAROL, primary=True, signature="")])
    return pool


@pytest.fixture
def directory():
    return FakeDirectory(
        users=[
            user(ALICE, "/01 OTB/Exec", full="Alice Able", title="Director"),
            user(
                BOB,
                "/02 JIT",
                full="Bob Baker",
                title="Operations Manager",
                phones=[{"type": "mobile", "value": "07700 900123"}],
            ),
            user(CAROL, "/01 OTB", full="Carol Cole", title=None),
        ],
        groups={"leads@otbgroup.co.uk": [{"email": ALICE, "type": "USER"}]},
    )


@pytest.fixture
def sheets():
    return FakeSheets({"Ledger": [LEDGER_HEADER]})


@pytest.fixture
def runtime(config, directory, sheets, pool, monkeypatch):
    rt = operations.Runtime(
        config=config,
        directory=directory,
        sheets=sheets,
        sheet_id=SHEET_ID,
        gmail_factory=pool.factory,
    )
    calls = []

    def build(**kwargs):
        calls.append(kwargs)
        return rt

    monkeypatch.setattr(signature_tools, "build_runtime", build)
    rt.build_calls = calls  # type: ignore[attr-defined]
    return rt


@pytest.fixture
def as_owner(monkeypatch):
    monkeypatch.setattr(
        signature_tools, "_resolve_caller_email", AsyncMock(return_value=OWNER)
    )


def _tool_calls():
    """Every tool with the minimal arguments that reach the gate."""
    return [
        ("preview", lambda: preview_email_signature(ALICE)),
        ("get", lambda: get_email_signatures(ALICE)),
        ("set", lambda: set_email_signature(ALICE)),
        ("apply", lambda: apply_email_signatures(ou_path="/01 OTB")),
        ("audit", lambda: audit_email_signatures(ou_path="/01 OTB")),
    ]


# ---------------------------------------------------------------------------
# Caller gate
# ---------------------------------------------------------------------------


class TestCallerGate:
    @pytest.mark.asyncio
    async def test_resolver_returns_none_without_a_context(self):
        # No FastMCP request is live under pytest.
        assert await signature_tools._resolve_caller_email() is None

    @pytest.mark.parametrize("name,call", _tool_calls())
    @pytest.mark.asyncio
    async def test_no_context_is_refused(self, runtime, monkeypatch, name, call):
        monkeypatch.setattr(
            signature_tools, "_resolve_caller_email", AsyncMock(return_value=None)
        )
        with pytest.raises(UserInputError) as excinfo:
            await call()
        assert "authenticated caller" in str(excinfo.value)
        assert runtime.build_calls == []

    @pytest.mark.parametrize("name,call", _tool_calls())
    @pytest.mark.asyncio
    async def test_empty_email_is_refused(self, runtime, monkeypatch, name, call):
        monkeypatch.setattr(
            signature_tools, "_resolve_caller_email", AsyncMock(return_value="  ")
        )
        with pytest.raises(UserInputError):
            await call()
        assert runtime.build_calls == []

    @pytest.mark.parametrize("name,call", _tool_calls())
    @pytest.mark.asyncio
    async def test_non_allowlisted_is_refused(self, runtime, monkeypatch, name, call):
        monkeypatch.setattr(
            signature_tools,
            "_resolve_caller_email",
            AsyncMock(return_value="mallory@otbgroup.co.uk"),
        )
        with pytest.raises(UserInputError) as excinfo:
            await call()
        assert "not allowed" in str(excinfo.value)
        assert sa_auth.ENV_ADMIN_EMAILS in str(excinfo.value)
        assert runtime.build_calls == []

    @pytest.mark.parametrize("name,call", _tool_calls())
    @pytest.mark.asyncio
    async def test_allowlisted_proceeds(self, runtime, as_owner, name, call):
        result = await call()
        assert isinstance(result, str) and result
        assert len(runtime.build_calls) == 1

    @pytest.mark.asyncio
    async def test_allowlist_is_case_insensitive_and_env_driven(
        self, runtime, monkeypatch
    ):
        monkeypatch.setattr(
            signature_tools,
            "_resolve_caller_email",
            AsyncMock(return_value="Oliver@OTBGroup.co.uk"),
        )
        assert await preview_email_signature(ALICE)
        monkeypatch.setenv(sa_auth.ENV_ADMIN_EMAILS, "someone.else@otbgroup.co.uk")
        with pytest.raises(UserInputError):
            await preview_email_signature(ALICE)


# ---------------------------------------------------------------------------
# preview_email_signature
# ---------------------------------------------------------------------------


class TestPreview:
    @pytest.mark.asyncio
    async def test_primary_preview(self, runtime, as_owner, pool):
        out = await preview_email_signature(ALICE)
        assert ALICE in out
        assert "Entity: OTB" in out
        assert "template 1.0.0" in out and "statutory 1.0.0" in out
        assert "Alice Able" in out
        assert "Director" in out
        assert "Mobile: (none)" in out
        assert "statutory_verified" in out and "WARNING" in out
        assert "<table" in out
        assert pool.patch_calls() == []
        # Read-only: the ledger is not needed for a preview.
        assert runtime.build_calls[0].get("need_ledger") is False

    @pytest.mark.asyncio
    async def test_alias_preview_uses_alias_entity_and_address(self, runtime, as_owner):
        out = await preview_email_signature(ALICE, send_as_email=ALICE_JIT)
        assert "Entity: JIT" in out
        assert ALICE_JIT in out
        assert "<table" in out

    @pytest.mark.asyncio
    async def test_mobile_shown_when_present(self, runtime, as_owner):
        out = await preview_email_signature(BOB)
        assert "Mobile: 07700 900123" in out

    @pytest.mark.asyncio
    async def test_unmanaged_alias_has_no_html(self, runtime, as_owner):
        out = await preview_email_signature(ALICE, send_as_email=ALICE_HOME)
        assert "not managed" in out.lower()
        assert "blakefamily.uk" in out
        assert "<table" not in out

    @pytest.mark.asyncio
    async def test_plan_error_is_reported(self, runtime, as_owner):
        out = await preview_email_signature(CAROL)
        assert "title" in out
        assert "<table" not in out

    @pytest.mark.asyncio
    async def test_unknown_send_as_is_a_user_input_error(self, runtime, as_owner):
        with pytest.raises(UserInputError):
            await preview_email_signature(ALICE, send_as_email="x@otbgroup.co.uk")

    @pytest.mark.asyncio
    async def test_blank_user_email_is_refused(self, runtime, as_owner):
        with pytest.raises(UserInputError):
            await preview_email_signature("  ")


# ---------------------------------------------------------------------------
# get_email_signatures
# ---------------------------------------------------------------------------


class TestGetEmailSignatures:
    @pytest.mark.asyncio
    async def test_one_block_per_send_as(self, runtime, as_owner, sheets):
        row = dict(
            zip(
                LEDGER_HEADER,
                [
                    "2026-09-20T09:00:00+00:00",
                    OWNER,
                    ALICE,
                    ALICE,
                    "OTB",
                    "1.0.0",
                    "1.0.0",
                    "r",
                    signature_hash(OLD_PRIMARY),
                    "",
                    "",
                    "run-old",
                ],
            )
        )
        sheets.tabs["Ledger"].append([row[k] for k in LEDGER_HEADER])
        out = await get_email_signatures(ALICE)
        assert out.count("- ") >= 3
        assert "primary" in out and "default" in out
        assert signature_hash(OLD_PRIMARY)[:12] in out
        assert "(empty)" in out
        assert "OTB" in out and "JIT" in out
        assert "blakefamily.uk" in out
        assert "in_sync" in out
        assert "never_applied" in out
        assert "unmanaged" in out
        assert "template 1.0.0" in out

    @pytest.mark.asyncio
    async def test_ledger_unavailable_is_reported_not_raised(
        self, runtime, as_owner, sheets
    ):
        sheets.fail_reads = http_error(500, "backendError")
        out = await get_email_signatures(ALICE)
        assert "ledger unavailable" in out
        assert "OTB" in out

    @pytest.mark.asyncio
    async def test_no_ledger_configured_is_reported(self, runtime, as_owner):
        runtime.sheets = None
        runtime.sheet_id = None
        out = await get_email_signatures(ALICE)
        assert "ledger" in out.lower()
        assert "OTB" in out


# ---------------------------------------------------------------------------
# set_email_signature
# ---------------------------------------------------------------------------


class TestSetEmailSignature:
    @pytest.mark.asyncio
    async def test_default_is_dry_run_on_the_primary(self, runtime, as_owner, pool):
        out = await set_email_signature(ALICE)
        assert "DRY RUN" in out
        assert "would_apply" in out
        assert ALICE in out and ALICE_JIT not in out
        assert "confirm=True" in out
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_alias_only(self, runtime, as_owner, pool):
        out = await set_email_signature(ALICE, send_as_email=ALICE_JIT)
        assert ALICE_JIT in out
        assert "JIT" in out
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_live_without_confirm_is_refused(self, runtime, as_owner, pool):
        with pytest.raises(UserInputError) as excinfo:
            await set_email_signature(ALICE, dry_run=False)
        assert "confirm=True" in str(excinfo.value)
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_live_with_confirm_applies_and_records(
        self, runtime, as_owner, pool, sheets
    ):
        out = await set_email_signature(ALICE, dry_run=False, confirm=True)
        assert "LIVE" in out
        assert "applied" in out
        assert pool.patch_calls() == [(ALICE, ALICE)]
        rows = sheets.ledger_rows()
        assert len(rows) == 1
        assert rows[0]["actor"] == OWNER
        assert rows[0]["previous_signature_html"] == OLD_PRIMARY
        assert rows[0]["run_id"] in out

    @pytest.mark.asyncio
    async def test_force_is_passed_through(self, runtime, as_owner):
        out = await set_email_signature(ALICE, force=True)
        assert "force" in out


# ---------------------------------------------------------------------------
# apply_email_signatures
# ---------------------------------------------------------------------------


class TestApplyEmailSignatures:
    @pytest.mark.asyncio
    async def test_dry_run_output_shape(self, runtime, as_owner, pool):
        out = await apply_email_signatures(ou_path="/01 OTB")
        head = out.splitlines()[0]
        assert "OU /01 OTB" in head
        assert "run_id" in head
        assert "dry run" in head.lower()
        assert OWNER in head
        assert "user_email" in out and "would_apply" in out
        assert "signatures-dryrun-" in out
        assert "evidence" in out.lower()
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_no_scope_is_refused(self, runtime, as_owner):
        with pytest.raises(UserInputError):
            await apply_email_signatures()

    @pytest.mark.asyncio
    async def test_live_run(self, runtime, as_owner, pool, sheets):
        out = await apply_email_signatures(
            group_email="leads@otbgroup.co.uk", dry_run=False, confirm=True
        )
        assert "LIVE" in out.splitlines()[0]
        assert "applied" in out
        assert "signatures-apply-" in out
        assert set(pool.patch_calls()) == {(ALICE, ALICE), (ALICE, ALICE_JIT)}
        assert len(sheets.ledger_rows()) == 2

    @pytest.mark.asyncio
    async def test_max_users_refusal(self, runtime, as_owner, pool):
        with pytest.raises(UserInputError) as excinfo:
            await apply_email_signatures(domain="otbgroup.co.uk", max_users=1)
        assert "max_users" in str(excinfo.value)
        assert pool.factory_calls == []


# ---------------------------------------------------------------------------
# audit_email_signatures
# ---------------------------------------------------------------------------


class TestAuditEmailSignatures:
    @pytest.mark.asyncio
    async def test_counts_and_table(self, runtime, as_owner, pool, sheets):
        out = await audit_email_signatures(ou_path="/01 OTB")
        assert "never_applied: 2" in out
        assert "unmanaged: 1" in out
        assert "error: 1" in out
        assert "in_sync: 0" in out
        for column in ("user", "send_as", "entity", "status", "reason"):
            assert column in out
        assert pool.patch_calls() == []
        # No report tab unless asked for.
        assert set(sheets.tabs) == {"Ledger"}

    @pytest.mark.asyncio
    async def test_write_report_creates_dated_tab(self, runtime, as_owner, sheets):
        out = await audit_email_signatures(all_users=True, write_report=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tab = f"Audit_{today}"
        assert tab in sheets.tabs
        assert sheets.tabs[tab][0] == AUDIT_HEADER
        assert len(sheets.tabs[tab]) > 1
        assert tab in out

    @pytest.mark.asyncio
    async def test_no_scope_is_refused(self, runtime, as_owner):
        with pytest.raises(UserInputError):
            await audit_email_signatures()

    @pytest.mark.asyncio
    async def test_unreadable_ledger_is_a_user_input_error(
        self, runtime, as_owner, sheets
    ):
        sheets.fail_reads = http_error(500, "backendError")
        with pytest.raises(UserInputError) as excinfo:
            await audit_email_signatures(ou_path="/01 OTB")
        assert "ledger" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


class TestErrorMapping:
    @pytest.mark.parametrize(
        "error",
        [
            sa_auth.SignatureAuthError("no service account key"),
            LedgerError("no ledger sheet"),
            SignatureConfigError("bad entities.yaml"),
        ],
    )
    @pytest.mark.asyncio
    async def test_setup_errors_become_user_input_errors(
        self, as_owner, monkeypatch, error
    ):
        def boom(**kwargs):
            raise error

        monkeypatch.setattr(signature_tools, "build_runtime", boom)
        with pytest.raises(UserInputError) as excinfo:
            await set_email_signature(ALICE)
        assert str(error) in str(excinfo.value)
        assert excinfo.value.__cause__ is error

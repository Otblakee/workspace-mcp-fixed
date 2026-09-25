"""Unit tests for the six MCP tools in ``gsignatures.signature_tools``.

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
* ``restore_email_signature``: dry run by default, the live path, run_id;
* ``--read-only`` mode: the write tools refuse a live run and still dry-run;
* the switch checks run before the runtime is built, so the caller sees the
  right refusal even when no ledger or key is configured;
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

from auth.scopes import set_read_only  # noqa: E402
from core.utils import UserInputError  # noqa: E402
from gsignatures import operations, sa_auth, signature_tools  # noqa: E402
from gsignatures.engine import (  # noqa: E402
    SignatureConfigError,
    load_config,
    plan_for_user,
    signature_hash,
)
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
restore_email_signature = _unwrap(signature_tools.restore_email_signature)

SHEET_ID = "ledger-sheet"
OWNER = "oliver@otbgroup.co.uk"
ALICE = "alice@otbgroup.co.uk"
ALICE_JIT = "alice@jit-logistics.com"
ALICE_HOME = "alice@blakefamily.uk"
BOB = "bob@jit-logistics.com"
CAROL = "carol@otbgroup.co.uk"
EMILY = "emily@arthistorywithemily.co.uk"
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
    pool.add(EMILY, [send_as(EMILY, primary=True, signature="")])
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
            user(
                EMILY,
                "/99 _SYSTEM/Personal",
                given="Emily",
                family="Example",
                full="Emily Example",
                title="Art Historian",
            ),
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
        ("restore", lambda: restore_email_signature(ALICE)),
    ]


@pytest.fixture
def ledger_row_for_alice(sheets):
    """A ledger row for Alice's primary that says OLD_PRIMARY was our apply."""
    row = [
        "2026-09-20T09:00:00+00:00",
        OWNER,
        ALICE,
        ALICE,
        "OTB",
        "1.0.0",
        "1.0.0",
        "rendered-old",
        signature_hash(OLD_PRIMARY),
        "",
        "<div>before the rollout</div>",
        "run-old",
    ]
    sheets.tabs["Ledger"].append(row)
    return row


class _StubContext:
    """Stands in for the FastMCP request context in the resolver tests."""

    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.keys = []

    async def get_state(self, key):
        self.keys.append(key)
        if self.error is not None:
            raise self.error
        return self.value


@pytest.fixture
def context_owner(monkeypatch):
    """A live FastMCP context whose authenticated user is the owner."""
    import fastmcp.server.dependencies as deps

    ctx = _StubContext(value=OWNER)
    monkeypatch.setattr(deps, "get_context", lambda: ctx)
    return ctx


@pytest.fixture
def read_only_server():
    set_read_only(True)
    try:
        yield
    finally:
        set_read_only(False)


# ---------------------------------------------------------------------------
# Caller gate
# ---------------------------------------------------------------------------


class TestCallerGate:
    @pytest.mark.asyncio
    async def test_resolver_returns_none_without_a_context(self):
        # No FastMCP request is live under pytest.
        assert await signature_tools._resolve_caller_email() is None

    @pytest.mark.asyncio
    async def test_resolver_reads_authenticated_user_email_from_context(
        self, context_owner
    ):
        assert await signature_tools._resolve_caller_email() == OWNER
        assert context_owner.keys == ["authenticated_user_email"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["", None])
    async def test_resolver_returns_none_when_state_is_empty(self, monkeypatch, state):
        import fastmcp.server.dependencies as deps

        monkeypatch.setattr(deps, "get_context", lambda: _StubContext(value=state))
        monkeypatch.setenv("DEFAULT_USER", "oli")
        assert await signature_tools._resolve_caller_email() is None

    @pytest.mark.asyncio
    async def test_resolver_returns_none_when_get_state_raises(self, monkeypatch):
        import fastmcp.server.dependencies as deps

        monkeypatch.setattr(
            deps,
            "get_context",
            lambda: _StubContext(error=RuntimeError("state store gone")),
        )
        monkeypatch.setenv("DEFAULT_USER", "oli")
        assert await signature_tools._resolve_caller_email() is None

    @pytest.mark.asyncio
    async def test_resolver_returns_none_when_context_is_none(self, monkeypatch):
        import fastmcp.server.dependencies as deps

        monkeypatch.setattr(deps, "get_context", lambda: None)
        assert await signature_tools._resolve_caller_email() is None

    @pytest.mark.asyncio
    async def test_resolver_never_falls_back_to_default_user(self, monkeypatch):
        """core.audit falls back to DEFAULT_USER; the gate must not."""
        import fastmcp.server.dependencies as deps

        monkeypatch.setenv("DEFAULT_USER", OWNER)
        monkeypatch.setattr(deps, "get_context", lambda: _StubContext(value=""))
        assert await signature_tools._resolve_caller_email() is None
        with pytest.raises(UserInputError):
            await preview_email_signature(ALICE)

    @pytest.mark.parametrize("name,call", _tool_calls())
    @pytest.mark.asyncio
    async def test_gate_passes_through_the_real_resolver(
        self, runtime, context_owner, ledger_row_for_alice, name, call
    ):
        """Every tool, through the real resolver against a stub context that
        carries the owner's identity: the gate opens and the tool runs."""
        result = await call()
        assert isinstance(result, str) and result
        assert len(runtime.build_calls) == 1
        assert context_owner.keys == ["authenticated_user_email"]

    @pytest.mark.parametrize("name,call", _tool_calls())
    @pytest.mark.asyncio
    async def test_gate_refuses_a_stranger_through_the_real_resolver(
        self, runtime, monkeypatch, name, call
    ):
        import fastmcp.server.dependencies as deps

        monkeypatch.setattr(
            deps, "get_context", lambda: _StubContext(value="mallory@otbgroup.co.uk")
        )
        with pytest.raises(UserInputError):
            await call()
        assert runtime.build_calls == []

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
    async def test_allowlisted_proceeds(
        self, runtime, as_owner, ledger_row_for_alice, name, call
    ):
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
    async def test_ahwe_preview_is_ring_fenced(self, runtime, as_owner):
        """The whole preview text for the AHWE mailbox names no group company,
        not only the template: a shared strapline in the preview layout would
        leak group branding into the ring-fenced entity."""
        from tests.gsignatures.test_engine import _assert_ring_fenced

        out = await preview_email_signature(EMILY)
        assert "Entity: AHWE (Art History with Emily)" in out
        assert "Emily Example" in out and "Art Historian" in out
        assert "<table" in out
        # Every line apart from the entity line, and the entity line itself
        # once its legal name (the one permitted mention) is removed.
        for line in out.splitlines():
            if line.strip().startswith("Entity:"):
                line = line.replace("Art History with Emily", "")
            _assert_ring_fenced(line, f"AHWE preview line {line!r}")

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
    async def test_one_block_per_send_as(
        self, runtime, as_owner, sheets, config, directory, pool
    ):
        plan = plan_for_user(
            config,
            directory.users_by_email[ALICE],
            list(pool.mailboxes[ALICE].send_as.values()),
        )[0]
        assert plan.send_as_email == ALICE
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
                    plan.rendered_hash,
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

    @pytest.mark.asyncio
    async def test_no_ledger_tab_reports_never_applied(self, runtime, as_owner, sheets):
        """RUNBOOK step 6.2: on a Sheet nothing has written yet (no Ledger tab)
        every managed address reads never_applied, not 'ledger unavailable'."""
        sheets.tabs.clear()
        sheets.tabs["Sheet1"] = [[]]
        out = await get_email_signatures(ALICE)
        assert "ledger unavailable" not in out
        assert out.count("never_applied") == 2
        assert "unmanaged" in out
        assert "Ledger" not in sheets.tabs  # a read never creates the tab


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

    @pytest.mark.asyncio
    async def test_confirm_alone_is_still_a_dry_run(
        self, runtime, as_owner, pool, sheets
    ):
        out = await set_email_signature(ALICE, confirm=True)
        assert out.startswith("DRY RUN")
        assert "would_apply" in out
        assert pool.patch_calls() == []
        assert sheets.ledger_rows() == []
        assert runtime.build_calls[0]["need_ledger"] is False

    @pytest.mark.asyncio
    async def test_live_without_confirm_refuses_before_building_the_runtime(
        self, runtime, as_owner, monkeypatch
    ):
        """With no ledger configured the caller must still see the confirm
        message, not a LedgerError from build_runtime."""

        def boom(**kwargs):
            raise LedgerError("SIGNATURE_LEDGER_SHEET_ID is not set.")

        monkeypatch.setattr(signature_tools, "build_runtime", boom)
        with pytest.raises(UserInputError) as excinfo:
            await set_email_signature(ALICE, dry_run=False)
        assert str(excinfo.value) == operations.LIVE_CONFIRM_MESSAGE
        assert runtime.build_calls == []

    @pytest.mark.asyncio
    async def test_dry_run_with_unreadable_ledger_says_so(
        self, runtime, as_owner, pool, sheets
    ):
        sheets.fail_reads = http_error(500, "backendError")
        out = await set_email_signature(ALICE)
        lines = out.splitlines()
        assert lines[0].startswith("DRY RUN")
        assert lines[1].startswith("Ledger: ledger unavailable (HttpError")
        assert "Drift was not judged" in lines[1]
        assert "would_apply" in out
        # The row's reason carries the note too, as the table is the evidence.
        table_row = next(ln for ln in lines if ln.startswith(ALICE))
        assert "ledger unavailable" in table_row
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_dry_run_with_readable_ledger_has_no_ledger_line(
        self, runtime, as_owner
    ):
        out = await set_email_signature(ALICE)
        assert not any(ln.startswith("Ledger:") for ln in out.splitlines())

    @pytest.mark.asyncio
    async def test_read_only_server_refuses_a_live_write_and_allows_dry_run(
        self, runtime, as_owner, pool, sheets, read_only_server
    ):
        with pytest.raises(UserInputError) as excinfo:
            await set_email_signature(ALICE, dry_run=False, confirm=True)
        assert "read-only" in str(excinfo.value)
        assert runtime.build_calls == []
        assert pool.patch_calls() == []
        assert sheets.ledger_rows() == []
        out = await set_email_signature(ALICE)
        assert out.startswith("DRY RUN")


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

    @pytest.mark.asyncio
    async def test_confirm_alone_is_still_a_dry_run(
        self, runtime, as_owner, pool, sheets
    ):
        out = await apply_email_signatures(ou_path="/01 OTB", confirm=True)
        assert "DRY RUN" in out.splitlines()[0]
        assert "signatures-dryrun-" in out
        assert pool.patch_calls() == []
        assert sheets.ledger_rows() == []

    @pytest.mark.asyncio
    async def test_scope_and_confirm_checked_before_building_the_runtime(
        self, runtime, as_owner, monkeypatch
    ):
        def boom(**kwargs):
            raise sa_auth.SignatureAuthError("No signature service account configured.")

        monkeypatch.setattr(signature_tools, "build_runtime", boom)
        with pytest.raises(UserInputError) as excinfo:
            await apply_email_signatures(ou_path="/01 OTB", domain="otbgroup.co.uk")
        assert "exactly one scope" in str(excinfo.value)
        with pytest.raises(UserInputError) as excinfo:
            await apply_email_signatures(ou_path="/01 OTB", dry_run=False)
        assert str(excinfo.value) == operations.LIVE_CONFIRM_MESSAGE
        assert runtime.build_calls == []

    @pytest.mark.asyncio
    async def test_dry_run_with_unreadable_ledger_says_so(
        self, runtime, as_owner, sheets
    ):
        sheets.fail_reads = http_error(500, "backendError")
        out = await apply_email_signatures(ou_path="/01 OTB")
        lines = out.splitlines()
        assert lines[1].startswith("Ledger: ledger unavailable (HttpError")
        assert "Drift was not judged" in lines[1]
        table_row = next(ln for ln in lines if ln.startswith(ALICE + " "))
        assert "ledger unavailable" in table_row

    @pytest.mark.asyncio
    async def test_live_run_ledger_failure_mid_run_is_flagged(
        self, runtime, as_owner, pool, sheets
    ):
        sheets.fail_append = http_error(500, "backendError")
        out = await apply_email_signatures(
            group_email="leads@otbgroup.co.uk", dry_run=False, confirm=True
        )
        assert "LEDGER FAILED MID-RUN" in out
        assert operations.LEDGER_FAILED_REASON in out
        assert pool.patch_calls() == [(ALICE, ALICE)]

    @pytest.mark.asyncio
    async def test_read_only_server_refuses_a_live_write_and_allows_dry_run(
        self, runtime, as_owner, pool, read_only_server
    ):
        with pytest.raises(UserInputError) as excinfo:
            await apply_email_signatures(
                group_email="leads@otbgroup.co.uk", dry_run=False, confirm=True
            )
        assert "read-only" in str(excinfo.value)
        assert runtime.build_calls == []
        assert pool.patch_calls() == []
        out = await apply_email_signatures(ou_path="/01 OTB")
        assert "DRY RUN" in out.splitlines()[0]


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

    def test_docstring_lists_every_status(self):
        doc = signature_tools.audit_email_signatures.__doc__ or ""
        doc = doc if doc.strip() else audit_email_signatures.__doc__
        for status in operations.AUDIT_STATUSES:
            assert status in doc, status


# ---------------------------------------------------------------------------
# restore_email_signature
# ---------------------------------------------------------------------------


class TestRestoreEmailSignature:
    @pytest.mark.asyncio
    async def test_default_is_dry_run(
        self, runtime, as_owner, pool, sheets, ledger_row_for_alice
    ):
        out = await restore_email_signature(ALICE)
        assert out.startswith("RESTORE DRY RUN")
        assert "would_apply" in out
        assert "run-old" in out
        assert "restored" in out
        assert "confirm=True" in out
        assert pool.patch_calls() == []
        assert len(sheets.ledger_rows()) == 1
        # Even a dry run needs the ledger: the row being restored lives there.
        assert runtime.build_calls[0]["need_ledger"] is True

    @pytest.mark.asyncio
    async def test_confirm_alone_is_still_a_dry_run(
        self, runtime, as_owner, pool, sheets, ledger_row_for_alice
    ):
        out = await restore_email_signature(ALICE, confirm=True)
        assert out.startswith("RESTORE DRY RUN")
        assert pool.patch_calls() == []
        assert len(sheets.ledger_rows()) == 1

    @pytest.mark.asyncio
    async def test_live_without_confirm_is_refused(
        self, runtime, as_owner, pool, ledger_row_for_alice
    ):
        with pytest.raises(UserInputError) as excinfo:
            await restore_email_signature(ALICE, dry_run=False)
        assert str(excinfo.value) == operations.LIVE_CONFIRM_MESSAGE
        assert runtime.build_calls == []
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_live_restores_and_records(
        self, runtime, as_owner, pool, sheets, ledger_row_for_alice
    ):
        out = await restore_email_signature(ALICE, dry_run=False, confirm=True)
        assert out.startswith("RESTORE LIVE")
        assert "applied" in out
        assert pool.patch_calls() == [(ALICE, ALICE)]
        assert pool.mailboxes[ALICE].send_as[ALICE]["signature"] == (
            "<div>before the rollout</div>"
        )
        rows = sheets.ledger_rows()
        assert len(rows) == 2
        assert rows[-1]["template_version"] == operations.RESTORED_VERSION
        assert rows[-1]["previous_signature_html"] == OLD_PRIMARY
        assert rows[-1]["actor"] == OWNER
        assert rows[-1]["run_id"] in out

    @pytest.mark.asyncio
    async def test_run_id_is_passed_through(
        self, runtime, as_owner, pool, ledger_row_for_alice
    ):
        out = await restore_email_signature(ALICE, run_id="run-old")
        assert "run-old" in out
        with pytest.raises(UserInputError) as excinfo:
            await restore_email_signature(ALICE, run_id="run-nope")
        assert "run-nope" in str(excinfo.value) and "run-old" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_no_ledger_row_is_a_user_input_error(self, runtime, as_owner, pool):
        with pytest.raises(UserInputError) as excinfo:
            await restore_email_signature(ALICE, send_as_email=ALICE_JIT)
        assert "no row" in str(excinfo.value)
        assert pool.patch_calls() == []

    @pytest.mark.asyncio
    async def test_read_only_server_refuses_a_live_restore(
        self, runtime, as_owner, pool, ledger_row_for_alice, read_only_server
    ):
        with pytest.raises(UserInputError) as excinfo:
            await restore_email_signature(ALICE, dry_run=False, confirm=True)
        assert "read-only" in str(excinfo.value)
        assert runtime.build_calls == []
        assert pool.patch_calls() == []
        assert (await restore_email_signature(ALICE)).startswith("RESTORE DRY RUN")

    @pytest.mark.asyncio
    async def test_blank_user_email_is_refused(self, runtime, as_owner):
        with pytest.raises(UserInputError):
            await restore_email_signature(" ")


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

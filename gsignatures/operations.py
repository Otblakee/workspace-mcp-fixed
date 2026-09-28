"""
The operations behind the signature tools and the audit CLI.

Everything here is async and transport-agnostic: no FastMCP, no argparse,
no environment reads except in ``build_runtime``. Every function takes its
Google clients as arguments (a Directory client, a Sheets client and a
``gmail_factory`` that returns a Gmail client for one user), so the whole
module is testable with fakes. The MCP tool layer
(``gsignatures/signature_tools.py``) and the cron CLI
(``gsignatures/audit_cli.py``) are thin wrappers over this module.

Rules carried by this module (owner decisions, see CLAUDE.md):

* Writes default to a dry run. A live write needs ``dry_run=False`` AND
  ``confirm=True``; anything else is refused before any Google call.
* The ledger is the evidence. A live run needs a reachable ledger Sheet
  (readable and writable, proved by re-writing the header row unchanged)
  BEFORE the first Gmail write; if the ledger cannot be prepared the run is
  refused and nothing is written. Once a ledger append fails mid-run, no
  further address in that run is patched: each becomes an ``error`` row
  saying it was not attempted, in this user and every later one.
* Drift and "unchanged" are judged against the ledger's read-back hash
  (what Gmail returned right after the last apply), never against a fresh
  render, because Gmail sanitises what it stores. A dry run whose ledger
  could not be read says so in every ``would_apply`` reason rather than
  claiming the address was never applied.
* A restore (``restore_user`` for one address, ``restore_scope`` for every
  address a run touched inside one scope) puts back the
  ``previous_signature_html`` a ledger row recorded, under the same dry-run
  and confirm rule, and records it the same way as an apply: a pending
  ledger row first, then the Gmail patch, then the completed row, both
  versions set to ``restored`` so the next apply and audit see the managed
  signature is not in place.
* A live run over more than one user (``apply_scope``, ``restore_scope``)
  also needs ``expected_users`` equal to the user count the preceding dry
  run printed; a mismatch is refused before any write, with the current
  count. A single-user run (and the single-address tools) needs no count.
* Isolation: one failing send-as address becomes an ``error`` row and the
  rest of the user continues; one failing user becomes an ``error`` row
  and the rest of the scope continues.
* Scopes are explicit and singular: exactly one of OU, domain, group (or,
  for audits, all users). A scope larger than ``max_users`` is refused
  with the count, never silently truncated.
* Audits never write a signature. ``audit_scope`` reads Gmail only.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from googleapiclient.errors import HttpError

from core.utils import UserInputError
from gdrive.drive_batch import write_jsonl_report

from gsignatures import clients, sa_auth
from gsignatures.engine import (
    APPLY_INTERRUPTED,
    PENDING_READBACK,
    is_pending_ledger_row,
    PlannedSignature,
    ResultRow,
    SignatureConfig,
    SignatureConfigError,
    drift_status,
    load_config,
    plan_for_user,
    result_rows_as_dicts,
    signature_hash,
)
from gsignatures.ledger import (
    LEDGER_HEADER,
    LEDGER_TAB,
    LedgerError,
    append_ledger_rows,
    outranks,
    assert_tab_writable,
    ensure_tab,
    ledger_key,
    ledger_sheet_id,
    ledger_tab_exists,
    read_ledger_latest,
    read_ledger_rows,
)

logger = logging.getLogger(__name__)

GmailFactory = Callable[[str], Any]
LedgerLatest = Dict[Tuple[str, str], Dict[str, str]]

# The exact instruction a caller sees when a live run is attempted without
# the second switch. Tested verbatim.
LIVE_CONFIRM_MESSAGE = (
    "Live run refused: a live write needs dry_run=False together with "
    "confirm=True. Nothing was changed. Run with the default dry_run=True "
    "first and check the table, then repeat with dry_run=False, confirm=True."
)

# Placeholder send-as value for a row that describes a whole user (the user
# could not be planned at all), so the table still names the user.
USER_LEVEL_SEND_AS = "*"

DEFAULT_MAX_USERS = 200

# Recorded as both versions on the ledger row a restore writes. It can never
# equal a pinned semver, so the next apply re-applies and the next audit
# reports stale_template for a restored address, which is the truth: the
# managed signature is not in place.
RESTORED_VERSION = "restored"

# A live scope run over more than one user must name the user count the
# preceding dry run printed. Tested verbatim (after formatting).
EXPECTED_USERS_MESSAGE = (
    "Live scope run refused: scope {label} currently covers {count} users, and "
    "a live run over more than one user needs expected_users={count}, the user "
    "count the preceding dry run printed (got {given}). Nothing was changed. "
    "Repeat the dry run if the count has moved, then pass expected_users={count}."
)

# A scope restore has no "latest row" to fall back on: the run_id is what
# says which rows are undone. Tested verbatim.
SCOPE_RESTORE_RUN_ID_MESSAGE = (
    "A scope restore needs run_id: the run whose ledger rows are to be put "
    "back for every user in the scope. Nothing was changed. Find the run_id "
    "in the result table of the apply you are undoing, or in the Ledger tab."
)

# Reason prefix on every address left unpatched after a ledger append
# failed earlier in the same run. Tested verbatim.
LEDGER_FAILED_REASON = "not attempted: ledger append failed earlier in this run"

# Reason on a live address whose ledger row could not be appended after the
# patch went through. Tested verbatim (prefix).
LEDGER_APPEND_FAILED_REASON = "but the ledger append failed"

# Reason on a live address whose pending ledger row (the one written before
# the Gmail patch) could not be appended. No patch happens for it.
LEDGER_PENDING_FAILED_REASON = (
    "not applied: the pending ledger row could not be written before the patch"
)

# The two notes a dry run carries when the ledger could not be consulted.
# ``read_ledger_best_effort`` produces them; the tool layer recognises them
# on the rows to print one header line.
LEDGER_NOT_CONFIGURED_NOTE = "ledger not configured"

# The live apply order for one address, kept in one place:
#   1. pending ledger row (readback_hash = PENDING_READBACK, rollback record
#      complete) so a kill after the patch never loses the previous signature;
#   2. Gmail patch and read-back;
#   3. completed ledger row (same run_id, real readback_hash).
# ledger.outranks makes the completed row win on every read.
LIVE_APPLY_ORDER = ("ledger_pending", "gmail_patch", "ledger_completed")
LEDGER_UNAVAILABLE_NOTE_PREFIX = "ledger unavailable ("

# Setup errors that are never one user's fault. They propagate out of a
# scope loop instead of becoming N identical error rows.
_SETUP_ERRORS = (sa_auth.SignatureAuthError, SignatureConfigError)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def new_run_id() -> str:
    """Ten hex characters from a UUID4: unique enough to correlate a run."""
    return uuid.uuid4().hex[:10]


def utc_now_iso() -> str:
    """UTC timestamp in ISO-8601 to the second, e.g. 2026-09-25T07:00:00+00:00."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _http_status(exc: BaseException) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _format_expected(value: Optional[int]) -> str:
    return "none" if value is None else repr(value)


def require_expected_users(
    label: str, count: int, *, dry_run: bool, expected_users: Optional[int]
) -> None:
    """Refuse a live run over more than one user without the matching count.

    ``expected_users`` must equal ``count`` (the number of users the run
    covers, which is what the dry run printed). A dry run, and a live run
    over one user, need no count. Raises before any write.
    """
    if dry_run or count <= 1:
        return
    if isinstance(expected_users, bool) or expected_users != count:
        raise UserInputError(
            EXPECTED_USERS_MESSAGE.format(
                label=label, count=count, given=_format_expected(expected_users)
            )
        )


def scope_label(
    *,
    ou_path: Optional[str] = None,
    domain: Optional[str] = None,
    group_email: Optional[str] = None,
    all_users: Optional[bool] = None,
) -> str:
    """Name the one scope selected, or refuse when it is not exactly one.

    ``all_users`` is ``None`` for callers that do not offer it (the apply
    tools), so the refusal names only the scopes the caller can pass.
    """
    chosen = []
    if ou_path and ou_path.strip():
        chosen.append(f"OU {ou_path.strip()}")
    if domain and domain.strip():
        chosen.append(f"domain {domain.strip().lower()}")
    if group_email and group_email.strip():
        chosen.append(f"group {group_email.strip().lower()}")
    if all_users:
        chosen.append("all active users")
    if len(chosen) != 1:
        raise UserInputError(
            "Pass exactly one scope: ou_path, domain or group_email"
            + (", or all_users=True" if all_users is not None else "")
            + f". Got {len(chosen)}: {', '.join(chosen) or 'none'}."
        )
    return chosen[0]


# ---------------------------------------------------------------------------
# Runtime (the real clients), built from sa_auth
# ---------------------------------------------------------------------------


@dataclass
class Runtime:
    """The clients one tool call or CLI run works with."""

    config: SignatureConfig
    directory: Any
    sheets: Any
    sheet_id: Optional[str]
    gmail_factory: GmailFactory


def build_runtime(*, need_ledger: bool = True) -> Runtime:
    """Load the config and build the real Directory, Sheets and Gmail clients.

    Raises ``SignatureConfigError`` (bad config), ``SignatureAuthError`` (no
    key or a bad one) or ``LedgerError`` (no ledger Sheet configured). With
    ``need_ledger=False`` a missing or unbuildable ledger leaves ``sheets``
    and ``sheet_id`` as ``None`` instead of raising; callers that only read
    the ledger for information use that mode.
    """
    config = load_config()
    directory = sa_auth.build_directory_as_admin()
    sheets = None
    sheet_id: Optional[str] = None
    try:
        sheet_id = ledger_sheet_id()
        sheets = sa_auth.build_sheets_for_ledger()
    except (LedgerError, sa_auth.SignatureAuthError):
        if need_ledger:
            raise
        logger.warning("signature ledger not available; continuing without it")
        sheets, sheet_id = None, None
    return Runtime(
        config=config,
        directory=directory,
        sheets=sheets,
        sheet_id=sheet_id,
        gmail_factory=sa_auth.build_gmail_for_user,
    )


# ---------------------------------------------------------------------------
# Ledger access
# ---------------------------------------------------------------------------


def _require_ledger_client(sheets, sheet_id: Optional[str]) -> str:
    if sheets is None or not (sheet_id or "").strip():
        raise LedgerError(
            "A live run needs the signature ledger (a Sheets client and "
            f"{sa_auth.ENV_LEDGER_SHEET_ID}). The ledger is the evidence of "
            "every apply, so no signature is written without it."
        )
    return str(sheet_id).strip()


async def _prepare_ledger_tab(sheets, sheet_id: str, *, probe_write: bool) -> None:
    """Ensure the ``Ledger`` tab and header; with ``probe_write`` prove it writable."""
    await ensure_tab(sheets, sheet_id, LEDGER_TAB, LEDGER_HEADER)
    if probe_write:
        await assert_tab_writable(sheets, sheet_id, LEDGER_TAB, LEDGER_HEADER)


async def prepare_ledger(
    sheets, sheet_id: Optional[str], *, probe_write: bool = False
) -> LedgerLatest:
    """Prepare the ledger and return its latest rows, or raise ``LedgerError``.

    Ensures the ``Ledger`` tab exists with the right header (creating it on
    first use), then reads the latest row per (user, send-as). With
    ``probe_write=True`` (every path about to write a signature) it also
    re-writes the identical header row first, so a Sheet shared as Viewer is
    refused here and not after the first Gmail patch. Audits pass the default
    and only read. Any failure is raised as ``LedgerError`` naming the
    underlying error, so a live run can be refused before its first Gmail
    write.
    """
    sheet_id = _require_ledger_client(sheets, sheet_id)
    try:
        await _prepare_ledger_tab(sheets, sheet_id, probe_write=probe_write)
        return await read_ledger_latest(sheets, sheet_id)
    except LedgerError:
        raise
    except Exception as exc:
        raise LedgerError(
            "The signature ledger could not be prepared or read "
            f"({_error_text(exc)}). No signature was written."
        ) from exc


async def read_ledger_best_effort(
    sheets, sheet_id: Optional[str]
) -> Tuple[Optional[LedgerLatest], Optional[str]]:
    """Read the ledger for information only: ``(rows, None)`` or ``(None, why)``.

    A Sheet with no ``Ledger`` tab yet (shared, never written) is an empty
    ledger, ``({}, None)``, not a failure: every managed address then reads
    ``never_applied``, which is the truth.
    """
    if sheets is None or not (sheet_id or "").strip():
        return None, LEDGER_NOT_CONFIGURED_NOTE
    try:
        if not await ledger_tab_exists(sheets, sheet_id):
            return {}, None
        return await read_ledger_latest(sheets, sheet_id), None
    except Exception as exc:
        logger.warning("ledger read failed (continuing): %s", _error_text(exc))
        return None, f"{LEDGER_UNAVAILABLE_NOTE_PREFIX}{_error_text(exc)})"


# ---------------------------------------------------------------------------
# Planning one user
# ---------------------------------------------------------------------------


@dataclass
class _LoadedUser:
    user: Dict[str, Any]
    gmail: Any
    send_as_list: List[Dict[str, Any]]
    planned: List[PlannedSignature]

    @property
    def email(self) -> str:
        return str(self.user.get("primaryEmail") or "")

    def current_signature(self, send_as_email: str) -> str:
        wanted = send_as_email.lower()
        for entry in self.send_as_list:
            if str(entry.get("sendAsEmail") or "").lower() == wanted:
                return str(entry.get("signature") or "")
        return ""


async def _load_from_user(
    config: SignatureConfig, user: Dict[str, Any], gmail_factory: GmailFactory
) -> _LoadedUser:
    email = str(user.get("primaryEmail") or "").strip()
    if not email:
        raise ValueError("Directory user has no primaryEmail.")
    gmail = gmail_factory(email)
    send_as_list = await clients.list_send_as(gmail)
    planned = plan_for_user(config, user, send_as_list)
    return _LoadedUser(
        user=user, gmail=gmail, send_as_list=send_as_list, planned=planned
    )


async def _load_user(
    config: SignatureConfig, directory, user_key: str, gmail_factory: GmailFactory
) -> _LoadedUser:
    user = await clients.get_directory_user(directory, user_key)
    return await _load_from_user(config, user, gmail_factory)


async def plan_user(
    config: SignatureConfig,
    directory,
    user_key: str,
    *,
    gmail_factory: GmailFactory = sa_auth.build_gmail_for_user,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[PlannedSignature]]:
    """Read one user and plan every send-as address. Nothing is written.

    Returns ``(user, send_as_list, planned)``: the Directory resource, the
    current Gmail send-as entries (primary first, each with its current
    ``signature``), and the engine's plan per address.
    """
    loaded = await _load_user(config, directory, user_key, gmail_factory)
    return loaded.user, loaded.send_as_list, loaded.planned


# ---------------------------------------------------------------------------
# Applying one user
# ---------------------------------------------------------------------------


def _ledger_match(
    plan: PlannedSignature, current_hash: str, ledger_row: Optional[Dict[str, str]]
) -> Tuple[bool, str]:
    """Does the ledger say this exact signature is already in place?

    Returns ``(unchanged, reason)``. Unchanged means the latest ledger row for
    the address carries the same template and statutory versions and the
    same rendered hash as the plan, AND Gmail's current signature hashes to
    that row's read-back hash. Otherwise the reason says which part moved.
    """
    if not ledger_row:
        return False, "no ledger row for this address"
    if is_pending_ledger_row(ledger_row):
        return False, (
            f"ledger row for run {ledger_row.get('run_id') or '?'} is pending "
            "(the apply was interrupted before the read-back was recorded)"
        )
    ledger_tv = str(ledger_row.get("template_version") or "")
    ledger_sv = str(ledger_row.get("statutory_version") or "")
    if ledger_tv != (plan.template_version or "") or ledger_sv != (
        plan.statutory_version or ""
    ):
        return False, (
            f"ledger has template {ledger_tv or '?'} / statutory {ledger_sv or '?'}, "
            f"config pins {plan.template_version} / {plan.statutory_version}"
        )
    if str(ledger_row.get("rendered_hash") or "") != (plan.rendered_hash or ""):
        return False, "rendered output differs from the ledger (Directory data changed)"
    readback = str(ledger_row.get("readback_hash") or "")
    if current_hash != readback:
        return False, (
            f"current Gmail hash {current_hash[:12] or '(empty)'} differs from "
            f"ledger readback {readback[:12] or '(empty)'}"
        )
    return True, f"ledger and Gmail match (readback {readback[:12]})"


def _row(
    plan: PlannedSignature,
    action: str,
    reason: str,
    before_hash: Optional[str],
    after_hash: Optional[str],
) -> ResultRow:
    return ResultRow(
        user_email=plan.user_email,
        send_as_email=plan.send_as_email,
        entity=plan.entity,
        template_version=plan.template_version,
        action=action,
        reason=reason,
        before_hash=before_hash,
        after_hash=after_hash,
    )


def _count_actions(rows: List[ResultRow]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row.action] = counts.get(row.action, 0) + 1
    return counts


def _user_error_row(user_email: str, exc: BaseException) -> ResultRow:
    return ResultRow(
        user_email=user_email,
        send_as_email=USER_LEVEL_SEND_AS,
        entity=None,
        template_version=None,
        action="error",
        reason=_error_text(exc),
        before_hash=None,
        after_hash=None,
    )


def _select_plans(
    loaded: _LoadedUser, only_send_as: Optional[str], primary_only: bool
) -> List[PlannedSignature]:
    plans = loaded.planned
    if only_send_as and only_send_as.strip():
        wanted = only_send_as.strip().lower()
        chosen = [p for p in plans if p.send_as_email.lower() == wanted]
        if not chosen:
            available = ", ".join(p.send_as_email for p in plans) or "(none)"
            raise UserInputError(
                f"{loaded.email} has no send-as address {only_send_as.strip()}. "
                f"Available: {available}."
            )
        return chosen
    if primary_only:
        return [p for p in plans if p.is_primary]
    return plans


@dataclass
class _RunState:
    """What one run has learnt so far; shared across every user in a scope.

    ``ledger_failed`` holds the text of the first failed ledger append. Once
    set, no further address is patched: a write that cannot be recorded does
    not happen, and that rule holds mid-run as much as before the run.
    """

    ledger_failed: Optional[str] = None


async def _apply_loaded(
    loaded: _LoadedUser,
    *,
    actor: str,
    run_id: str,
    dry_run: bool,
    force: bool,
    include_aliases: bool,
    ledger_latest: LedgerLatest,
    sheets,
    sheet_id: Optional[str],
    only_send_as: Optional[str] = None,
    primary_only: bool = False,
    state: Optional[_RunState] = None,
    ledger_note: Optional[str] = None,
) -> List[ResultRow]:
    rows: List[ResultRow] = []
    user_key = loaded.email.lower()
    state = state if state is not None else _RunState()

    for plan in _select_plans(loaded, only_send_as, primary_only):
        # The engine's skip (personal alias, suspended user, excluded OU)
        # is checked before force so force can never push a company
        # signature onto an address the rules exclude, and before the
        # caller's alias switch so the rule's own reason is what is shown.
        if plan.status == "skipped":
            rows.append(_row(plan, "skipped", plan.reason, None, None))
            continue
        # An alias the caller excluded is reported as excluded, whatever
        # the engine would have said about it: its faults are not this
        # run's errors.
        if not include_aliases and not plan.is_primary:
            rows.append(_row(plan, "skipped", "aliases not included", None, None))
            continue
        if plan.status == "error":
            rows.append(_row(plan, "error", plan.reason, None, None))
            continue

        current_html = loaded.current_signature(plan.send_as_email)
        current_hash = signature_hash(current_html)
        ledger_row = ledger_latest.get((user_key, plan.send_as_email.lower()))
        unchanged, why = _ledger_match(plan, current_hash, ledger_row)

        if unchanged and not force:
            rows.append(_row(plan, "unchanged", why, current_hash, current_hash))
            continue
        if force:
            why = "force=True" + (f" ({why})" if unchanged else f"; {why}")

        if dry_run:
            if ledger_note:
                # The ledger could not be read, so "no ledger row" would be
                # a claim this run cannot make. Say what actually happened.
                why = f"{ledger_note}; {why}"
            rows.append(
                _row(plan, "would_apply", why, current_hash, plan.rendered_hash)
            )
            continue

        if state.ledger_failed:
            rows.append(
                _row(
                    plan,
                    "error",
                    f"{LEDGER_FAILED_REASON} ({state.ledger_failed})",
                    current_hash,
                    None,
                )
            )
            continue

        # Live path, in this order: pending ledger row, patch, completed
        # ledger row. The pending row carries the full rollback record
        # (previous_hash, previous_signature_html) with readback_hash set
        # to the literal "pending", so a process killed between the patch
        # and the second append leaves the previous signature on record.
        # Nothing is patched until the pending row is on the sheet.
        ledger_entry = {
            "applied_at": utc_now_iso(),
            "actor": actor,
            "user_email": plan.user_email,
            "send_as_email": plan.send_as_email,
            "entity": plan.entity,
            "template_version": plan.template_version,
            "statutory_version": plan.statutory_version,
            "rendered_hash": plan.rendered_hash,
            "readback_hash": PENDING_READBACK,
            "previous_hash": current_hash,
            "previous_signature_html": current_html,
            "run_id": run_id,
        }
        try:
            await append_ledger_rows(sheets, sheet_id or "", [ledger_entry])
        except Exception as exc:
            logger.error(
                "pending ledger row for %s / %s could not be written; "
                "signature not applied: %s",
                plan.user_email,
                plan.send_as_email,
                _error_text(exc),
            )
            state.ledger_failed = _error_text(exc)
            rows.append(
                _row(
                    plan,
                    "error",
                    f"{LEDGER_PENDING_FAILED_REASON} ({_error_text(exc)})",
                    current_hash,
                    None,
                )
            )
            continue

        readback_hash: Optional[str] = None
        try:
            readback = await clients.patch_signature(
                loaded.gmail, plan.send_as_email, plan.html or ""
            )
            readback_hash = signature_hash(readback.get("signature"))
        except Exception as exc:  # one address must not stop the rest
            logger.warning(
                "signature patch failed for %s / %s: %s",
                plan.user_email,
                plan.send_as_email,
                _error_text(exc),
            )
            rows.append(
                _row(
                    plan,
                    "error",
                    f"{_error_text(exc)}; a pending ledger row for run "
                    f"{run_id} remains and the audit reports apply_interrupted "
                    "until the address is re-applied",
                    current_hash,
                    None,
                )
            )
            continue

        # The completed row: same run_id, the real read-back hash. It
        # outranks the pending row on every read (ledger.outranks).
        ledger_entry = dict(
            ledger_entry,
            applied_at=utc_now_iso(),
            readback_hash=readback_hash,
        )
        try:
            await append_ledger_rows(sheets, sheet_id or "", [ledger_entry])
        except Exception as exc:
            logger.error(
                "signature applied for %s / %s but the ledger append failed: %s",
                plan.user_email,
                plan.send_as_email,
                _error_text(exc),
            )
            state.ledger_failed = _error_text(exc)
            rows.append(
                _row(
                    plan,
                    "error",
                    f"signature applied {LEDGER_APPEND_FAILED_REASON} "
                    f"({_error_text(exc)}); record this row by hand",
                    current_hash,
                    readback_hash,
                )
            )
            continue

        rows.append(_row(plan, "applied", why, current_hash, readback_hash))
    return rows


def gate_live(dry_run: bool, confirm: bool) -> None:
    """Refuse a live run without the second switch. Raises before any call.

    Public so the tool layer can run it before building any client; the
    operations run it again themselves, so nothing depends on the caller.
    """
    if not dry_run and not confirm:
        raise UserInputError(LIVE_CONFIRM_MESSAGE)


_gate_live = gate_live


async def _resolve_ledger(
    dry_run: bool, ledger_latest: Optional[LedgerLatest], sheets, sheet_id
) -> Tuple[LedgerLatest, Optional[str]]:
    """Live: prepare with a write probe (or refuse). Dry run: best effort.

    Returns ``(latest, note)``. ``note`` is set only on a dry run whose
    ledger could not be read or is not configured; the rows then carry it.
    """
    if ledger_latest is not None:
        if not dry_run and (sheets is None or not (sheet_id or "").strip()):
            # The caller proved the ledger readable but gave no client to
            # write with; the row could never be recorded.
            await prepare_ledger(sheets, sheet_id, probe_write=True)
        return ledger_latest, None
    if not dry_run:
        return await prepare_ledger(sheets, sheet_id, probe_write=True), None
    rows, note = await read_ledger_best_effort(sheets, sheet_id)
    return (rows if rows is not None else {}), note


async def apply_user(
    config: SignatureConfig,
    directory,
    user_key: str,
    *,
    actor: str,
    run_id: str,
    dry_run: bool = True,
    confirm: bool = False,
    force: bool = False,
    include_aliases: bool = True,
    ledger_latest: Optional[LedgerLatest] = None,
    sheets=None,
    sheet_id: Optional[str] = None,
    gmail_factory: GmailFactory = sa_auth.build_gmail_for_user,
    only_send_as: Optional[str] = None,
    primary_only: bool = False,
) -> List[ResultRow]:
    """Plan one user and apply (or dry-run) every selected send-as address.

    Order of checks: the live gate (``dry_run=False`` needs ``confirm=True``)
    before any Google call; then, for a live run, the ledger is prepared
    (readable and writable) before the Directory or Gmail is touched; then
    the user is planned and each address handled on its own.

    ``ledger_latest`` may be passed by a caller that already read the ledger
    (``apply_scope`` does, once per run). ``only_send_as`` limits the run to
    one address; ``primary_only`` to the primary. One row per address
    handled; see ``engine.ResultRow``.
    """
    gate_live(dry_run, confirm)
    latest, note = await _resolve_ledger(dry_run, ledger_latest, sheets, sheet_id)
    loaded = await _load_user(config, directory, user_key, gmail_factory)
    return await _apply_loaded(
        loaded,
        actor=actor,
        run_id=run_id,
        dry_run=dry_run,
        force=force,
        include_aliases=include_aliases,
        ledger_latest=latest,
        sheets=sheets,
        sheet_id=sheet_id,
        only_send_as=only_send_as,
        primary_only=primary_only,
        state=_RunState(),
        ledger_note=note,
    )


# ---------------------------------------------------------------------------
# Restoring one address from the ledger
# ---------------------------------------------------------------------------


def _pick_send_as(
    send_as_list: List[Dict[str, Any]], user_email: str, send_as_email: Optional[str]
) -> Dict[str, Any]:
    if send_as_email and send_as_email.strip():
        wanted = send_as_email.strip().lower()
        for entry in send_as_list:
            if str(entry.get("sendAsEmail") or "").lower() == wanted:
                return entry
        available = (
            ", ".join(str(e.get("sendAsEmail") or "") for e in send_as_list) or "(none)"
        )
        raise UserInputError(
            f"{user_email} has no send-as address {send_as_email.strip()}. "
            f"Available: {available}."
        )
    for entry in send_as_list:
        if entry.get("isPrimary"):
            return entry
    raise UserInputError(f"{user_email} has no primary send-as address in Gmail.")


def _pick_ledger_row(
    records: List[Dict[str, str]],
    user_email: str,
    send_as_email: str,
    from_run_id: Optional[str],
) -> Dict[str, str]:
    key = (user_email.lower(), send_as_email.lower())
    mine = [r for r in records if ledger_key(r) == key]
    if not mine:
        raise UserInputError(
            f"The ledger has no row for {user_email} / {send_as_email}, so there "
            "is no previous signature to restore. Nothing was changed."
        )
    if from_run_id and from_run_id.strip():
        wanted = from_run_id.strip()
        hits = [r for r in mine if (r.get("run_id") or "").strip() == wanted]
        if not hits:
            newest_first = sorted(mine, key=lambda r: r["applied_at"], reverse=True)
            known = ", ".join(
                f"{r['run_id'] or '?'} ({r['applied_at']})" for r in newest_first[:10]
            )
            raise UserInputError(
                f"The ledger has no row for {user_email} / {send_as_email} with "
                f"run_id {wanted}. Known run_ids, newest first: {known}. "
                "Nothing was changed."
            )
        chosen = hits[0]
        for record in hits[1:]:
            if outranks(record, chosen):
                chosen = record
        return chosen
    latest = mine[0]
    for record in mine[1:]:
        if outranks(record, latest):
            latest = record
    return latest


def _restore_row(
    user_email: str,
    send_as_email: str,
    ledger_row: Dict[str, str],
    action: str,
    reason: str,
    before_hash: Optional[str],
    after_hash: Optional[str],
) -> ResultRow:
    return ResultRow(
        user_email=user_email,
        send_as_email=send_as_email,
        entity=ledger_row.get("entity") or None,
        template_version=RESTORED_VERSION,
        action=action,
        reason=reason,
        before_hash=before_hash,
        after_hash=after_hash,
    )


async def _read_ledger_for_restore(
    sheets, sheet_id: Optional[str], *, dry_run: bool
) -> Tuple[str, List[Dict[str, str]]]:
    """Prepare the ledger (write probe on a live run) and read every row.

    A dry run still needs a readable ledger: the row is what is restored.
    """
    sheet_id = _require_ledger_client(sheets, sheet_id)
    try:
        await _prepare_ledger_tab(sheets, sheet_id, probe_write=not dry_run)
        return sheet_id, await read_ledger_rows(sheets, sheet_id)
    except LedgerError:
        raise
    except Exception as exc:
        raise LedgerError(
            "The signature ledger could not be prepared or read "
            f"({_error_text(exc)}). No signature was written."
        ) from exc


async def _restore_address(
    *,
    gmail,
    entry: Dict[str, Any],
    ledger_row: Dict[str, str],
    user_email: str,
    actor: str,
    run_id: str,
    dry_run: bool,
    sheets,
    sheet_id: str,
    state: _RunState,
) -> ResultRow:
    """Restore one send-as address from one ledger row. The live order is
    the same as an apply: pending ledger row, Gmail patch, completed ledger
    row (``LIVE_APPLY_ORDER``), so a process killed after the patch still
    leaves the replaced signature on record and ``ledger.outranks`` makes
    the completed row win on every read."""
    address = str(entry.get("sendAsEmail") or "")
    previous_html = ledger_row.get("previous_signature_html") or ""
    target_hash = signature_hash(previous_html)
    current_html = str(entry.get("signature") or "")
    current_hash = signature_hash(current_html)
    source = (
        f"ledger row run_id {ledger_row.get('run_id') or '?'} applied "
        f"{ledger_row.get('applied_at') or '?'}"
    )
    if not previous_html.strip():
        source += " (previous signature was empty: this clears the signature)"

    def row(action: str, reason: str, after_hash: Optional[str]) -> ResultRow:
        return _restore_row(
            user_email, address, ledger_row, action, reason, current_hash, after_hash
        )

    if current_hash == target_hash:
        return row(
            "unchanged",
            f"Gmail already holds the previous signature from {source}",
            current_hash,
        )
    if dry_run:
        return row(
            "would_apply", f"restore previous signature from {source}", target_hash
        )
    if state.ledger_failed:
        return row("error", f"{LEDGER_FAILED_REASON} ({state.ledger_failed})", None)

    # Pending row first: the rollback record (the signature being replaced)
    # is on the sheet before Gmail changes.
    ledger_entry = {
        "applied_at": utc_now_iso(),
        "actor": actor,
        "user_email": user_email,
        "send_as_email": address,
        "entity": ledger_row.get("entity") or "",
        "template_version": RESTORED_VERSION,
        "statutory_version": RESTORED_VERSION,
        "rendered_hash": target_hash,
        "readback_hash": PENDING_READBACK,
        "previous_hash": current_hash,
        "previous_signature_html": current_html,
        "run_id": run_id,
    }
    try:
        await append_ledger_rows(sheets, sheet_id, [ledger_entry])
    except Exception as exc:
        logger.error(
            "pending ledger row for restore of %s / %s could not be written; "
            "signature not restored: %s",
            user_email,
            address,
            _error_text(exc),
        )
        state.ledger_failed = _error_text(exc)
        return row(
            "error", f"{LEDGER_PENDING_FAILED_REASON} ({_error_text(exc)})", None
        )

    try:
        readback = await clients.patch_signature(gmail, address, previous_html)
    except Exception as exc:
        logger.warning(
            "signature restore failed for %s / %s: %s",
            user_email,
            address,
            _error_text(exc),
        )
        return row(
            "error",
            f"{_error_text(exc)}; a pending ledger row for run {run_id} remains "
            "and the audit reports apply_interrupted until the address is "
            "re-applied or restored",
            None,
        )
    readback_hash = signature_hash(readback.get("signature"))

    ledger_entry = dict(
        ledger_entry, applied_at=utc_now_iso(), readback_hash=readback_hash
    )
    try:
        await append_ledger_rows(sheets, sheet_id, [ledger_entry])
    except Exception as exc:
        logger.error(
            "signature restored for %s / %s but the ledger append failed: %s",
            user_email,
            address,
            _error_text(exc),
        )
        state.ledger_failed = _error_text(exc)
        return row(
            "error",
            f"signature restored {LEDGER_APPEND_FAILED_REASON} "
            f"({_error_text(exc)}); record this row by hand",
            readback_hash,
        )
    return row("applied", f"restored previous signature from {source}", readback_hash)


async def restore_user(
    user_email: str,
    send_as_email: Optional[str] = None,
    *,
    actor: str,
    run_id: str,
    from_run_id: Optional[str] = None,
    dry_run: bool = True,
    confirm: bool = False,
    sheets=None,
    sheet_id: Optional[str] = None,
    gmail_factory: GmailFactory = sa_auth.build_gmail_for_user,
) -> List[ResultRow]:
    """Put back the previous signature a ledger row recorded for one address.

    The ledger row is the latest for (user, send-as), or the one with
    ``from_run_id`` when given; within one run the completed row is preferred
    over its ``pending`` row, and a pending row with no completed row (an
    interrupted apply) is a valid source, because its
    ``previous_signature_html`` was written before the patch. That HTML is
    patched onto the address (an empty value clears the signature, which is
    what "previous" meant then). Same rule as an apply: dry run by default; live
    needs ``dry_run=False`` AND ``confirm=True`` and a writable ledger, and
    the ledger is prepared before Gmail is touched. A dry run still needs a
    readable ledger, because the row is what is being restored.

    A live restore is recorded like an apply: a pending ledger row (the
    signature being replaced already in ``previous_signature_html``), then
    the patch, then a completed row with ``rendered_hash`` of what was
    restored and ``readback_hash`` of what Gmail kept, both versions
    ``restored``, so a restore is itself reversible. Exactly one ``ResultRow``
    is returned.
    """
    gate_live(dry_run, confirm)
    user_email = user_email.strip()
    sheet_id, records = await _read_ledger_for_restore(
        sheets, sheet_id, dry_run=dry_run
    )

    gmail = gmail_factory(user_email)
    send_as_list = await clients.list_send_as(gmail)
    entry = _pick_send_as(send_as_list, user_email, send_as_email)
    address = str(entry.get("sendAsEmail") or "")
    ledger_row = _pick_ledger_row(records, user_email, address, from_run_id)
    return [
        await _restore_address(
            gmail=gmail,
            entry=entry,
            ledger_row=ledger_row,
            user_email=user_email,
            actor=actor,
            run_id=run_id,
            dry_run=dry_run,
            sheets=sheets,
            sheet_id=sheet_id,
            state=_RunState(),
        )
    ]


def _rows_for_run(
    records: List[Dict[str, str]], from_run_id: str
) -> Dict[Tuple[str, str], Dict[str, str]]:
    """The best ledger row per (user, send-as) that carries ``from_run_id``.

    The completed row of the run beats its pending row (``ledger.outranks``);
    a pending row with no completed row is a valid source.
    """
    chosen: Dict[Tuple[str, str], Dict[str, str]] = {}
    for record in records:
        if (record.get("run_id") or "").strip() != from_run_id:
            continue
        key = ledger_key(record)
        current = chosen.get(key)
        if current is None or outranks(record, current):
            chosen[key] = record
    return chosen


def _restore_missing_address_row(
    user_email: str, ledger_row: Dict[str, str], reason: str
) -> ResultRow:
    return _restore_row(
        user_email,
        str(ledger_row.get("send_as_email") or ""),
        ledger_row,
        "error",
        reason,
        None,
        None,
    )


async def restore_scope(
    directory,
    *,
    ou_path: Optional[str] = None,
    domain: Optional[str] = None,
    group_email: Optional[str] = None,
    from_run_id: Optional[str],
    actor: str,
    run_id: str,
    dry_run: bool = True,
    confirm: bool = False,
    expected_users: Optional[int] = None,
    max_users: int = DEFAULT_MAX_USERS,
    sheets=None,
    sheet_id: Optional[str] = None,
    gmail_factory: GmailFactory = sa_auth.build_gmail_for_user,
) -> Tuple[List[ResultRow], Dict[str, Any]]:
    """Undo one run: restore every (user, send-as) in a scope that has a
    ledger row for ``from_run_id``.

    Checks, in order: exactly one scope; the live gate; ``from_run_id`` is
    required (refused with ``SCOPE_RESTORE_RUN_ID_MESSAGE``); the ledger
    (write probe on a live run, then every row is read); the scope's users
    against ``max_users``; then the rows of that run inside the scope. No row
    at all is refused. A live restore over more than one affected user needs
    ``expected_users`` equal to that count (``require_expected_users``),
    checked before any write. Rows of the run for users outside the scope
    are counted in ``report_meta["rows_outside_scope"]`` and left alone.

    Each address then follows ``_restore_address`` (pending row, patch,
    completed row) with one ``_RunState`` for the run, so a failed ledger
    append stops every later patch. A user whose Gmail cannot be read
    becomes one ``error`` row per ledger row; an address the mailbox no
    longer has becomes an ``error`` row. Every row is written as JSONL into
    the attachment store and ``report_meta`` carries the access line.
    """
    label = scope_label(ou_path=ou_path, domain=domain, group_email=group_email)
    gate_live(dry_run, confirm)
    if not (from_run_id or "").strip():
        raise UserInputError(SCOPE_RESTORE_RUN_ID_MESSAGE)
    from_run_id = str(from_run_id).strip()
    if not isinstance(max_users, int) or max_users < 1:
        raise UserInputError(
            f"max_users must be a positive integer, got {max_users!r}."
        )

    sheet_id, records = await _read_ledger_for_restore(
        sheets, sheet_id, dry_run=dry_run
    )
    resolved = await _resolve_scope_users(
        directory,
        ou_path=ou_path,
        domain=domain,
        group_email=group_email,
        all_users=False,
    )
    if len(resolved) > max_users:
        raise UserInputError(
            f"Scope {label} matches {len(resolved)} users, more than "
            f"max_users={max_users}. Narrow the scope or raise max_users on "
            "purpose. Nothing was changed."
        )

    run_rows = _rows_for_run(records, from_run_id)
    in_scope = {email.lower() for email, _, _ in resolved if email}
    by_user: Dict[str, List[Dict[str, str]]] = {}
    outside = 0
    for (user_key, _), record in run_rows.items():
        if user_key in in_scope:
            by_user.setdefault(user_key, []).append(record)
        else:
            outside += 1
    if not by_user:
        raise UserInputError(
            f"The ledger has no row with run_id {from_run_id} for any user in "
            f"scope {label}"
            + (
                f" ({outside} row(s) with that run_id belong to users outside "
                "the scope)"
                if outside
                else ""
            )
            + ". Nothing was changed."
        )
    user_count = len(by_user)
    require_expected_users(
        label, user_count, dry_run=dry_run, expected_users=expected_users
    )

    state = _RunState()
    rows: List[ResultRow] = []
    scope_errors = {email.lower(): err for email, _, err in resolved if err}
    for email, _, _ in resolved:
        user_key = email.lower()
        ledger_rows = by_user.get(user_key)
        if not ledger_rows:
            continue
        ledger_rows.sort(key=lambda r: (r.get("send_as_email") or "").lower())
        display_email = str(ledger_rows[0].get("user_email") or email)
        error = scope_errors.get(user_key)
        if error is None:
            try:
                gmail = gmail_factory(display_email)
                send_as_list = await clients.list_send_as(gmail)
            except _SETUP_ERRORS:  # the key or config, never one user's fault
                raise
            except Exception as exc:  # one user must not stop the scope
                logger.warning(
                    "signature restore failed for %s: %s",
                    display_email,
                    _error_text(exc),
                )
                error = exc
        if error is not None:
            rows.extend(
                _restore_missing_address_row(display_email, r, _error_text(error))
                for r in ledger_rows
            )
            continue
        entries = {str(e.get("sendAsEmail") or "").lower(): e for e in send_as_list}
        for ledger_row in ledger_rows:
            address = str(ledger_row.get("send_as_email") or "")
            entry = entries.get(address.lower())
            if entry is None:
                rows.append(
                    _restore_missing_address_row(
                        display_email,
                        ledger_row,
                        f"{display_email} no longer has send-as address {address}; "
                        "nothing to restore onto",
                    )
                )
                continue
            rows.append(
                await _restore_address(
                    gmail=gmail,
                    entry=entry,
                    ledger_row=ledger_row,
                    user_email=display_email,
                    actor=actor,
                    run_id=run_id,
                    dry_run=dry_run,
                    sheets=sheets,
                    sheet_id=sheet_id,
                    state=state,
                )
            )

    filename = f"signatures-restore-{'dryrun' if dry_run else 'live'}-{run_id}.jsonl"
    attachment_id, path, access_line = write_jsonl_report(
        result_rows_as_dicts(rows), filename=filename
    )
    report_meta = {
        "scope": label,
        "run_id": run_id,
        "from_run_id": from_run_id,
        "dry_run": dry_run,
        "actor": actor,
        "user_count": user_count,
        "scope_user_count": len(resolved),
        "rows_outside_scope": outside,
        "ledger_failed": state.ledger_failed,
        "counts": _count_actions(rows),
        "report_filename": filename,
        "report_attachment_id": attachment_id,
        "report_path": path,
        "access_line": access_line,
    }
    return rows, report_meta


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------


async def _resolve_scope_users(
    directory,
    *,
    ou_path: Optional[str],
    domain: Optional[str],
    group_email: Optional[str],
    all_users: bool,
) -> List[Tuple[str, Optional[Dict[str, Any]], Optional[BaseException]]]:
    """Users in the scope as ``(email, user_or_None, error_or_None)`` triples.

    OU, domain and all-users scopes come from one ``users.list`` (active
    users only, full projection). A group scope lists the direct user
    members and fetches each one; a member that cannot be fetched is kept
    as an error triple so the caller can report it without losing the rest.
    """
    if group_email and group_email.strip():
        try:
            emails = await clients.list_group_member_emails(directory, group_email)
        except HttpError as exc:
            if _http_status(exc) in (403, 404):
                raise UserInputError(
                    f"Group {group_email.strip()} was not found or is not "
                    "readable by the Directory admin. Nothing was changed."
                ) from exc
            raise
        out: List[Tuple[str, Optional[Dict[str, Any]], Optional[BaseException]]] = []
        for email in emails:
            try:
                out.append(
                    (email, await clients.get_directory_user(directory, email), None)
                )
            except Exception as exc:
                out.append((email, None, exc))
        return out
    users = await clients.list_directory_users(
        directory,
        ou_path=ou_path.strip() if ou_path and ou_path.strip() else None,
        domain=domain.strip().lower() if domain and domain.strip() else None,
    )
    return [(str(u.get("primaryEmail") or ""), u, None) for u in users]


async def apply_scope(
    config: SignatureConfig,
    directory,
    *,
    ou_path: Optional[str] = None,
    domain: Optional[str] = None,
    group_email: Optional[str] = None,
    actor: str,
    run_id: str,
    dry_run: bool = True,
    confirm: bool = False,
    force: bool = False,
    include_aliases: bool = True,
    max_users: int = DEFAULT_MAX_USERS,
    expected_users: Optional[int] = None,
    sheets=None,
    sheet_id: Optional[str] = None,
    gmail_factory: GmailFactory = sa_auth.build_gmail_for_user,
) -> Tuple[List[ResultRow], Dict[str, Any]]:
    """Apply (or dry-run) every user in exactly one scope.

    Checks, in order: exactly one scope; the live gate; the ledger (live
    runs only, before any write); the user count against ``max_users``
    (refused with the count, never truncated); then, on a live run over
    more than one user, ``expected_users`` must equal that count
    (``require_expected_users``), before any signature is written. Then
    each user is handled on its own: a user that cannot be planned at all
    becomes one ``error`` row with send-as ``*``. Every row is written as
    JSONL into the attachment store and ``report_meta`` carries the access
    line.
    """
    label = scope_label(ou_path=ou_path, domain=domain, group_email=group_email)
    gate_live(dry_run, confirm)
    if not isinstance(max_users, int) or max_users < 1:
        raise UserInputError(
            f"max_users must be a positive integer, got {max_users!r}."
        )

    latest, note = await _resolve_ledger(dry_run, None, sheets, sheet_id)
    state = _RunState()
    resolved = await _resolve_scope_users(
        directory,
        ou_path=ou_path,
        domain=domain,
        group_email=group_email,
        all_users=False,
    )
    if len(resolved) > max_users:
        raise UserInputError(
            f"Scope {label} matches {len(resolved)} users, more than "
            f"max_users={max_users}. Narrow the scope or raise max_users on "
            "purpose. Nothing was changed."
        )
    require_expected_users(
        label, len(resolved), dry_run=dry_run, expected_users=expected_users
    )

    rows: List[ResultRow] = []
    for email, user, error in resolved:
        if error is not None:
            rows.append(_user_error_row(email, error))
            continue
        try:
            loaded = await _load_from_user(config, user or {}, gmail_factory)
            rows.extend(
                await _apply_loaded(
                    loaded,
                    actor=actor,
                    run_id=run_id,
                    dry_run=dry_run,
                    force=force,
                    include_aliases=include_aliases,
                    ledger_latest=latest,
                    sheets=sheets,
                    sheet_id=sheet_id,
                    state=state,
                    ledger_note=note,
                )
            )
        except _SETUP_ERRORS:  # the key or config, never one user's fault
            raise
        except Exception as exc:  # one user must not stop the scope
            logger.warning("signature run failed for %s: %s", email, _error_text(exc))
            rows.append(_user_error_row(email, exc))

    filename = f"signatures-{'dryrun' if dry_run else 'apply'}-{run_id}.jsonl"
    attachment_id, path, access_line = write_jsonl_report(
        result_rows_as_dicts(rows), filename=filename
    )
    report_meta = {
        "scope": label,
        "run_id": run_id,
        "dry_run": dry_run,
        "actor": actor,
        "user_count": len(resolved),
        "ledger_note": note,
        "ledger_failed": state.ledger_failed,
        "counts": _count_actions(rows),
        "report_filename": filename,
        "report_attachment_id": attachment_id,
        "report_path": path,
        "access_line": access_line,
    }
    return rows, report_meta


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _audit_row(
    audited_at: str,
    plan: PlannedSignature,
    status: str,
    reason: str,
    ledger_row: Optional[Dict[str, str]],
    current_hash: str,
) -> Dict[str, Any]:
    ledger_row = ledger_row or {}
    return {
        "audited_at": audited_at,
        "user_email": plan.user_email,
        "send_as_email": plan.send_as_email,
        "entity": plan.entity or "",
        "expected_template_version": plan.template_version or "",
        "expected_statutory_version": plan.statutory_version or "",
        "ledger_template_version": ledger_row.get("template_version") or "",
        "ledger_statutory_version": ledger_row.get("statutory_version") or "",
        "status": status,
        "reason": reason,
        "current_hash": current_hash,
    }


def _audit_user_error_row(
    audited_at: str, user_email: str, exc: BaseException
) -> Dict[str, Any]:
    return {
        "audited_at": audited_at,
        "user_email": user_email,
        "send_as_email": USER_LEVEL_SEND_AS,
        "entity": "",
        "expected_template_version": "",
        "expected_statutory_version": "",
        "ledger_template_version": "",
        "ledger_statutory_version": "",
        "status": "error",
        "reason": _error_text(exc),
        "current_hash": "",
    }


async def audit_scope(
    config: SignatureConfig,
    directory,
    *,
    ou_path: Optional[str] = None,
    domain: Optional[str] = None,
    group_email: Optional[str] = None,
    all_users: bool = False,
    ledger_latest: LedgerLatest,
    gmail_factory: GmailFactory = sa_auth.build_gmail_for_user,
    audited_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Compare every send-as in exactly one scope with the ledger. Reads only.

    Rows are dicts in ``ledger.AUDIT_HEADER`` shape, one per send-as address,
    with ``status`` and ``reason`` from ``engine.drift_status``. A user that
    cannot be read at all becomes one ``error`` row with send-as ``*``.
    ``all_users`` is customer-wide active users and counts as a scope.
    """
    scope_label(
        ou_path=ou_path, domain=domain, group_email=group_email, all_users=all_users
    )
    stamp = audited_at or utc_now_iso()
    resolved = await _resolve_scope_users(
        directory,
        ou_path=ou_path,
        domain=domain,
        group_email=group_email,
        all_users=all_users,
    )
    rows: List[Dict[str, Any]] = []
    for email, user, error in resolved:
        if error is not None:
            rows.append(_audit_user_error_row(stamp, email, error))
            continue
        try:
            loaded = await _load_from_user(config, user or {}, gmail_factory)
        except _SETUP_ERRORS:  # the key or config, never one user's fault
            raise
        except Exception as exc:
            logger.warning("signature audit failed for %s: %s", email, _error_text(exc))
            rows.append(_audit_user_error_row(stamp, email, exc))
            continue
        user_key = loaded.email.lower()
        for plan in loaded.planned:
            current_html = loaded.current_signature(plan.send_as_email)
            ledger_row = ledger_latest.get((user_key, plan.send_as_email.lower()))
            status, reason = drift_status(plan, current_html, ledger_row)
            rows.append(
                _audit_row(
                    stamp,
                    plan,
                    status,
                    reason,
                    ledger_row,
                    signature_hash(current_html),
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Audit reporting (plain text, shared by the tool and the CLI)
# ---------------------------------------------------------------------------

# Every status ``engine.drift_status`` can return, in report order.
AUDIT_STATUSES = (
    "in_sync",
    "unmanaged",
    "never_applied",
    APPLY_INTERRUPTED,
    "stale_template",
    "stale_directory",
    "changed_since_apply",
    "error",
)

# Statuses that mean a managed address is not what the ledger says it
# should be. The CLI exits 2 when any row carries one of these.
DRIFT_STATUSES = frozenset(
    {
        "never_applied",
        APPLY_INTERRUPTED,
        "stale_template",
        "stale_directory",
        "changed_since_apply",
        "error",
    }
)

_AUDIT_TABLE_COLUMNS = (
    ("user", "user_email"),
    ("send_as", "send_as_email"),
    ("entity", "entity"),
    ("status", "status"),
    ("reason", "reason"),
)


def audit_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """Row count per status, every known status present (zero when absent)."""
    counts = {status: 0 for status in AUDIT_STATUSES}
    for row in rows:
        status = str(row.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    return counts


def has_drift(rows: List[Dict[str, Any]]) -> bool:
    return any(str(row.get("status") or "") in DRIFT_STATUSES for row in rows)


def format_audit_table(rows: List[Dict[str, Any]]) -> str:
    """Aligned plain-text table: user, send_as, entity, status, reason."""
    headers = [name for name, _ in _AUDIT_TABLE_COLUMNS]
    cells = [
        [str(row.get(key) or "") for _, key in _AUDIT_TABLE_COLUMNS] for row in rows
    ]
    widths = [len(h) for h in headers]
    for line in cells:
        for i, value in enumerate(line):
            widths[i] = max(widths[i], len(value))

    def fmt(values: List[str]) -> str:
        return "  ".join(v.ljust(widths[i]) for i, v in enumerate(values)).rstrip()

    return "\n".join([fmt(headers)] + [fmt(line) for line in cells])


def format_audit_counts(rows: List[Dict[str, Any]]) -> str:
    counts = audit_counts(rows)
    return ", ".join(f"{status}: {counts[status]}" for status in AUDIT_STATUSES)


__all__ = [
    "AUDIT_STATUSES",
    "LEDGER_PENDING_FAILED_REASON",
    "LIVE_APPLY_ORDER",
    "DEFAULT_MAX_USERS",
    "DRIFT_STATUSES",
    "EXPECTED_USERS_MESSAGE",
    "SCOPE_RESTORE_RUN_ID_MESSAGE",
    "LEDGER_APPEND_FAILED_REASON",
    "LEDGER_FAILED_REASON",
    "LEDGER_NOT_CONFIGURED_NOTE",
    "LEDGER_UNAVAILABLE_NOTE_PREFIX",
    "LIVE_CONFIRM_MESSAGE",
    "RESTORED_VERSION",
    "USER_LEVEL_SEND_AS",
    "Runtime",
    "apply_scope",
    "apply_user",
    "audit_counts",
    "audit_scope",
    "build_runtime",
    "format_audit_counts",
    "format_audit_table",
    "gate_live",
    "has_drift",
    "new_run_id",
    "plan_user",
    "prepare_ledger",
    "read_ledger_best_effort",
    "require_expected_users",
    "restore_scope",
    "restore_user",
    "scope_label",
    "utc_now_iso",
]

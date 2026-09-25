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
  (readable and writable) BEFORE the first Gmail write; if the ledger
  cannot be prepared the run is refused and nothing is written.
* Drift and "unchanged" are judged against the ledger's read-back hash
  (what Gmail returned right after the last apply), never against a fresh
  render, because Gmail sanitises what it stores.
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

from core.utils import UserInputError
from gdrive.drive_batch import write_jsonl_report

from gsignatures import clients, sa_auth
from gsignatures.engine import (
    PlannedSignature,
    ResultRow,
    SignatureConfig,
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
    ensure_tab,
    ledger_sheet_id,
    read_ledger_latest,
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
        sheets = sa_auth.build_sheets_as_service_account()
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


async def prepare_ledger(sheets, sheet_id: Optional[str]) -> LedgerLatest:
    """Prove the ledger is writable and readable, then return its latest rows.

    Ensures the ``Ledger`` tab exists with the right header (creating it on
    first use), then reads the latest row per (user, send-as). Any failure
    is raised as ``LedgerError`` naming the underlying error, so a live run
    can be refused before its first Gmail write.
    """
    if sheets is None or not (sheet_id or "").strip():
        raise LedgerError(
            "A live run needs the signature ledger (a Sheets client and "
            f"{sa_auth.ENV_LEDGER_SHEET_ID}). The ledger is the evidence of "
            "every apply, so no signature is written without it."
        )
    try:
        await ensure_tab(sheets, sheet_id, LEDGER_TAB, LEDGER_HEADER)
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
    """Read the ledger for information only: ``(rows, None)`` or ``(None, why)``."""
    if sheets is None or not (sheet_id or "").strip():
        return None, "ledger not configured"
    try:
        return await read_ledger_latest(sheets, sheet_id), None
    except Exception as exc:
        logger.warning("ledger read failed (continuing): %s", _error_text(exc))
        return None, f"ledger unavailable ({_error_text(exc)})"


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
) -> List[ResultRow]:
    rows: List[ResultRow] = []
    user_key = loaded.email.lower()

    for plan in _select_plans(loaded, only_send_as, primary_only):
        if plan.status == "skipped":
            rows.append(_row(plan, "skipped", plan.reason, None, None))
            continue
        if plan.status == "error":
            rows.append(_row(plan, "error", plan.reason, None, None))
            continue
        if not include_aliases and not plan.is_primary:
            rows.append(_row(plan, "skipped", "aliases not included", None, None))
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
            rows.append(
                _row(plan, "would_apply", why, current_hash, plan.rendered_hash)
            )
            continue

        # Live path. Patch, read back, record. The ledger row goes in
        # straight after each address so partial progress is still evidence.
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
            rows.append(_row(plan, "error", _error_text(exc), current_hash, None))
            continue

        ledger_entry = {
            "applied_at": utc_now_iso(),
            "actor": actor,
            "user_email": plan.user_email,
            "send_as_email": plan.send_as_email,
            "entity": plan.entity,
            "template_version": plan.template_version,
            "statutory_version": plan.statutory_version,
            "rendered_hash": plan.rendered_hash,
            "readback_hash": readback_hash,
            "previous_hash": current_hash,
            "previous_signature_html": current_html,
            "run_id": run_id,
        }
        try:
            await append_ledger_rows(sheets, sheet_id or "", [ledger_entry])
        except Exception as exc:
            logger.error(
                "signature applied for %s / %s but the ledger append failed: %s",
                plan.user_email,
                plan.send_as_email,
                _error_text(exc),
            )
            rows.append(
                _row(
                    plan,
                    "error",
                    "signature applied but the ledger append failed "
                    f"({_error_text(exc)}); record this row by hand",
                    current_hash,
                    readback_hash,
                )
            )
            continue

        rows.append(_row(plan, "applied", why, current_hash, readback_hash))
    return rows


def _gate_live(dry_run: bool, confirm: bool) -> None:
    if not dry_run and not confirm:
        raise UserInputError(LIVE_CONFIRM_MESSAGE)


async def _resolve_ledger(
    dry_run: bool, ledger_latest: Optional[LedgerLatest], sheets, sheet_id
) -> LedgerLatest:
    """Live: prepare (or refuse). Dry run: best effort, empty when unavailable."""
    if ledger_latest is not None:
        if not dry_run and (sheets is None or not (sheet_id or "").strip()):
            # The caller proved the ledger readable but gave no client to
            # write with; the row could never be recorded.
            await prepare_ledger(sheets, sheet_id)
        return ledger_latest
    if not dry_run:
        return await prepare_ledger(sheets, sheet_id)
    rows, _ = await read_ledger_best_effort(sheets, sheet_id)
    return rows or {}


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
    _gate_live(dry_run, confirm)
    latest = await _resolve_ledger(dry_run, ledger_latest, sheets, sheet_id)
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
    )


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
        emails = await clients.list_group_member_emails(directory, group_email)
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


def _count_actions(rows: List[ResultRow]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row.action] = counts.get(row.action, 0) + 1
    return counts


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
    sheets=None,
    sheet_id: Optional[str] = None,
    gmail_factory: GmailFactory = sa_auth.build_gmail_for_user,
) -> Tuple[List[ResultRow], Dict[str, Any]]:
    """Apply (or dry-run) every user in exactly one scope.

    Checks, in order: exactly one scope; the live gate; the ledger (live
    runs only, before any write); the user count against ``max_users``
    (refused with the count, never truncated). Then each user is handled
    on its own: a user that cannot be planned at all becomes one ``error``
    row with send-as ``*``. Every row is written as JSONL into the
    attachment store and ``report_meta`` carries the access line.
    """
    label = scope_label(ou_path=ou_path, domain=domain, group_email=group_email)
    _gate_live(dry_run, confirm)
    if not isinstance(max_users, int) or max_users < 1:
        raise UserInputError(
            f"max_users must be a positive integer, got {max_users!r}."
        )

    latest = await _resolve_ledger(dry_run, None, sheets, sheet_id)
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
                )
            )
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
    "stale_template",
    "changed_since_apply",
    "error",
)

# Statuses that mean a managed address is not what the ledger says it
# should be. The CLI exits 2 when any row carries one of these.
DRIFT_STATUSES = frozenset(
    {"never_applied", "stale_template", "changed_since_apply", "error"}
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
    "DEFAULT_MAX_USERS",
    "DRIFT_STATUSES",
    "LIVE_CONFIRM_MESSAGE",
    "USER_LEVEL_SEND_AS",
    "Runtime",
    "apply_scope",
    "apply_user",
    "audit_counts",
    "audit_scope",
    "build_runtime",
    "format_audit_counts",
    "format_audit_table",
    "has_drift",
    "new_run_id",
    "plan_user",
    "prepare_ledger",
    "read_ledger_best_effort",
    "scope_label",
    "utc_now_iso",
]

"""
MCP tools for centrally managed Gmail signatures (opt-in service ``gsignatures``).

These six tools are thin. Every decision lives in ``gsignatures/engine.py``
and every Google call in ``gsignatures/operations.py``; this module resolves
the caller, checks the allowlist, builds the clients and formats the result.

Auth model, and why there is no ``require_google_service`` here
---------------------------------------------------------------
The other tool modules act with the calling user's own OAuth token. These
tools cannot: a user's token can only edit that user's own primary send-as
signature, and the feature must set signatures on other people's mailboxes
and on their aliases. So the work is done by a Google service account with
domain-wide delegation (``gsignatures/sa_auth.py``), impersonating each user
for Gmail settings and the configured admin for Directory reads, with the
ledger Sheet written as the service account itself.

That makes the caller gate essential. The service account can act as anyone
in the tenant, so the only thing standing between a connected MCP client
and every mailbox's signature is the check at the top of each tool: the
authenticated caller (read from the FastMCP request context, exactly as the
audit logger reads it) must be on ``SIGNATURE_ADMIN_EMAILS``. No context, an
empty identity, or an address off the list is refused before any client is
built. The gate is in the function body, not a decorator, so it cannot be
peeled off.

Write safety: the three write tools (set, apply, restore) default to
``dry_run=True``, and a live write needs ``dry_run=False`` AND
``confirm=True``. Every live apply is recorded in the ledger Sheet before the
tool returns; a live run is refused outright when the ledger cannot be
reached. The write tools also carry ``_workspace_write_tool = True`` (set by
``_write_tool``), which ``core.tool_registry.filter_server_tools`` honours in
``--read-only`` mode, so a read-only server drops them at registration; and
each refuses a live run in the body when the server is read-only, in case a
future registry change stops reading the marker. The read-only mode of the
server is honoured even though these tools hold no OAuth scope, because the
mode is a promise about the server, not about a scope.

Errors from the feature's own setup (``SignatureAuthError``, ``LedgerError``,
``SignatureConfigError``) are raised as ``UserInputError`` so the message
reaches the client instead of being flattened to a generic failure.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Awaitable, List, Optional, TypeVar

from auth.scopes import is_read_only_mode
from core.server import server
from core.utils import UserInputError, handle_http_errors

from gsignatures import operations
from gsignatures.engine import (
    PlannedSignature,
    SignatureConfigError,
    drift_status,
    extract_person,
    format_result_table,
    signature_hash,
)
from gsignatures.ledger import LedgerError, write_audit_report
from gsignatures.operations import build_runtime
from gsignatures.sa_auth import SignatureAuthError, assert_caller_allowed

logger = logging.getLogger(__name__)

T = TypeVar("T")

_SETUP_ERRORS = (SignatureAuthError, LedgerError, SignatureConfigError)

READ_ONLY_MESSAGE = (
    "This server is running in read-only mode; signature writes are disabled. "
    "Nothing was changed. Dry runs still work."
)


def _write_tool(fn):
    """Mark a tool as a write for ``--read-only`` filtering.

    The other write tools are recognised by the scopes ``require_google_service``
    attaches; these tools hold no OAuth scope, so they carry this marker
    instead. Innermost decorator: every wrapper above it uses
    ``functools.wraps``, which copies the attribute outwards to the function
    the registry inspects.
    """
    fn._workspace_write_tool = True
    return fn


def _refuse_if_read_only(dry_run: bool) -> None:
    """A live write on a read-only server is refused before any client is built."""
    if not dry_run and is_read_only_mode():
        raise UserInputError(READ_ONLY_MESSAGE)


# ---------------------------------------------------------------------------
# Caller gate
# ---------------------------------------------------------------------------


async def _resolve_caller_email() -> Optional[str]:
    """The authenticated caller from the FastMCP request context, or ``None``.

    Same source as ``core.audit._resolve_user_email``: the auth middleware
    sets ``authenticated_user_email`` on the request context. Unlike the
    audit logger there is no fallback identity here; ``None`` is refused by
    the gate.
    """
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
        if ctx is None:
            return None
        email = await ctx.get_state("authenticated_user_email")
        return email or None
    except Exception as exc:  # no live request, or a context API change
        logger.debug("signature tools: no caller context (%s)", exc)
        return None


async def _require_allowed_caller() -> str:
    """Refuse unless the request carries an allowlisted identity."""
    email = await _resolve_caller_email()
    try:
        assert_caller_allowed(email)
    except SignatureAuthError as exc:
        raise UserInputError(str(exc)) from exc
    return (email or "").strip().lower()


async def _translated(awaitable: Awaitable[T]) -> T:
    """Await, turning the feature's setup errors into ``UserInputError``."""
    try:
        return await awaitable
    except _SETUP_ERRORS as exc:
        raise UserInputError(str(exc)) from exc


def _require_email(value: Optional[str], what: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned or "@" not in cleaned:
        raise UserInputError(f"{what} must be an email address, got {value!r}.")
    return cleaned


def _pick_plan(
    planned: List[PlannedSignature], user_email: str, send_as_email: Optional[str]
) -> PlannedSignature:
    if send_as_email and send_as_email.strip():
        wanted = send_as_email.strip().lower()
        for plan in planned:
            if plan.send_as_email.lower() == wanted:
                return plan
        available = ", ".join(p.send_as_email for p in planned) or "(none)"
        raise UserInputError(
            f"{user_email} has no send-as address {send_as_email.strip()}. "
            f"Available: {available}."
        )
    for plan in planned:
        if plan.is_primary:
            return plan
    raise UserInputError(f"{user_email} has no primary send-as address in Gmail.")


def _short(value: Optional[str]) -> str:
    return (value or "")[:12] or "(empty)"


# ---------------------------------------------------------------------------
# Implementations (called after the gate)
# ---------------------------------------------------------------------------


async def _preview(user_email: str, send_as_email: Optional[str]) -> str:
    rt = build_runtime(need_ledger=False)
    user, _, planned = await operations.plan_user(
        rt.config, rt.directory, user_email, gmail_factory=rt.gmail_factory
    )
    plan = _pick_plan(planned, user_email, send_as_email)
    lines = [f"Signature preview for {plan.user_email} (send-as {plan.send_as_email})"]
    if plan.status == "skipped":
        lines.append(f"   Not managed: {plan.reason}")
        return "\n".join(lines)

    entity = rt.config.entities[plan.entity] if plan.entity else None
    if entity is not None:
        lines.append(f"   Entity: {entity.code} ({entity.legal_name})")
        lines.append(
            f"   Versions: template {entity.template_version}, "
            f"statutory {entity.statutory_version}"
        )
        try:
            person = extract_person(user, entity)
            lines.append(f"   Name: {person.name}")
            lines.append(f"   Title: {person.title}")
            lines.append(f"   Mobile: {person.mobile or '(none)'}")
        except ValueError as exc:  # MissingDirectoryDataError
            lines.append(f"   Directory data: {exc}")
        lines.append(f"   Email in signature: {plan.send_as_email}")
        if not entity.statutory_verified:
            lines.append(
                f"   WARNING: {entity.code} has statutory_verified: false in "
                "entities.yaml. Verify the company number, VAT number and "
                "registered office against Companies House before a live apply."
            )
    if plan.status == "error":
        lines.append(f"   Cannot render: {plan.reason}")
        return "\n".join(lines)

    lines.append(f"   Rendered hash: {plan.rendered_hash}")
    lines.append("")
    lines.append("Rendered HTML:")
    lines.append(plan.html or "")
    return "\n".join(lines)


async def _get(user_email: str) -> str:
    rt = build_runtime(need_ledger=False)
    user, send_as_list, planned = await operations.plan_user(
        rt.config, rt.directory, user_email, gmail_factory=rt.gmail_factory
    )
    ledger_latest, ledger_note = await operations.read_ledger_best_effort(
        rt.sheets, rt.sheet_id
    )
    primary_email = str(user.get("primaryEmail") or user_email).lower()
    plans = {p.send_as_email.lower(): p for p in planned}

    lines = [f"Send-as addresses for {user.get('primaryEmail') or user_email}:"]
    if ledger_note:
        lines.append(f"   Drift: {ledger_note}; drift is not judged in this output.")
    for entry in send_as_list:
        address = str(entry.get("sendAsEmail") or "")
        flags = []
        if entry.get("isPrimary"):
            flags.append("primary")
        if entry.get("isDefault"):
            flags.append("default")
        flag_text = f" ({', '.join(flags)})" if flags else ""
        current_html = str(entry.get("signature") or "")
        current_hash = signature_hash(current_html)
        plan = plans.get(address.lower())

        lines.append(f"- {address}{flag_text}")
        lines.append(f"   displayName: {entry.get('displayName') or '(none)'}")
        lines.append(f"   current signature hash: {_short(current_hash)}")
        if plan is None:
            lines.append("   entity: (no plan)")
            continue
        if plan.entity:
            lines.append(f"   entity: {plan.entity}")
            lines.append(
                f"   expected versions: template {plan.template_version}, "
                f"statutory {plan.statutory_version}"
            )
        else:
            lines.append(f"   entity: not managed ({plan.reason})")
        if plan.status == "error":
            lines.append(f"   plan: error ({plan.reason})")
        if ledger_latest is None:
            lines.append("   drift: ledger unavailable")
        else:
            ledger_row = ledger_latest.get((primary_email, address.lower()))
            status, reason = drift_status(plan, current_html, ledger_row)
            lines.append(f"   drift: {status} ({reason})")
    return "\n".join(lines)


def _mode_word(dry_run: bool) -> str:
    return "DRY RUN (no change made)" if dry_run else "LIVE"


def _ledger_note_lines(rows) -> List[str]:
    """One header line when a dry run could not read the ledger."""
    prefixes = (
        operations.LEDGER_NOT_CONFIGURED_NOTE,
        operations.LEDGER_UNAVAILABLE_NOTE_PREFIX,
    )
    notes = sorted(
        {
            r.reason.split("; ", 1)[0]
            for r in rows
            if r.action == "would_apply"
            and r.reason
            and r.reason.startswith(prefixes)
            and "; " in r.reason
        }
    )
    if not notes:
        return []
    return [
        "Ledger: "
        + "; ".join(notes)
        + ". Drift was not judged: every would_apply row says so. Fix the "
        "ledger before a live run; a live run with this ledger is refused."
    ]


async def _set(
    actor: str,
    user_email: str,
    send_as_email: Optional[str],
    dry_run: bool,
    confirm: bool,
    force: bool,
) -> str:
    # Switch checks first, so the caller sees the right refusal even when the
    # runtime cannot be built (no ledger env, no key).
    _refuse_if_read_only(dry_run)
    operations.gate_live(dry_run, confirm)
    rt = build_runtime(need_ledger=not dry_run)
    run_id = operations.new_run_id()
    rows = await operations.apply_user(
        rt.config,
        rt.directory,
        user_email,
        actor=actor,
        run_id=run_id,
        dry_run=dry_run,
        confirm=confirm,
        force=force,
        include_aliases=True,
        sheets=rt.sheets,
        sheet_id=rt.sheet_id,
        gmail_factory=rt.gmail_factory,
        only_send_as=send_as_email,
        primary_only=not (send_as_email and send_as_email.strip()),
    )
    lines = [
        f"{_mode_word(dry_run)} | run_id {run_id} | actor {actor}",
        *_ledger_note_lines(rows),
        format_result_table(rows),
    ]
    if dry_run:
        lines.append(
            "Notes: nothing was written. To apply, repeat with dry_run=False "
            "and confirm=True."
        )
    else:
        lines.append(
            "Notes: every applied row has a matching ledger row (Ledger tab) "
            "with the previous signature HTML for rollback. Keep this table."
        )
    return "\n".join(lines)


async def _restore(
    actor: str,
    user_email: str,
    send_as_email: Optional[str],
    run_id_to_restore: Optional[str],
    dry_run: bool,
    confirm: bool,
) -> str:
    _refuse_if_read_only(dry_run)
    operations.gate_live(dry_run, confirm)
    # The row being restored lives in the ledger, so even a dry run needs it.
    rt = build_runtime(need_ledger=True)
    run_id = operations.new_run_id()
    rows = await operations.restore_user(
        user_email,
        send_as_email,
        actor=actor,
        run_id=run_id,
        from_run_id=run_id_to_restore,
        dry_run=dry_run,
        confirm=confirm,
        sheets=rt.sheets,
        sheet_id=rt.sheet_id,
        gmail_factory=rt.gmail_factory,
    )
    lines = [
        f"RESTORE {_mode_word(dry_run)} | run_id {run_id} | actor {actor}",
        format_result_table(rows),
    ]
    if dry_run:
        lines.append(
            "Notes: nothing was written. To restore, repeat with dry_run=False "
            "and confirm=True."
        )
    else:
        lines.append(
            "Notes: the restore is recorded in the Ledger tab with versions "
            "'restored' and the replaced signature in previous_signature_html, "
            "so it can itself be reversed. The next apply will re-apply the "
            "managed signature unless the address is excluded first."
        )
    return "\n".join(lines)


async def _apply(
    actor: str,
    ou_path: Optional[str],
    domain: Optional[str],
    group_email: Optional[str],
    include_aliases: bool,
    dry_run: bool,
    confirm: bool,
    force: bool,
    max_users: int,
) -> str:
    # Scope and switch checks first, so the caller sees the right refusal
    # even when the runtime cannot be built (no ledger env, no key).
    operations.scope_label(ou_path=ou_path, domain=domain, group_email=group_email)
    _refuse_if_read_only(dry_run)
    operations.gate_live(dry_run, confirm)
    rt = build_runtime(need_ledger=not dry_run)
    run_id = operations.new_run_id()
    rows, meta = await operations.apply_scope(
        rt.config,
        rt.directory,
        ou_path=ou_path,
        domain=domain,
        group_email=group_email,
        actor=actor,
        run_id=run_id,
        dry_run=dry_run,
        confirm=confirm,
        force=force,
        include_aliases=include_aliases,
        max_users=max_users,
        sheets=rt.sheets,
        sheet_id=rt.sheet_id,
        gmail_factory=rt.gmail_factory,
    )
    lines = [
        f"Scope {meta['scope']} | run_id {run_id} | {_mode_word(dry_run)} | "
        f"actor {actor} | users {meta['user_count']}",
        *(
            [
                f"Ledger: {meta['ledger_note']}. Drift was not judged: every "
                "would_apply row says so. Fix the ledger before a live run; a "
                "live run with this ledger is refused."
            ]
            if meta.get("ledger_note")
            else []
        ),
        *(
            [
                "LEDGER FAILED MID-RUN: after the first failed ledger append "
                "no further address was patched; those rows read 'not "
                "attempted'. Record the 'applied but the ledger append "
                "failed' rows by hand, fix the ledger, then run again."
            ]
            if meta.get("ledger_failed")
            else []
        ),
        format_result_table(rows),
        f"Report: {meta['report_filename']}. {meta['access_line']}",
        "Keep this table and the report: together with the Ledger tab they are "
        "the evidence of what was applied."
        if not dry_run
        else "Keep this table: it is the evidence of what a live run would do. "
        "Repeat with dry_run=False and confirm=True to apply.",
    ]
    return "\n".join(lines)


async def _audit(
    ou_path: Optional[str],
    domain: Optional[str],
    group_email: Optional[str],
    all_users: bool,
    write_report: bool,
) -> str:
    rt = build_runtime(need_ledger=True)
    ledger_latest = await operations.prepare_ledger(rt.sheets, rt.sheet_id)
    rows = await operations.audit_scope(
        rt.config,
        rt.directory,
        ou_path=ou_path,
        domain=domain,
        group_email=group_email,
        all_users=all_users,
        ledger_latest=ledger_latest,
        gmail_factory=rt.gmail_factory,
    )
    label = operations.scope_label(
        ou_path=ou_path, domain=domain, group_email=group_email, all_users=all_users
    )
    lines = [
        f"Signature audit | scope {label} | rows {len(rows)}",
        operations.format_audit_counts(rows),
        operations.format_audit_table(rows),
    ]
    if write_report:
        tab = f"Audit_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        await write_audit_report(rt.sheets, rt.sheet_id or "", tab, rows)
        lines.append(f"Report written to tab {tab} of the ledger Sheet.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@server.tool()
@handle_http_errors("preview_email_signature", is_read_only=True, service_type="gmail")
async def preview_email_signature(
    user_email: str, send_as_email: Optional[str] = None
) -> str:
    """
    Renders the signature one user would get, without writing anything.

    Shows the entity, the pinned template and statutory versions, the
    Directory fields used (name, title, mobile or '(none)'), a warning when
    the entity's statutory values are not yet verified, then the HTML.
    Caller must be on SIGNATURE_ADMIN_EMAILS.

    Args:
        user_email (str): The user's primary address. Required.
        send_as_email (Optional[str]): One of the user's send-as addresses.
            Defaults to the primary.

    Returns:
        str: The preview, or the reason the address is not managed.
    """
    await _require_allowed_caller()
    user_email = _require_email(user_email, "user_email")
    return await _translated(_preview(user_email, send_as_email))


@server.tool()
@handle_http_errors("get_email_signatures", is_read_only=True, service_type="gmail")
async def get_email_signatures(user_email: str) -> str:
    """
    Lists a user's send-as addresses with their current signature state.

    One block per address: primary and default flags, display name, the
    current signature hash (or '(empty)'), the resolved entity or the skip
    reason, the expected versions, and the drift status against the ledger.
    Read-only. When the ledger cannot be read the block says 'ledger
    unavailable' rather than failing. Caller must be on SIGNATURE_ADMIN_EMAILS.

    Args:
        user_email (str): The user's primary address. Required.

    Returns:
        str: One block per send-as address.
    """
    await _require_allowed_caller()
    user_email = _require_email(user_email, "user_email")
    return await _translated(_get(user_email))


@server.tool()
@handle_http_errors("set_email_signature", service_type="gmail")
@_write_tool
async def set_email_signature(
    user_email: str,
    send_as_email: Optional[str] = None,
    dry_run: bool = True,
    confirm: bool = False,
    force: bool = False,
) -> str:
    """
    Sets the managed signature on one send-as address (the primary by default).

    Dry run by default. A live write needs dry_run=False AND confirm=True,
    and a reachable ledger Sheet; every live apply is recorded there with
    the previous signature HTML. An address whose ledger row already matches
    Gmail is reported 'unchanged' unless force=True. force never touches an
    address the rules skip (personal alias, suspended user, excluded OU).
    Refused on a read-only server. Caller must be on SIGNATURE_ADMIN_EMAILS.

    Args:
        user_email (str): The user's primary address. Required.
        send_as_email (Optional[str]): The send-as address to set. Defaults
            to the primary.
        dry_run (bool): Report without writing. Defaults to True.
        confirm (bool): Second switch for a live write. Defaults to False.
        force (bool): Re-apply even when the ledger says it is unchanged.

    Returns:
        str: Mode line, result table (user, send-as, entity, template,
            action, before and after hash, reason) and notes.
    """
    actor = await _require_allowed_caller()
    user_email = _require_email(user_email, "user_email")
    return await _translated(
        _set(actor, user_email, send_as_email, dry_run, confirm, force)
    )


@server.tool()
@handle_http_errors("apply_email_signatures", service_type="gmail")
@_write_tool
async def apply_email_signatures(
    ou_path: Optional[str] = None,
    domain: Optional[str] = None,
    group_email: Optional[str] = None,
    include_aliases: bool = True,
    dry_run: bool = True,
    confirm: bool = False,
    force: bool = False,
    max_users: int = operations.DEFAULT_MAX_USERS,
) -> str:
    """
    Applies managed signatures across one scope: an OU, a domain or a group.

    Exactly one scope. Dry run by default; a live write needs dry_run=False
    AND confirm=True and a reachable ledger. A scope with more users than
    max_users is refused with the count (never truncated). One failing
    address or user is an error row; the rest continue, except that after a
    failed ledger append nothing further is patched in that run. Every row
    is also written as a JSONL report. Refused on a read-only server. Caller
    must be on SIGNATURE_ADMIN_EMAILS.

    Args:
        ou_path (Optional[str]): Organisational unit path, e.g. '/01 OTB'
            (includes child OUs).
        domain (Optional[str]): A primary-address domain, e.g. 'jit-logistics.com'.
        group_email (Optional[str]): A group; direct user members only.
        include_aliases (bool): Also set non-primary send-as addresses.
            Defaults to True.
        dry_run (bool): Report without writing. Defaults to True.
        confirm (bool): Second switch for a live write. Defaults to False.
        force (bool): Re-apply addresses the ledger says are unchanged.
        max_users (int): Refuse scopes larger than this. Defaults to 200.

    Returns:
        str: Header (scope, run_id, mode, actor), the result table, the
            JSONL report access line and a reminder to keep the table.
    """
    actor = await _require_allowed_caller()
    return await _translated(
        _apply(
            actor,
            ou_path,
            domain,
            group_email,
            include_aliases,
            dry_run,
            confirm,
            force,
            max_users,
        )
    )


@server.tool()
@handle_http_errors("audit_email_signatures", is_read_only=True, service_type="gmail")
async def audit_email_signatures(
    ou_path: Optional[str] = None,
    domain: Optional[str] = None,
    group_email: Optional[str] = None,
    all_users: bool = False,
    write_report: bool = False,
) -> str:
    """
    Compares every send-as in one scope with the ledger. Never writes a signature.

    Exactly one scope (all_users=True counts as one). Statuses: in_sync,
    unmanaged, never_applied, stale_template, stale_directory (the person's
    Directory data changed since the apply, so the signature is out of
    date), changed_since_apply, error. With write_report=True the rows are
    also written to an 'Audit_<UTC date>' tab of the ledger Sheet. Caller
    must be on SIGNATURE_ADMIN_EMAILS.

    Args:
        ou_path (Optional[str]): Organisational unit path (includes children).
        domain (Optional[str]): A primary-address domain.
        group_email (Optional[str]): A group; direct user members only.
        all_users (bool): Every active user in the tenant. Defaults to False.
        write_report (bool): Also write the Audit_<date> tab. Defaults to False.

    Returns:
        str: Counts per status, then a table (user, send_as, entity, status,
            reason).
    """
    await _require_allowed_caller()
    return await _translated(
        _audit(ou_path, domain, group_email, all_users, write_report)
    )


@server.tool()
@handle_http_errors("restore_email_signature", service_type="gmail")
@_write_tool
async def restore_email_signature(
    user_email: str,
    send_as_email: Optional[str] = None,
    run_id: Optional[str] = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> str:
    """
    Puts back the previous signature the ledger recorded for one send-as address.

    Uses the latest ledger row for the address (or the row from a given
    run_id) and restores its previous_signature_html: the signature that was
    in place before that apply. An empty previous signature clears the
    signature. Dry run by default; a live restore needs dry_run=False AND
    confirm=True and a writable ledger, and is itself recorded as a ledger
    row (versions 'restored') so it can be reversed. The next apply will
    re-apply the managed signature. Refused on a read-only server. Caller
    must be on SIGNATURE_ADMIN_EMAILS.

    Args:
        user_email (str): The user's primary address. Required.
        send_as_email (Optional[str]): The send-as address to restore.
            Defaults to the primary.
        run_id (Optional[str]): Restore from the ledger row of this run_id
            instead of the latest row for the address.
        dry_run (bool): Report without writing. Defaults to True.
        confirm (bool): Second switch for a live restore. Defaults to False.

    Returns:
        str: Mode line, a one-row result table and notes.
    """
    actor = await _require_allowed_caller()
    user_email = _require_email(user_email, "user_email")
    return await _translated(
        _restore(actor, user_email, send_as_email, run_id, dry_run, confirm)
    )

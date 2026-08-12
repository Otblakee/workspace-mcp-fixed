"""
Shared plumbing for the Drive architecture / migration tools.

Everything in here is deliberately transport-agnostic and free of
``@server.tool`` decorators so it can be unit-tested without touching the
FastMCP registration chokepoint.

Three concerns live here:

1. **Retry / backoff** (``execute_with_backoff``, ``paginate``) — every batch
   tool in this repo is expected to survive Drive's rate limiter, so the retry
   policy is written once and shared.
2. **Guardrails** (``resolve_principal``, ``validate_drive_role``) — the
   groups-only permission rule from the Build Sheet decision of 2026-01-21.
3. **Manifests** (``parse_manifest``, ``write_jsonl_report``) — the migration
   tools all speak JSONL and all hand their output to the attachment store so
   the caller can fetch it over HTTP without the bytes passing through an MCP
   tool result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from googleapiclient.errors import HttpError

from core.attachment_storage import get_attachment_storage, get_attachment_url
from core.config import get_transport_mode
from core.utils import UserInputError, validate_file_path

logger = logging.getLogger(__name__)

SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

# Drive roles accepted on shared drives and files. ``owner`` is deliberately
# absent: ownership transfer is a blocked capability on this deployment
# (see core/tool_policy.py).
VALID_DRIVE_ROLES = (
    "organizer",
    "fileOrganizer",
    "writer",
    "commenter",
    "reader",
)

# Permission principal types this server will ever create. ``anyone`` and
# ``domain`` are refused outright — a public or domain-wide link is a
# permanent leak and is the exact failure mode the tool denylist exists to
# prevent.
ALLOWED_PRINCIPAL_TYPES = ("group", "user")

# Retry classification turns on ONE question: did the server definitely not
# perform the operation?
#
# A rate-limit rejection is a guarantee that nothing happened, so retrying is
# always safe. A 5xx or a dropped connection is ambiguous — the write may have
# been committed and only the response lost — so retrying a non-idempotent
# mutation there can duplicate a copied file, a folder, a shortcut, or a group
# membership. Those are retried only when the caller declares the request
# idempotent.
_REJECTED_STATUSES = frozenset({429})
_AMBIGUOUS_STATUSES = frozenset({500, 502, 503, 504})

# Drive signals rate limiting as 403 + reason rather than 429. Every reason
# here means the request was refused outright.
_REJECTED_REASONS = frozenset(
    {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "sharingRateLimitExceeded",
        "quotaExceeded",
    }
)
# Server-side faults reported with a 5xx-style reason. Ambiguous, same as 5xx.
_AMBIGUOUS_REASONS = frozenset({"backendError", "internalError"})

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_DELAY_S = 1.0
# Ceiling on a single backoff sleep so a batch tool can't wedge a request
# thread for minutes on a persistent 503.
MAX_BACKOFF_DELAY_S = 32.0


class BatchRowError(Exception):
    """A single manifest row failed. Batch tools catch this and continue."""


def _http_error_reason(error: HttpError) -> str:
    """Best-effort extraction of Google's machine-readable error reason."""
    try:
        payload = json.loads(error.content.decode("utf-8"))
    except Exception:
        return ""
    errors = (payload.get("error") or {}).get("errors") or []
    if errors and isinstance(errors, list):
        return str(errors[0].get("reason") or "")
    return str((payload.get("error") or {}).get("status") or "")


def is_retryable_http_error(error: HttpError, *, idempotent: bool = True) -> bool:
    """True when the failure is transient AND retrying is safe.

    ``idempotent=False`` restricts retries to failures that prove the server
    did not perform the operation. See the constants above for why.
    """
    status = getattr(getattr(error, "resp", None), "status", None)
    reason = _http_error_reason(error)

    # Definitely refused — safe to retry whatever the operation is.
    if status in _REJECTED_STATUSES:
        return True
    if status == 403:
        if reason in _REJECTED_REASONS:
            return True
        if reason in _AMBIGUOUS_REASONS:
            return idempotent
        return False

    # Outcome unknown: the mutation may already have been committed.
    if status in _AMBIGUOUS_STATUSES:
        return idempotent

    return False


async def execute_with_backoff(
    request_factory: Callable[[], Any],
    *,
    label: str = "drive-call",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY_S,
    idempotent: bool = True,
) -> Any:
    """Execute a googleapiclient request with exponential backoff + jitter.

    ``request_factory`` must return a *fresh* request object on every call —
    re-executing a consumed request is not guaranteed to be safe across
    googleapiclient versions.

    ``idempotent`` declares whether replaying the request is harmless. Pass
    ``False`` for creating mutations — ``files.create``, ``files.copy``,
    ``groups.insert``, ``members.insert`` — where a retry after an ambiguous
    failure could produce a duplicate file, folder, shortcut or membership.
    Those calls still retry on rate-limit rejections, which are proof the
    operation did not happen; they stop on 5xx and dropped connections, where
    the write may already have landed. The caller's own preflight check plus a
    re-run of the manifest is the recovery path.

    Reads default to ``True``: replaying a list or get costs a request and
    nothing else.

    Non-retryable errors propagate immediately so the caller's
    ``handle_http_errors`` wrapper can render them.
    """
    last_error: Optional[BaseException] = None
    for attempt in range(max_attempts):
        try:
            request = request_factory()
            return await asyncio.to_thread(request.execute)
        except HttpError as error:
            if (
                not is_retryable_http_error(error, idempotent=idempotent)
                or attempt == max_attempts - 1
            ):
                raise
            last_error = error
        except (TimeoutError, ConnectionError) as error:
            # Ambiguous by definition: the request may have reached Google.
            if not idempotent or attempt == max_attempts - 1:
                raise
            last_error = error

        delay = min(base_delay * (2**attempt), MAX_BACKOFF_DELAY_S)
        # Full jitter: spreads retries when many rows in a batch trip the
        # limiter in the same second.
        delay = random.uniform(delay / 2, delay)
        logger.warning(
            "[%s] transient error on attempt %d/%d (%s); retrying in %.1fs",
            label,
            attempt + 1,
            max_attempts,
            last_error,
            delay,
        )
        await asyncio.sleep(delay)

    # Unreachable: the final attempt either returns or raises.
    raise AssertionError(f"[{label}] backoff loop exited without a result")


async def paginate(
    request_factory: Callable[[Optional[str]], Any],
    *,
    items_key: str = "files",
    max_items: Optional[int] = None,
    label: str = "drive-list",
) -> List[Dict[str, Any]]:
    """Drain every page of a Drive/Admin list endpoint.

    ``request_factory(page_token)`` must build a fresh request for the given
    token (``None`` for the first page). Pagination is exhaustive by design —
    a partially-drained list is how the previous crawl lost six folders and a
    whole shared drive.
    """
    collected: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    pages = 0
    while True:
        response = await execute_with_backoff(
            lambda token=page_token: request_factory(token), label=label
        )
        pages += 1
        collected.extend(response.get(items_key, []) or [])
        page_token = response.get("nextPageToken")
        if not page_token:
            break
        if max_items is not None and len(collected) >= max_items:
            logger.warning(
                "[%s] stopped at max_items=%d with more pages available; "
                "results are a floor, not a complete inventory",
                label,
                max_items,
            )
            break
    if max_items is not None:
        return collected[:max_items]
    return collected


# --- Guardrails -------------------------------------------------------------


def validate_drive_role(role: str) -> str:
    """Validate a Drive permission role, returning it unchanged."""
    if role not in VALID_DRIVE_ROLES:
        raise UserInputError(
            f"Invalid role '{role}'. Must be one of: {', '.join(VALID_DRIVE_ROLES)}. "
            "('owner' is not available: ownership transfer is disabled on this "
            "deployment.)"
        )
    return role


def _allowed_permission_domains() -> List[str]:
    """Domains an added principal may belong to.

    Configured via ``DRIVE_PERMISSION_ALLOWED_DOMAINS`` (comma-separated).
    Unset means no domain restriction, which preserves the current
    single-tenant workflow while letting the OTB deploy pin sharing to
    ``otbgroup.co.uk``.
    """
    raw = os.getenv("DRIVE_PERMISSION_ALLOWED_DOMAINS", "").strip()
    if not raw:
        return []
    return [d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()]


def resolve_principal(
    principal: str,
    *,
    allow_individual: bool = False,
) -> Tuple[str, str]:
    """Apply the address-shape checks and return the declared ``(type, email)``.

    The Build Sheet decision of 2026-01-21 is that Drive access is granted to
    Google Groups, never to individuals: an individual grant is invisible to
    the group-membership model and survives offboarding. Passing
    ``allow_individual=True`` is the documented, explicit escape hatch.

    IMPORTANT — this function does NOT establish that the principal is a group.
    It was originally written on the assumption that Drive rejects a
    ``type=group`` permission whose address belongs to a person. Live testing
    on 2026-08-12 disproved that: Drive **accepted** the request and silently
    created a ``type=user`` permission instead. The declared type is a request,
    not a constraint.

    The actual enforcement is ``assert_principal_is_group`` below, which
    resolves the address against the Directory API before any grant is made.
    """
    email = (principal or "").strip()
    if not email or "@" not in email:
        raise UserInputError(
            f"principal must be an email address; got {principal!r}. "
            "Public ('anyone') and domain-wide sharing are not available on "
            "this deployment."
        )
    lowered = email.lower()
    # Exact match only. The bare Drive principal types carry no "@" and are
    # already refused above; matching on a prefix here would reject a real
    # group whose address merely starts with one of these words (an
    # ``anyone@…`` distribution list is a perfectly ordinary Workspace group).
    if lowered in {"domain", "anyone", "anyonewithlink"}:
        raise UserInputError(
            "Public / domain-wide sharing is refused: a public link is a "
            "permanent leak. Grant access to a group instead."
        )

    domain = lowered.rsplit("@", 1)[-1]
    allowed = _allowed_permission_domains()
    if allowed and domain not in allowed:
        raise UserInputError(
            f"principal '{email}' is outside the allowed domains "
            f"({', '.join(allowed)}). Set DRIVE_PERMISSION_ALLOWED_DOMAINS to "
            "change this."
        )

    if allow_individual:
        return "user", email
    return "group", email


# --- Manifests --------------------------------------------------------------


def parse_manifest(
    manifest_json: Optional[str] = None,
    manifest_path: Optional[str] = None,
    *,
    required_keys: Iterable[str] = (),
) -> List[Dict[str, Any]]:
    """Load a manifest from an inline payload or an on-disk JSONL/JSON file.

    Accepts, in order of preference:
      * ``manifest_json`` — a JSON array, a single JSON object, or JSONL text.
      * ``manifest_path`` — a path to a ``.jsonl`` (one object per line) or
        ``.json`` file, validated by ``core.utils.validate_file_path``.

    Raises ``UserInputError`` on anything malformed so the caller gets a
    non-retryable, human-readable failure.
    """
    if manifest_json and manifest_path:
        raise UserInputError("Pass either manifest_json or manifest_path, not both.")
    if not manifest_json and not manifest_path:
        raise UserInputError(
            "A manifest is required: pass manifest_json or manifest_path."
        )

    if manifest_path:
        resolved = validate_file_path(manifest_path)
        text = Path(resolved).read_text(encoding="utf-8")
    else:
        text = manifest_json or ""

    rows = _parse_manifest_text(text)

    missing_report = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise UserInputError(
                f"Manifest row {index} is a {type(row).__name__}, expected an object."
            )
        missing = [k for k in required_keys if not row.get(k)]
        if missing:
            missing_report.append(f"row {index}: missing {', '.join(missing)}")
    if missing_report:
        raise UserInputError(
            "Manifest is missing required fields — "
            + "; ".join(missing_report[:10])
            + (" …" if len(missing_report) > 10 else "")
        )
    return rows


def _parse_manifest_text(text: str) -> List[Dict[str, Any]]:
    stripped = (text or "").strip()
    if not stripped:
        raise UserInputError("Manifest is empty.")

    # Whole-document JSON first (array or single object).
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return parsed
        raise UserInputError(
            f"Manifest JSON must be an object or array; got {type(parsed).__name__}."
        )

    # Fall back to JSONL.
    rows: List[Dict[str, Any]] = []
    for line_no, line in enumerate(stripped.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise UserInputError(
                f"Manifest line {line_no} is not valid JSON: {exc}"
            ) from exc
    if not rows:
        raise UserInputError("Manifest contained no rows.")
    return rows


def write_jsonl_report(
    rows: Iterable[Dict[str, Any]],
    *,
    filename: str,
) -> Tuple[str, str, str]:
    """Stream ``rows`` to a JSONL file in the attachment store.

    Rows are serialised one at a time so a 100k-row inventory never exists in
    memory as a single string. Returns ``(attachment_id, path, access_line)``
    where ``access_line`` is the transport-appropriate way for the caller to
    fetch it (local path on stdio, expiring URL on HTTP).
    """
    storage = get_attachment_storage()
    attachment_id, target_path = storage.reserve_path(filename)

    fd = os.open(
        target_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
        0o600,
    )
    written = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                written += 1
    except Exception:
        try:
            os.unlink(target_path)
        except OSError:  # pragma: no cover - best effort
            pass
        raise

    storage.register_existing_file(
        file_id=attachment_id,
        file_path=target_path,
        filename=filename,
        mime_type="application/x-ndjson",
        size=os.path.getsize(target_path),
    )
    logger.info("[write_jsonl_report] wrote %d rows to %s", written, target_path)
    return attachment_id, target_path, format_access_line(attachment_id, target_path)


def format_access_line(attachment_id: str, path: str) -> str:
    """Render the transport-appropriate access hint for a stored report."""
    if get_transport_mode() == "stdio":
        return f"path: {path}"
    return f"url: {get_attachment_url(attachment_id)} (expires in 1 hour)"


def summarise_counts(counts: Dict[str, int]) -> str:
    """Render a ``key: value`` count block for tool output."""
    return "\n".join(f"   {k}: {v}" for k, v in counts.items())

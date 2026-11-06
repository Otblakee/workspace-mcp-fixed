"""
MCP tools for the OTB CDN (opt-in service ``gcdn``).

Two tools. ``upload_cdn_asset`` PUTs one object into the Cloudflare R2 bucket
behind ``cdn.otbgroup.co.uk`` through the Cloudflare REST API and proves it
is served; ``list_cdn_assets`` lists what is there. Signature logos and
other brand assets referenced by absolute URL live here.

Rules carried by this module:

* Versioned names only (``name-vN.ext``) and never an overwrite: an existing
  key is refused before any upload. A CDN URL is permanent once a signature
  or page points at it, so a changed asset is a new version, not a replaced
  file.
* Every upload is verified by HEAD on the public URL (ETag against the local
  MD5, or sha256 of the served bytes when the ETag is not an MD5), and the
  tool prints the ETag, the MD5 and the sha256.
* The caller gate: the token is held by the server, not by the caller, so
  any connected client could publish to the CDN without one. Each tool
  first resolves the authenticated caller from the FastMCP request context
  (the same source as the audit logger and the signature tools), refuses
  anyone not on ``CDN_ADMIN_EMAILS`` (default ``oliver@otbgroup.co.uk``),
  and accepts only the server's OAuth 2.1 authentication paths.
* ``upload_cdn_asset`` carries the ``_workspace_write_tool`` marker, so
  ``--read-only`` removes it at registration, and it refuses in the body too.
* The token never appears in a message, a log line or an audit row.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
from typing import Awaitable, Optional, Set, TypeVar

from auth.scopes import is_read_only_mode
from core.server import server
from core.utils import UserInputError, handle_http_errors, validate_file_path

from gcdn import r2_client
from gcdn.r2_client import (
    CdnApiError,
    CdnConfigError,
    CdnKeyError,
    CdnSettings,
    R2Client,
    format_size,
    load_settings,
    public_url,
    validate_key,
    verify_public,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

ENV_ADMIN_EMAILS = "CDN_ADMIN_EMAILS"
DEFAULT_ADMIN_EMAILS = "oliver@otbgroup.co.uk"

# Same two paths the signature tools accept, for the same reason: a raw
# bearer token or a stdio identity is not proof enough to publish to the CDN.
ACCEPTED_AUTH_PATHS = frozenset({"fastmcp_oauth", "mcp_session_binding"})

READ_ONLY_MESSAGE = (
    "This server is running in read-only mode; CDN uploads are disabled. "
    "Nothing was uploaded. list_cdn_assets still works."
)

AUTH_PATH_MESSAGE = (
    "CDN tools accept only callers authenticated through the server's OAuth "
    "2.1 flow (authenticated_via 'fastmcp_oauth' or 'mcp_session_binding'); "
    "this request was authenticated via '{via}'."
)

_SETUP_ERRORS = (CdnConfigError, CdnKeyError, CdnApiError)

MAX_LIST_RESULTS = 1000


def _write_tool(fn):
    """Mark a tool as a write for ``--read-only`` filtering (innermost decorator)."""
    fn._workspace_write_tool = True
    return fn


# ---------------------------------------------------------------------------
# Caller gate
# ---------------------------------------------------------------------------


async def _resolve_caller_email() -> Optional[str]:
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
        if ctx is None:
            return None
        email = await ctx.get_state("authenticated_user_email")
        return email or None
    except Exception as exc:  # no live request, or a context API change
        logger.debug("cdn tools: no caller context (%s)", exc)
        return None


async def _resolve_caller_auth_path() -> Optional[str]:
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
        if ctx is None:
            return None
        via = await ctx.get_state("authenticated_via")
        return via or None
    except Exception as exc:
        logger.debug("cdn tools: no auth path in context (%s)", exc)
        return None


def allowed_admin_emails() -> Set[str]:
    """Addresses allowed to use the CDN tools (``CDN_ADMIN_EMAILS``, comma-separated)."""
    raw = os.environ.get(ENV_ADMIN_EMAILS)
    source = ENV_ADMIN_EMAILS if raw is not None else "default"
    raw = DEFAULT_ADMIN_EMAILS if raw is None else raw
    emails = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not emails:
        raise UserInputError(
            f"{source} contains no email addresses. Set {ENV_ADMIN_EMAILS} to a "
            "comma-separated list, or unset it for the default."
        )
    bad = sorted(e for e in emails if "@" not in e)
    if bad:
        raise UserInputError(
            f"{ENV_ADMIN_EMAILS} contains entries that are not email addresses."
        )
    return emails


async def _require_allowed_caller() -> str:
    email = (await _resolve_caller_email() or "").strip().lower()
    if not email:
        raise UserInputError(
            "CDN tools need an authenticated caller and none was resolved on this request."
        )
    if email not in allowed_admin_emails():
        logger.warning("cdn tool refused for a caller not on %s", ENV_ADMIN_EMAILS)
        raise UserInputError(
            "This caller is not allowed to publish to the CDN. Only the addresses "
            f"in {ENV_ADMIN_EMAILS} may use these tools."
        )
    via = await _resolve_caller_auth_path()
    if via not in ACCEPTED_AUTH_PATHS:
        raise UserInputError(AUTH_PATH_MESSAGE.format(via=via or "unknown"))
    return email


async def _translated(awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except _SETUP_ERRORS as exc:
        raise UserInputError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Client construction (monkeypatched in tests)
# ---------------------------------------------------------------------------


def build_client(settings: CdnSettings) -> R2Client:
    return R2Client(settings)


# ---------------------------------------------------------------------------
# Content sources
# ---------------------------------------------------------------------------


def _decode_base64(value: str, max_bytes: int) -> bytes:
    text = "".join((value or "").split())
    if not text:
        raise UserInputError("base64_content is empty.")
    # Standard base64 (the same alphabet create_drive_file takes), padding
    # restored defensively.
    text += "=" * (-len(text) % 4)
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UserInputError(
            f"base64_content is not valid standard base64 ({exc})."
        ) from None
    if not data:
        raise UserInputError("base64_content decodes to zero bytes.")
    if len(data) > max_bytes:
        raise UserInputError(
            f"Content is {format_size(len(data))}, over the CDN upload limit of "
            f"{format_size(max_bytes)} ({r2_client.ENV_MAX_BYTES})."
        )
    return data


def _read_local_file(path: str, max_bytes: int) -> bytes:
    try:
        resolved = validate_file_path(path)
    except (ValueError, FileNotFoundError) as exc:
        raise UserInputError(f"local_file cannot be read: {exc}") from None
    if not resolved.is_file():
        raise UserInputError(f"local_file is not a regular file: {resolved}")
    size = resolved.stat().st_size
    if size == 0:
        raise UserInputError(f"local_file is empty: {resolved}")
    if size > max_bytes:
        raise UserInputError(
            f"local_file is {format_size(size)}, over the CDN upload limit of "
            f"{format_size(max_bytes)} ({r2_client.ENV_MAX_BYTES})."
        )
    return resolved.read_bytes()


def _content_type_ok(content_type: str) -> str:
    cleaned = (content_type or "").strip()
    if "/" not in cleaned or " " in cleaned.split(";", 1)[0]:
        raise UserInputError(
            f"content_type must be a MIME type such as 'image/png', got {content_type!r}."
        )
    return cleaned


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


def _upload_sync(
    key: str, data: bytes, content_type: str, settings: CdnSettings
) -> str:
    with build_client(settings) as client:
        existing = client.find_object(key)
        if existing is not None:
            raise UserInputError(
                f"Refused: {key!r} already exists in bucket {settings.bucket} "
                f"({format_size(int(existing.get('size') or 0))}, uploaded "
                f"{existing.get('uploaded') or existing.get('last_modified') or '?'}). "
                "CDN objects are never overwritten: upload it under the next "
                "version number instead."
            )
        result = client.put_object(key, data, content_type)
        report = verify_public(client, settings, key, data)

    lines = [
        ("Uploaded " if report["verified"] else "Uploaded, but verification FAILED: ")
        + key,
        f"   bucket: {settings.bucket}",
        f"   size: {format_size(len(data))}",
        f"   content_type: {content_type}",
        f"   sha256: {report['sha256']}",
        f"   md5: {report['md5']}",
        f"   public URL: {report['url']}",
    ]
    api_etag = r2_client.normalise_etag(
        str(result.get("etag") or result.get("http_etag") or "")
    )
    if api_etag:
        lines.append(f"   API ETag: {api_etag}")
    status = report["status"] if report["status"] is not None else "(no answer)"
    lines.append(
        f"   HEAD {report['url']}: HTTP {status}, ETag "
        f"{report['etag'] or '(none)'}"
        + (
            f", content-length {report['content_length']}"
            if report["content_length"] is not None
            else ""
        )
    )
    lines.append(
        "   ETag matches MD5: " + ("yes" if report["etag_matches_md5"] else "no")
    )
    if report["body_sha256_matches"] is not None:
        lines.append(
            "   served sha256 matches: "
            + ("yes" if report["body_sha256_matches"] else "NO")
        )
    if report["note"]:
        lines.append(f"   note: {report['note']}")
    lines.append("   verified: " + ("yes" if report["verified"] else "NO"))
    if not report["verified"]:
        lines.append(
            "   The object was written but could not be confirmed at the public "
            "URL. Check the custom domain, the bucket's public access and the "
            "CDN cache, then re-run list_cdn_assets."
        )
    return "\n".join(lines)


async def _upload(
    path: str,
    content_type: str,
    base64_content: Optional[str],
    local_file: Optional[str],
) -> str:
    key = validate_key(path)
    content_type = _content_type_ok(content_type)
    has_b64 = bool(base64_content and base64_content.strip())
    has_file = bool(local_file and local_file.strip())
    if has_b64 == has_file:
        raise UserInputError(
            "Pass exactly one of base64_content (standard base64 of the bytes) "
            "or local_file (a path on the server)."
        )
    settings = load_settings()
    if has_b64:
        data = _decode_base64(base64_content or "", settings.max_bytes)
    else:
        data = _read_local_file(local_file or "", settings.max_bytes)
    return await asyncio.to_thread(_upload_sync, key, data, content_type, settings)


def _list_sync(prefix: str, max_results: int, settings: CdnSettings) -> str:
    with build_client(settings) as client:
        items = client.list_objects(prefix=prefix, limit=max_results + 1)
    more = len(items) > max_results
    items = items[:max_results]
    header = (
        f"CDN assets in bucket {settings.bucket}"
        + (f" under prefix {prefix!r}" if prefix else "")
        + f": {len(items)} shown"
        + (f" (more exist; raise max_results above {max_results})" if more else "")
    )
    lines = [header]
    for item in items:
        key = str(item.get("key") or "")
        size = item.get("size")
        uploaded = item.get("uploaded") or item.get("last_modified") or ""
        etag = r2_client.normalise_etag(
            str(item.get("etag") or item.get("http_etag") or "")
        )
        lines.append(
            f"- {key}"
            + (f"  {format_size(int(size))}" if isinstance(size, int) else "")
            + (f"  uploaded {uploaded}" if uploaded else "")
            + (f"  etag {etag}" if etag else "")
        )
        lines.append(f"     {public_url(settings, key)}")
    if not items:
        lines.append("(no objects)")
    return "\n".join(lines)


async def _list(prefix: str, max_results: int) -> str:
    prefix = (prefix or "").strip().lstrip("/")
    if (
        not isinstance(max_results, int)
        or isinstance(max_results, bool)
        or max_results < 1
    ):
        raise UserInputError("max_results must be a positive integer.")
    max_results = min(max_results, MAX_LIST_RESULTS)
    settings = load_settings()
    return await asyncio.to_thread(_list_sync, prefix, max_results, settings)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@server.tool()
@handle_http_errors("upload_cdn_asset", service_type="cdn")
@_write_tool
async def upload_cdn_asset(
    path: str,
    content_type: str,
    base64_content: Optional[str] = None,
    local_file: Optional[str] = None,
) -> str:
    """
    Uploads one versioned object to the OTB CDN (cdn.otbgroup.co.uk) and verifies it.

    PUTs the bytes to the Cloudflare R2 bucket through the Cloudflare REST
    API (api.cloudflare.com), then HEADs the public URL and compares the
    ETag with the local MD5 (or the served bytes' sha256 when the ETag is
    not an MD5). Prints the size, sha256, MD5, ETag and the public URL.
    Refuses a file name that is not versioned ('name-vN.ext') and refuses
    to overwrite an existing key: a changed asset is the next version.
    Refused on a read-only server. Caller must be on CDN_ADMIN_EMAILS.

    Args:
        path (str): Object key, e.g. 'bir/bir-logo-black-v1.png'. Folders are
            plain path segments; the file name must end '-vN.ext'.
        content_type (str): The MIME type to serve, e.g. 'image/png'.
        base64_content (Optional[str]): The bytes as standard base64. Exactly
            one of base64_content and local_file.
        local_file (Optional[str]): A path on the server to read instead.

    Returns:
        str: The upload report with the public URL and the verification
            result, or a refusal naming the rule that stopped it.
    """
    await _require_allowed_caller()
    if is_read_only_mode():
        raise UserInputError(READ_ONLY_MESSAGE)
    return await _translated(_upload(path, content_type, base64_content, local_file))


@server.tool()
@handle_http_errors("list_cdn_assets", is_read_only=True, service_type="cdn")
async def list_cdn_assets(prefix: str = "", max_results: int = 200) -> str:
    """
    Lists objects in the OTB CDN bucket under a prefix. Read-only.

    One line per object: key, size, upload time, ETag, then its public URL.
    Says when more objects exist than max_results. Caller must be on
    CDN_ADMIN_EMAILS.

    Args:
        prefix (str): Key prefix to list, e.g. 'bir/'. Empty lists everything.
        max_results (int): Cap on objects shown. Defaults to 200, maximum 1000.

    Returns:
        str: The listing.
    """
    await _require_allowed_caller()
    return await _translated(_list(prefix, max_results))

"""
Cloudflare R2 object access through the Cloudflare REST API.

The bucket behind ``cdn.otbgroup.co.uk`` is written through
``api.cloudflare.com`` (``/accounts/{account}/r2/buckets/{bucket}/objects``),
the same endpoints the Cloudflare dashboard and ``wrangler r2 object`` use,
not through the S3-compatible endpoint. The REST API is reachable from every
environment this server runs in; the S3 endpoint is not.

Nothing here decides policy. ``validate_key`` enforces the one naming rule
(versioned file names, ``name-vN.ext``), ``R2Client`` makes the calls, and
``verify_public`` proves the upload by reading the public URL back. The tool
layer (``gcdn/cdn_tools.py``) does the gating and the formatting.

Secrets: the API token is read from ``CF_R2_TOKEN`` and sent only in the
``Authorization`` header to ``api.cloudflare.com``. No message, log line or
exception produced here ever carries it.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

CF_API_BASE = "https://api.cloudflare.com/client/v4"

ENV_ACCOUNT_ID = "CF_ACCOUNT_ID"
ENV_BUCKET = "CF_R2_BUCKET"
ENV_TOKEN = "CF_R2_TOKEN"
ENV_PUBLIC_BASE = "CDN_PUBLIC_BASE_URL"
ENV_MAX_BYTES = "CDN_MAX_UPLOAD_BYTES"

DEFAULT_PUBLIC_BASE = "https://cdn.otbgroup.co.uk"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024

# Object keys are versioned, never overwritten: ``bir/bir-logo-black-v1.png``.
# The last path segment must end ``-v<digits>.<ext>``.
VERSIONED_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*-v\d+\.[A-Za-z0-9]+$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_LIST_PAGE_SIZE = 1000


class CdnConfigError(Exception):
    """A CDN environment variable is missing or malformed."""


class CdnKeyError(ValueError):
    """The object key breaks the naming rule."""


class CdnApiError(Exception):
    """The Cloudflare API refused or failed a call. Never carries the token."""


@dataclass
class CdnSettings:
    account_id: str
    bucket: str
    token: str
    public_base: str
    max_bytes: int


def load_settings(env: Optional[Dict[str, str]] = None) -> CdnSettings:
    """Read the CDN settings from the environment, naming what is missing."""
    source = os.environ if env is None else env
    missing = [
        name
        for name in (ENV_ACCOUNT_ID, ENV_BUCKET, ENV_TOKEN)
        if not (source.get(name) or "").strip()
    ]
    if missing:
        raise CdnConfigError(
            "CDN uploads are not configured on this server: set "
            + ", ".join(missing)
            + " (Render environment). The token needs the Cloudflare permission "
            "'Workers R2 Storage: Edit' scoped to the one bucket."
        )
    public_base = (
        (source.get(ENV_PUBLIC_BASE) or DEFAULT_PUBLIC_BASE).strip().rstrip("/")
    )
    if not public_base.startswith("https://"):
        raise CdnConfigError(f"{ENV_PUBLIC_BASE} must be an https:// URL.")
    raw_max = (source.get(ENV_MAX_BYTES) or "").strip()
    max_bytes = DEFAULT_MAX_BYTES
    if raw_max:
        if not raw_max.isdigit() or int(raw_max) <= 0:
            raise CdnConfigError(
                f"{ENV_MAX_BYTES} must be a positive integer of bytes."
            )
        max_bytes = int(raw_max)
    return CdnSettings(
        account_id=source[ENV_ACCOUNT_ID].strip(),
        bucket=source[ENV_BUCKET].strip(),
        token=source[ENV_TOKEN].strip(),
        public_base=public_base,
        max_bytes=max_bytes,
    )


def validate_key(path: str) -> str:
    """Normalise and check an object key.

    A leading ``/`` is dropped. Every segment is ASCII letters, digits,
    ``.``, ``_`` or ``-`` with no leading dot; ``..`` and empty segments are
    refused. The file name must be versioned: ``name-vN.ext``. Returns the
    cleaned key.
    """
    cleaned = (path or "").strip().lstrip("/")
    if not cleaned:
        raise CdnKeyError("path is required, e.g. 'bir/bir-logo-black-v1.png'.")
    if any(ord(ch) > 126 or ch.isspace() for ch in cleaned):
        raise CdnKeyError(
            "path may contain only ASCII letters, digits, '.', '_', '-' and '/'."
        )
    segments = cleaned.split("/")
    for segment in segments:
        if not _SEGMENT_RE.match(segment):
            raise CdnKeyError(
                f"path segment {segment!r} is not allowed: use ASCII letters, digits, "
                "'.', '_' or '-', with no empty or dot-leading segments."
            )
    name = segments[-1]
    if not VERSIONED_NAME_RE.match(name):
        raise CdnKeyError(
            f"file name {name!r} is not versioned. CDN objects are never overwritten, "
            "so every name carries a version: 'name-vN.ext', e.g. "
            "'bir-logo-black-v1.png'. Bump N for a new version."
        )
    return cleaned


def public_url(settings: CdnSettings, key: str) -> str:
    return f"{settings.public_base}/{quote(key, safe='/')}"


def objects_url(settings: CdnSettings) -> str:
    return (
        f"{CF_API_BASE}/accounts/{quote(settings.account_id, safe='')}"
        f"/r2/buckets/{quote(settings.bucket, safe='')}/objects"
    )


def object_url(settings: CdnSettings, key: str) -> str:
    return f"{objects_url(settings)}/{quote(key, safe='/')}"


def normalise_etag(value: Optional[str]) -> str:
    """``W/"abc"`` or ``"abc"`` -> ``abc`` (lower-case); '' when absent."""
    text = (value or "").strip()
    if text.startswith("W/") or text.startswith("w/"):
        text = text[2:]
    return text.strip('"').strip().lower()


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size:,} bytes ({size / 1024:.1f} KB)"
    return f"{size:,} bytes ({size / (1024 * 1024):.2f} MB)"


def _api_errors(response: httpx.Response) -> str:
    """The API's own error messages, or the status, never the request."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        errors = payload.get("errors") or []
        messages = [
            f"{e.get('code', '?')}: {e.get('message', '')}".strip()
            for e in errors
            if isinstance(e, dict)
        ]
        if messages:
            return "; ".join(messages)
    text = (response.text or "").strip()
    return text[:200] if text else "(no error body)"


class R2Client:
    """Thin synchronous client; call it inside ``asyncio.to_thread``."""

    def __init__(
        self,
        settings: CdnSettings,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = 60.0,
    ):
        self.settings = settings
        self._client = httpx.Client(
            transport=transport, timeout=timeout, follow_redirects=False
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "R2Client":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.token}"}

    def _check(self, response: httpx.Response, what: str) -> Dict[str, Any]:
        if response.status_code >= 400:
            raise CdnApiError(
                f"Cloudflare API refused {what} (HTTP {response.status_code}): "
                f"{_api_errors(response)}"
            )
        try:
            payload = response.json()
        except ValueError:
            raise CdnApiError(f"Cloudflare API returned no JSON for {what}.") from None
        if isinstance(payload, dict) and payload.get("success") is False:
            raise CdnApiError(
                f"Cloudflare API reported failure for {what}: {_api_errors(response)}"
            )
        return payload if isinstance(payload, dict) else {"result": payload}

    def list_objects(
        self, prefix: str = "", limit: int = _LIST_PAGE_SIZE
    ) -> List[Dict[str, Any]]:
        """Objects under ``prefix``, following the cursor until ``limit``."""
        out: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {
                "per_page": min(_LIST_PAGE_SIZE, max(1, limit - len(out)))
            }
            if prefix:
                params["prefix"] = prefix
            if cursor:
                params["cursor"] = cursor
            response = self._client.get(
                objects_url(self.settings), params=params, headers=self._headers()
            )
            payload = self._check(response, f"listing objects under {prefix!r}")
            result = payload.get("result") or []
            if isinstance(result, dict):
                result = result.get("objects") or []
            for item in result:
                if isinstance(item, dict):
                    out.append(item)
            info = payload.get("result_info") or {}
            cursor = info.get("cursor") or None
            truncated = (
                bool(info.get("is_truncated"))
                if "is_truncated" in info
                else bool(cursor)
            )
            if not truncated or not cursor or len(out) >= limit:
                return out[:limit]

    def find_object(self, key: str) -> Optional[Dict[str, Any]]:
        """The object with exactly this key, or None."""
        for item in self.list_objects(prefix=key, limit=_LIST_PAGE_SIZE):
            if str(item.get("key") or "") == key:
                return item
        return None

    def put_object(self, key: str, data: bytes, content_type: str) -> Dict[str, Any]:
        """Upload ``data`` under ``key``. Returns the API's ``result`` (may be {})."""
        headers = dict(self._headers())
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(data))
        response = self._client.put(
            object_url(self.settings, key), content=data, headers=headers
        )
        payload = self._check(response, f"uploading {key!r}")
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    def fetch_public(self, url: str, method: str = "HEAD") -> httpx.Response:
        """HEAD or GET the public CDN URL. No credentials are sent."""
        return self._client.request(method, url, headers={"Cache-Control": "no-cache"})


def verify_public(
    client: R2Client, settings: CdnSettings, key: str, data: bytes
) -> Dict[str, Any]:
    """Prove the object is served: HEAD the public URL and compare.

    R2 sets the ETag of a single-request upload to the MD5 of the bytes, so
    the first check is ETag against the local MD5. When the ETag is absent
    or not an MD5 (a multipart-style ETag), the body is fetched once and its
    sha256 compared instead. Both hashes are returned either way.
    """
    url = public_url(settings, key)
    md5 = hashlib.md5(data).hexdigest()
    sha256 = hashlib.sha256(data).hexdigest()
    report: Dict[str, Any] = {
        "url": url,
        "status": None,
        "etag": "",
        "md5": md5,
        "sha256": sha256,
        "etag_matches_md5": False,
        "body_sha256_matches": None,
        "content_length": None,
        "verified": False,
        "note": "",
    }
    try:
        head = client.fetch_public(url, "HEAD")
    except httpx.HTTPError as exc:
        report["note"] = f"HEAD failed: {type(exc).__name__}"
        return report
    report["status"] = head.status_code
    report["etag"] = normalise_etag(head.headers.get("etag"))
    length = head.headers.get("content-length")
    report["content_length"] = int(length) if length and length.isdigit() else None
    if head.status_code != 200:
        report["note"] = f"public URL answered HTTP {head.status_code}"
        return report
    if report["etag"] == md5:
        report["etag_matches_md5"] = True
        report["verified"] = True
        return report
    try:
        body = client.fetch_public(url, "GET")
    except httpx.HTTPError as exc:
        report["note"] = f"ETag is not the MD5 and GET failed: {type(exc).__name__}"
        return report
    matches = (
        body.status_code == 200 and hashlib.sha256(body.content).hexdigest() == sha256
    )
    report["body_sha256_matches"] = matches
    report["verified"] = matches
    if not matches:
        report["note"] = "served bytes do not hash to the uploaded sha256"
    else:
        report["note"] = "ETag is not an MD5; verified by sha256 of the served bytes"
    return report


__all__ = [
    "CF_API_BASE",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_PUBLIC_BASE",
    "ENV_ACCOUNT_ID",
    "ENV_BUCKET",
    "ENV_MAX_BYTES",
    "ENV_PUBLIC_BASE",
    "ENV_TOKEN",
    "VERSIONED_NAME_RE",
    "CdnApiError",
    "CdnConfigError",
    "CdnKeyError",
    "CdnSettings",
    "R2Client",
    "format_size",
    "load_settings",
    "normalise_etag",
    "object_url",
    "objects_url",
    "public_url",
    "validate_key",
    "verify_public",
]

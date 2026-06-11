"""
Stateless attachment delivery helpers.

Gmail attachments are no longer staged on local disk behind
``/attachments/{file_id}`` URLs — those URLs are instance-local and die on
restart/redeploy. Instead, small attachments are returned inline as base64
and larger ones are uploaded to a transfer folder in the caller's own Drive
and read back via the existing Drive tools. Bytes are already in memory
(they arrive base64-encoded from the Gmail API), so there is no extra
memory cost relative to the old local-save path.
"""

import asyncio
import io
import logging
import os
from datetime import datetime
from typing import Dict, NamedTuple, Optional

from googleapiclient.http import MediaIoBaseUpload

logger = logging.getLogger(__name__)

# 512 KiB default. Inline content lands in the model context: 5 MB of
# base64 is ~1.7M tokens, which is unusable — keep the default small.
DEFAULT_INLINE_MAX_BYTES = 524288

DEFAULT_TRANSFER_FOLDER = "Claude Inbox/_attachment-transfer"

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

# Below this size a simple multipart upload is cheaper than a resumable
# session (matches Google's recommended resumable threshold).
RESUMABLE_THRESHOLD_BYTES = 5 * 1024 * 1024

# Resolved folder-path -> Drive folder ID. Optimisation only: a cold cache
# (process restart, multi-instance) just re-resolves via find-or-create.
_folder_id_cache: Dict[str, str] = {}


class TransferredAttachment(NamedTuple):
    """Result of uploading an attachment to the Drive transfer folder."""

    file_id: str
    name: str
    web_view_link: str
    folder_path: str
    size: int


def get_inline_max_bytes() -> int:
    """Max attachment size (bytes) returned inline as base64 in HTTP mode."""
    raw = os.getenv("ATTACHMENT_INLINE_MAX_BYTES", "")
    try:
        value = int(raw)
        if value > 0:
            return value
    except ValueError:
        pass
    return DEFAULT_INLINE_MAX_BYTES


def get_transfer_folder_path() -> str:
    """Slash-separated Drive folder path for over-cap attachment transfers."""
    raw = os.getenv("ATTACHMENT_TRANSFER_FOLDER", "").strip().strip("/")
    return raw or DEFAULT_TRANSFER_FOLDER


def _escape_drive_query(value: str) -> str:
    """Escape backslashes and single quotes for Drive ``q`` string literals."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


async def resolve_transfer_folder(drive_service, path: str) -> str:
    """Resolve a slash-separated folder path from the My Drive root,
    creating any missing segments, and return the leaf folder ID.
    """
    segments = [s.strip() for s in path.split("/") if s.strip()]
    if not segments:
        raise ValueError(f"Transfer folder path is empty: {path!r}")

    cache_key = "/".join(segments)
    cached = _folder_id_cache.get(cache_key)
    if cached:
        return cached

    parent_id = "root"
    for segment in segments:
        query = (
            f"name = '{_escape_drive_query(segment)}' "
            f"and mimeType = '{FOLDER_MIME_TYPE}' "
            f"and '{parent_id}' in parents and trashed = false"
        )
        listing = await asyncio.to_thread(
            drive_service.files()
            .list(q=query, fields="files(id)", pageSize=1)
            .execute
        )
        files = listing.get("files", [])
        if files:
            parent_id = files[0]["id"]
        else:
            created = await asyncio.to_thread(
                drive_service.files()
                .create(
                    body={
                        "name": segment,
                        "mimeType": FOLDER_MIME_TYPE,
                        "parents": [parent_id],
                    },
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute
            )
            parent_id = created["id"]
            logger.info(
                f"[attachment_transfer] Created Drive folder '{segment}' "
                f"(id={parent_id}) for transfer path '{cache_key}'"
            )

    _folder_id_cache[cache_key] = parent_id
    return parent_id


async def upload_attachment_to_drive(
    drive_service,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    folder_path: Optional[str] = None,
) -> TransferredAttachment:
    """Upload attachment bytes to the Drive transfer folder.

    The Drive file name is prefixed with a ``YYYYMMDD-HHMMSS_`` timestamp so
    repeated downloads of the same attachment don't collide.
    """
    resolved_path = folder_path or get_transfer_folder_path()
    folder_id = await resolve_transfer_folder(drive_service, resolved_path)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    drive_name = f"{timestamp}_{filename}"

    media = MediaIoBaseUpload(
        io.BytesIO(file_bytes),
        mimetype=mime_type or "application/octet-stream",
        resumable=len(file_bytes) > RESUMABLE_THRESHOLD_BYTES,
    )

    created = await asyncio.to_thread(
        drive_service.files()
        .create(
            body={"name": drive_name, "parents": [folder_id]},
            media_body=media,
            fields="id, name, webViewLink, size",
            supportsAllDrives=True,
        )
        .execute
    )

    try:
        size = int(created.get("size"))
    except (TypeError, ValueError):
        size = len(file_bytes)

    return TransferredAttachment(
        file_id=created["id"],
        name=created.get("name", drive_name),
        web_view_link=created.get("webViewLink", ""),
        folder_path=resolved_path,
        size=size,
    )

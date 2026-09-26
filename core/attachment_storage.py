"""
Temporary attachment storage for Gmail attachments.

Stores attachments to local disk and returns file paths for direct access.
Files are automatically cleaned up after expiration (default 1 hour).
"""

import base64
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import NamedTuple, Optional, Dict
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Default expiration: 1 hour
DEFAULT_EXPIRATION_SECONDS = 3600

# Storage directory - configurable via WORKSPACE_ATTACHMENT_DIR env var
# Uses absolute path to avoid creating tmp/ in arbitrary working directories (see #327)
_default_dir = str(Path.home() / ".workspace-mcp" / "attachments")
STORAGE_DIR = (
    Path(os.getenv("WORKSPACE_ATTACHMENT_DIR", _default_dir)).expanduser().resolve()
)


# Total-size cap for the storage directory. Once the age sweep has run,
# anything over this is evicted oldest-first. Configurable via
# WORKSPACE_ATTACHMENT_MAX_BYTES; default 512 MiB.
DEFAULT_MAX_BYTES = 512 * 1024 * 1024


def _max_storage_bytes() -> int:
    raw = os.getenv("WORKSPACE_ATTACHMENT_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "WORKSPACE_ATTACHMENT_MAX_BYTES=%r is not an integer; using default %d",
            raw,
            DEFAULT_MAX_BYTES,
        )
        return DEFAULT_MAX_BYTES
    return value if value > 0 else DEFAULT_MAX_BYTES


def _ensure_storage_dir() -> None:
    """Create the storage directory on first use, not at import time."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)


class SavedAttachment(NamedTuple):
    """Result of saving an attachment: provides both the UUID and the absolute file path."""

    file_id: str
    path: str


class AttachmentStorage:
    """Manages temporary storage of email attachments."""

    def __init__(self, expiration_seconds: int = DEFAULT_EXPIRATION_SECONDS):
        self.expiration_seconds = expiration_seconds
        self._metadata: Dict[str, Dict] = {}
        # Tool code runs save/register off the event loop (asyncio.to_thread)
        # while the /attachments route reads metadata on the loop thread —
        # guard all _metadata access. RLock because cleanup paths re-enter
        # (_cleanup_file is called from locked readers).
        self._lock = threading.RLock()
        # Files written before a restart are not in _metadata, so the
        # metadata-driven cleanup would never remove them. Sweep the
        # directory once at construction time so a restarted instance
        # does not carry stale attachments forever.
        try:
            self.sweep_directory()
        except Exception as e:
            logger.warning("Attachment directory sweep at start-up failed: %s", e)

    def save_attachment(
        self,
        base64_data: str,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> SavedAttachment:
        """
        Save an attachment to local disk.

        Args:
            base64_data: Base64-encoded attachment data
            filename: Original filename (optional)
            mime_type: MIME type (optional)

        Returns:
            SavedAttachment with file_id (UUID) and path (absolute file path)
        """
        _ensure_storage_dir()

        # Generate unique file ID for metadata tracking
        file_id = str(uuid.uuid4())

        # Decode base64 data
        try:
            file_bytes = base64.urlsafe_b64decode(base64_data)
        except Exception as e:
            logger.error(f"Failed to decode base64 attachment data: {e}")
            raise ValueError(f"Invalid base64 data: {e}")

        # Determine file extension from filename or mime type
        extension = ""
        if filename:
            extension = Path(filename).suffix
        elif mime_type:
            # Basic mime type to extension mapping
            mime_to_ext = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "application/pdf": ".pdf",
                "application/zip": ".zip",
                "text/plain": ".txt",
                "text/html": ".html",
            }
            extension = mime_to_ext.get(mime_type, "")

        # Use original filename if available, with UUID suffix for uniqueness
        if filename:
            stem = Path(filename).stem
            ext = Path(filename).suffix
            save_name = f"{stem}_{file_id[:8]}{ext}"
        else:
            save_name = f"{file_id}{extension}"

        # Save file with restrictive permissions (sensitive email/drive content)
        file_path = STORAGE_DIR / save_name
        try:
            fd = os.open(
                file_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                total_written = 0
                data_len = len(file_bytes)
                while total_written < data_len:
                    written = os.write(fd, file_bytes[total_written:])
                    if written == 0:
                        raise OSError(
                            "os.write returned 0 bytes; could not write attachment data"
                        )
                    total_written += written
            finally:
                os.close(fd)
            logger.info(
                f"Saved attachment file_id={file_id} filename={filename or save_name} "
                f"({len(file_bytes)} bytes) to {file_path} "
                f"(instance={os.getenv('RENDER_INSTANCE_ID', 'local')})"
            )
        except Exception as e:
            logger.error(
                f"Failed to save attachment file_id={file_id} "
                f"filename={filename or save_name} to {file_path}: {e}"
            )
            raise

        # Store metadata
        expires_at = datetime.now() + timedelta(seconds=self.expiration_seconds)
        with self._lock:
            self._metadata[file_id] = {
                "file_path": str(file_path),
                "filename": filename or f"attachment{extension}",
                "mime_type": mime_type or "application/octet-stream",
                "size": len(file_bytes),
                "created_at": datetime.now(),
                "expires_at": expires_at,
            }

        return SavedAttachment(file_id=file_id, path=str(file_path))

    def reserve_path(self, filename: Optional[str] = None) -> tuple[str, str]:
        """Reserve a destination path inside the storage dir, without writing
        anything to it yet.

        Used by streaming downloads that want to hand a real on-disk path to
        ``MediaIoBaseDownload`` (or similar) so bytes never have to be held
        in memory as a single buffer. The caller is responsible for opening
        the path with the desired permissions, writing to it, and then
        calling ``register_existing_file`` to add metadata.

        Returns:
            ``(file_id, file_path)`` where ``file_id`` is a UUID and
            ``file_path`` is an absolute path inside ``STORAGE_DIR``.
        """
        _ensure_storage_dir()
        file_id = str(uuid.uuid4())
        if filename:
            stem = Path(filename).stem
            ext = Path(filename).suffix
            save_name = f"{stem}_{file_id[:8]}{ext}"
        else:
            save_name = file_id
        return file_id, str(STORAGE_DIR / save_name)

    def register_existing_file(
        self,
        file_id: str,
        file_path: str,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
        size: Optional[int] = None,
    ) -> SavedAttachment:
        """Register a file already written into ``STORAGE_DIR`` with the
        attachment metadata table so it can be served via the
        ``/attachments/{file_id}`` route and aged out by ``cleanup_expired``.

        Pairs with ``reserve_path``. Use this when bytes were streamed
        directly to disk (large-file downloads) to avoid the
        ``save_attachment`` round-trip through base64.
        """
        path = Path(file_path)
        if not path.is_absolute():
            path = (STORAGE_DIR / path).resolve()
        if size is None:
            size = path.stat().st_size if path.exists() else 0
        expires_at = datetime.now() + timedelta(seconds=self.expiration_seconds)
        with self._lock:
            self._metadata[file_id] = {
                "file_path": str(path),
                "filename": filename or path.name,
                "mime_type": mime_type or "application/octet-stream",
                "size": size,
                "created_at": datetime.now(),
                "expires_at": expires_at,
            }
        return SavedAttachment(file_id=file_id, path=str(path))

    def get_attachment_path(self, file_id: str) -> Optional[Path]:
        """
        Get the file path for an attachment ID.

        Args:
            file_id: Unique file ID

        Returns:
            Path object if file exists and not expired, None otherwise
        """
        with self._lock:
            if file_id not in self._metadata:
                logger.warning(f"Attachment {file_id} not found in metadata")
                return None

            metadata = self._metadata[file_id]
            file_path = Path(metadata["file_path"])

            # Check if expired
            if datetime.now() > metadata["expires_at"]:
                logger.info(f"Attachment {file_id} has expired, cleaning up")
                self._cleanup_file(file_id)
                return None

            # Check if file exists
            if not file_path.exists():
                logger.warning(f"Attachment file {file_path} does not exist")
                del self._metadata[file_id]
                return None

            return file_path

    def get_attachment_metadata(self, file_id: str) -> Optional[Dict]:
        """
        Get metadata for an attachment.

        Args:
            file_id: Unique file ID

        Returns:
            Metadata dict if exists and not expired, None otherwise
        """
        with self._lock:
            if file_id not in self._metadata:
                return None

            metadata = self._metadata[file_id].copy()

            # Check if expired
            if datetime.now() > metadata["expires_at"]:
                self._cleanup_file(file_id)
                return None

            return metadata

    def _cleanup_file(self, file_id: str) -> None:
        """Remove file and metadata."""
        with self._lock:
            if file_id in self._metadata:
                file_path = Path(self._metadata[file_id]["file_path"])
                try:
                    if file_path.exists():
                        file_path.unlink()
                        logger.debug(f"Deleted expired attachment file: {file_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete attachment file {file_path}: {e}")
                del self._metadata[file_id]

    def cleanup_expired(self) -> int:
        """
        Clean up expired attachments.

        Returns:
            Number of files cleaned up
        """
        now = datetime.now()
        with self._lock:
            expired_ids = [
                file_id
                for file_id, metadata in self._metadata.items()
                if now > metadata["expires_at"]
            ]

            for file_id in expired_ids:
                self._cleanup_file(file_id)

        # Also remove anything on disk that the metadata table does not
        # know about (files left behind by a previous process).
        try:
            self.sweep_directory()
        except Exception as e:
            logger.warning("Attachment directory sweep failed: %s", e)

        return len(expired_ids)

    def _live_paths(self) -> set:
        """Paths of files still tracked in _metadata and not yet expired."""
        now = datetime.now()
        with self._lock:
            return {
                str(Path(meta["file_path"]))
                for meta in self._metadata.values()
                if now <= meta["expires_at"]
            }

    def _forget_path(self, path: Path) -> None:
        """Drop any metadata entry that points at ``path``."""
        target = str(path)
        with self._lock:
            stale = [
                file_id
                for file_id, meta in self._metadata.items()
                if str(Path(meta["file_path"])) == target
            ]
            for file_id in stale:
                del self._metadata[file_id]

    def sweep_directory(self) -> int:
        """Remove stale files directly from STORAGE_DIR.

        Two passes, both non-recursive (this module never writes
        subdirectories). Symlinks are never followed and directories are
        never removed.

        1. Age: unlink every regular file whose mtime is older than
           ``expiration_seconds``, unless it is still tracked in
           ``_metadata`` and not expired.
        2. Size: if the directory is still over the cap
           (``WORKSPACE_ATTACHMENT_MAX_BYTES``), unlink oldest files first
           until it fits, and log a warning.

        Returns the number of files removed.
        """
        try:
            if not STORAGE_DIR.is_dir():
                return 0
            entries = list(os.scandir(STORAGE_DIR))
        except OSError as e:
            logger.warning("Cannot scan attachment directory %s: %s", STORAGE_DIR, e)
            return 0

        cutoff = time.time() - self.expiration_seconds
        live = self._live_paths()
        removed = 0
        survivors: list[tuple[float, int, Path]] = []

        for entry in entries:
            try:
                # follow_symlinks=False: a symlink is never a regular file here,
                # so it is skipped rather than followed.
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            path = Path(entry.path)
            if st.st_mtime < cutoff and str(path) not in live:
                if self._unlink_quiet(path):
                    removed += 1
                    self._forget_path(path)
                continue
            survivors.append((st.st_mtime, st.st_size, path))

        if removed:
            logger.info(
                "Attachment sweep removed %d stale file(s) older than %ds from %s",
                removed,
                self.expiration_seconds,
                STORAGE_DIR,
            )

        cap = _max_storage_bytes()
        total = sum(size for _, size, _ in survivors)
        if total > cap:
            evicted = 0
            freed = 0
            for _, size, path in sorted(survivors, key=lambda item: item[0]):
                if total <= cap:
                    break
                if self._unlink_quiet(path):
                    self._forget_path(path)
                    total -= size
                    freed += size
                    evicted += 1
            removed += evicted
            logger.warning(
                "Attachment directory %s exceeded %d bytes; evicted %d oldest "
                "file(s) (%d bytes), now %d bytes",
                STORAGE_DIR,
                cap,
                evicted,
                freed,
                total,
            )

        return removed

    @staticmethod
    def _unlink_quiet(path: Path) -> bool:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as e:
            logger.warning("Failed to delete attachment file %s: %s", path, e)
            return False


# Global instance
_attachment_storage: Optional[AttachmentStorage] = None


def get_attachment_storage() -> AttachmentStorage:
    """Get the global attachment storage instance."""
    global _attachment_storage
    if _attachment_storage is None:
        _attachment_storage = AttachmentStorage()
    return _attachment_storage


def get_attachment_url(file_id: str) -> str:
    """
    Generate a URL for accessing an attachment.

    Args:
        file_id: Unique file ID

    Returns:
        Full URL to access the attachment
    """
    from core.config import WORKSPACE_MCP_PORT, WORKSPACE_MCP_BASE_URI

    # Use external URL if set (for reverse proxy scenarios)
    external_url = os.getenv("WORKSPACE_EXTERNAL_URL")
    if external_url:
        base_url = external_url.rstrip("/")
    else:
        base_url = f"{WORKSPACE_MCP_BASE_URI}:{WORKSPACE_MCP_PORT}"

    return f"{base_url}/attachments/{file_id}"

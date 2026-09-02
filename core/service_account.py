"""Shared loader for Google service-account keys supplied via environment.

Two server-side identities can use a service account: the group-policy
membership reader (``core/access_policy.py``) and the audit-sheet writer
(``core/audit.py``). Both accept the key either as a file path (Render
"Secret File", preferred) or as base64 of the JSON key. This module keeps the
parsing and validation in one place so both behave identically.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


class ServiceAccountConfigError(ValueError):
    """The configured service-account key is missing, unreadable or not a key."""


def load_service_account_info(
    file_env: str,
    b64_env: str,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """Return the parsed key, or ``None`` when neither variable is set.

    ``file_env`` wins when both are present. Raises
    ``ServiceAccountConfigError`` for a missing file, undecodable base64,
    invalid JSON, or JSON that is not a ``service_account`` key.
    """
    import os

    env = os.environ if environ is None else environ
    path = (env.get(file_env) or "").strip()
    raw: Optional[str] = None
    if path:
        p = Path(path).expanduser()
        if not p.exists():
            raise ServiceAccountConfigError(f"{file_env} points to a missing file: {p}")
        if not p.is_file():
            raise ServiceAccountConfigError(f"{file_env} is not a regular file: {p}")
        try:
            raw = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ServiceAccountConfigError(
                f"{file_env} cannot be read as UTF-8 text: {exc.__class__.__name__}"
            ) from exc
    elif (env.get(b64_env) or "").strip():
        try:
            raw = base64.b64decode(env[b64_env].strip(), validate=True).decode("utf-8")
        except Exception as exc:
            raise ServiceAccountConfigError(f"{b64_env} is not valid base64") from exc
    if raw is None:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ServiceAccountConfigError(
            "service account JSON is not valid JSON"
        ) from exc
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ServiceAccountConfigError(
            "service account JSON must be a Google service_account key"
        )
    return info


def service_account_email(info: Mapping[str, Any]) -> str:
    return str(info.get("client_email") or "<unknown service account>")

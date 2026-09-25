"""
Service-account auth for centrally managed Gmail signatures.

This feature does NOT use the MCP's own OAuth user token. A Google service
account with domain-wide delegation impersonates each user for their Gmail
settings, impersonates the configured Directory admin for Admin SDK reads,
and acts as itself (no subject) for the ledger Sheet, which is shared with
the service account directly.

Gmail API facts this module relies on (from the Gmail v1 discovery document
bundled with googleapiclient, ``resources.users.settings.sendAs``):

* ``users.settings.sendAs.patch`` and ``.update`` accept two scopes: the basic
  settings scope and the sharing settings scope. We request the basic scope
  only. The sharing scope (gmail.settings.sharing) is deliberately not used
  because it also covers forwarding and delegation, which this feature must
  never be able to touch.
* Send-as addresses other than the primary address can only be updated by
  service account clients that have been delegated domain-wide authority.
  That is why a service account is the auth model here, not a user token.
* Gmail sanitises the HTML signature before saving it. What comes back from
  ``sendAs.get`` after a write is therefore not byte-identical to what was
  sent, which is why drift is judged against the read-back hash recorded in
  the ledger (see ``gsignatures/ledger.py``), never against raw template
  output.

Domain-wide delegation is configured in the Admin console against the
service account's client ID with exactly ``DELEGATED_SCOPES``. Keep that list
minimal: every scope on it is something the key can do as any user.

Key handling rules, enforced here:

* the key is read from ``SIGNATURE_SERVICE_ACCOUNT_FILE`` (a path) first, then
  ``SIGNATURE_SERVICE_ACCOUNT_JSON`` (the JSON inline);
* no error message ever includes the key, the JSON, or the raw path; use
  ``redact_for_log`` before logging any path or address fragment;
* credentials are built fresh per call and never cached at module level, so
  a rotated key takes effect on the next call without a restart.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from google.oauth2 import service_account
from googleapiclient import discovery

from auth.scopes import (
    ADMIN_DIRECTORY_GROUP_MEMBER_READONLY_SCOPE,
    ADMIN_DIRECTORY_USER_READONLY_SCOPE,
    GMAIL_SETTINGS_BASIC_SCOPE,
)

logger = logging.getLogger(__name__)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# The exact list the owner pastes into Admin console > Security > API controls
# > Domain-wide delegation. Order matters only for the paste; keep it stable.
DELEGATED_SCOPES: List[str] = [
    GMAIL_SETTINGS_BASIC_SCOPE,
    ADMIN_DIRECTORY_USER_READONLY_SCOPE,
    ADMIN_DIRECTORY_GROUP_MEMBER_READONLY_SCOPE,
]

# Scope subsets used per client. Gmail gets settings only; the Directory
# gets the two read-only scopes; the ledger Sheet gets Sheets only.
GMAIL_SCOPES: List[str] = [GMAIL_SETTINGS_BASIC_SCOPE]
DIRECTORY_SCOPES: List[str] = [
    ADMIN_DIRECTORY_USER_READONLY_SCOPE,
    ADMIN_DIRECTORY_GROUP_MEMBER_READONLY_SCOPE,
]
LEDGER_SCOPES: List[str] = [SHEETS_SCOPE]

ENV_SA_FILE = "SIGNATURE_SERVICE_ACCOUNT_FILE"
ENV_SA_JSON = "SIGNATURE_SERVICE_ACCOUNT_JSON"
ENV_DIRECTORY_ADMIN = "SIGNATURE_DIRECTORY_ADMIN"
ENV_ADMIN_EMAILS = "SIGNATURE_ADMIN_EMAILS"
ENV_LEDGER_SHEET_ID = "SIGNATURE_LEDGER_SHEET_ID"

DEFAULT_DIRECTORY_ADMIN = "oliver@otbgroup.co.uk"
DEFAULT_ADMIN_EMAILS = "oliver@otbgroup.co.uk"

# Fields a Google service-account key file always carries. Checked so a
# truncated or hand-edited file fails here, with a clear message, rather
# than deep inside google-auth with a stack trace that may echo the input.
_REQUIRED_KEY_FIELDS = ("client_email", "private_key", "token_uri")

# Values at or below this length are masked completely by ``redact_for_log``;
# longer values keep a short head and tail so two different values can still
# be told apart in a log line. Two characters each side plus the three-char
# mask is always shorter than any value over eight characters.
_REDACT_FULL_MASK_MAX_LEN = 8
_REDACT_KEEP = 2
_MASK = "***"


class SignatureAuthError(Exception):
    """Service-account auth is missing, malformed, or the caller is not allowed."""


# --- Key loading -------------------------------------------------------------


def _setup_guidance() -> str:
    return (
        "Set either "
        f"{ENV_SA_FILE} (path to the service-account key JSON, preferred on "
        f"Render as a secret file) or {ENV_SA_JSON} (the key JSON inline). "
        "The service account needs domain-wide delegation for exactly the "
        "scopes in gsignatures.sa_auth.DELEGATED_SCOPES."
    )


def load_service_account_info() -> Dict[str, Any]:
    """Return the parsed service-account key from the environment.

    ``SIGNATURE_SERVICE_ACCOUNT_FILE`` wins when both are set. Every failure
    raises ``SignatureAuthError`` with a message that names the source and
    the problem but never repeats the path, the JSON or any key material.
    """
    file_value = os.environ.get(ENV_SA_FILE, "").strip()
    raw: Optional[str] = None
    source = ""

    if file_value:
        source = f"{ENV_SA_FILE} file"
        try:
            raw = Path(file_value).read_text(encoding="utf-8")
        except OSError as error:
            raise SignatureAuthError(
                f"{ENV_SA_FILE} is set but the file could not be read "
                f"({error.strerror or error.__class__.__name__}; path "
                f"{redact_for_log(file_value)}). {_setup_guidance()}"
            ) from None
    else:
        inline_value = os.environ.get(ENV_SA_JSON, "")
        if inline_value.strip():
            source = ENV_SA_JSON
            raw = inline_value

    if raw is None:
        raise SignatureAuthError(
            f"No signature service account configured. {_setup_guidance()}"
        )

    try:
        info = json.loads(raw)
    except json.JSONDecodeError as error:
        # Only the position is reported. The offending text is never echoed.
        raise SignatureAuthError(
            f"{source} is not valid JSON (line {error.lineno}, column "
            f"{error.colno}). Paste the whole key file as downloaded from "
            "Google Cloud, without edits."
        ) from None

    if not isinstance(info, dict):
        raise SignatureAuthError(
            f"{source} must be a JSON object (a service-account key file), "
            f"got {type(info).__name__}."
        )
    if info.get("type") != "service_account":
        raise SignatureAuthError(
            f"{source} is not a service-account key: its 'type' is not "
            "'service_account'. Download the JSON key for the service account "
            "from Google Cloud IAM, not an OAuth client or user credential."
        )
    missing = [k for k in _REQUIRED_KEY_FIELDS if not str(info.get(k) or "").strip()]
    if missing:
        raise SignatureAuthError(
            f"{source} is missing required key fields: {', '.join(missing)}."
        )
    return info


def service_account_email() -> str:
    """The service account's own address (the ``client_email`` of the key)."""
    return str(load_service_account_info()["client_email"])


# --- Credential construction ---------------------------------------------------


def _check_scopes(scopes: List[str]) -> List[str]:
    if not scopes:
        raise SignatureAuthError("At least one scope is required to build credentials.")
    cleaned = [str(s).strip() for s in scopes]
    if any(not s for s in cleaned):
        raise SignatureAuthError("Scopes may not be blank.")
    return cleaned


def _check_subject(subject: Optional[str]) -> str:
    cleaned = (subject or "").strip()
    if not cleaned or "@" not in cleaned:
        raise SignatureAuthError(
            "An impersonation subject (the user's primary email) is required."
        )
    return cleaned


def delegated_credentials(subject: str, scopes: List[str]):
    """Credentials that act AS ``subject`` via domain-wide delegation."""
    subject = _check_subject(subject)
    scopes = _check_scopes(scopes)
    info = load_service_account_info()
    base = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    return base.with_subject(subject)


def service_account_credentials(scopes: List[str]):
    """Credentials for the service account's own identity (no subject).

    Used for the ledger Sheet, which is shared with the service account
    directly so no user needs to be impersonated to write it.
    """
    scopes = _check_scopes(scopes)
    info = load_service_account_info()
    return service_account.Credentials.from_service_account_info(info, scopes=scopes)


def build_gmail_for_user(user_email: str):
    """Gmail v1 client impersonating ``user_email`` with the settings scope only."""
    creds = delegated_credentials(user_email, GMAIL_SCOPES)
    return discovery.build("gmail", "v1", credentials=creds, cache_discovery=False)


def build_directory_as_admin():
    """Admin Directory client impersonating the configured Directory admin."""
    creds = delegated_credentials(directory_admin_email(), DIRECTORY_SCOPES)
    return discovery.build(
        "admin", "directory_v1", credentials=creds, cache_discovery=False
    )


def build_sheets_as_service_account():
    """Sheets v4 client acting as the service account itself."""
    creds = service_account_credentials(LEDGER_SCOPES)
    return discovery.build("sheets", "v4", credentials=creds, cache_discovery=False)


# --- Identities and the caller allowlist --------------------------------------


def directory_admin_email() -> str:
    """The Workspace admin the service account impersonates for Directory reads.

    Defaults to ``oliver@otbgroup.co.uk``. An env var that is set but blank is
    a configuration error, not a reason to fall back silently.
    """
    if ENV_DIRECTORY_ADMIN in os.environ:
        value = os.environ[ENV_DIRECTORY_ADMIN].strip().lower()
        if not value or "@" not in value:
            raise SignatureAuthError(
                f"{ENV_DIRECTORY_ADMIN} is set but is not an email address. "
                "Unset it to use the default, or set it to the admin the "
                "service account should impersonate for Directory reads."
            )
        return value
    return DEFAULT_DIRECTORY_ADMIN


def allowed_admin_emails() -> Set[str]:
    """Addresses allowed to call the signature tools (lower-cased, stripped).

    Defaults to ``oliver@otbgroup.co.uk`` only. Comma-separated in the env var.
    An env var that is set but yields no addresses is refused rather than
    silently widened or narrowed.
    """
    if ENV_ADMIN_EMAILS in os.environ:
        raw = os.environ[ENV_ADMIN_EMAILS]
        source = ENV_ADMIN_EMAILS
    else:
        raw = DEFAULT_ADMIN_EMAILS
        source = "default"
    emails = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not emails:
        raise SignatureAuthError(
            f"{source} contains no email addresses. Set {ENV_ADMIN_EMAILS} to a "
            "comma-separated list, or unset it for the default."
        )
    bad = sorted(e for e in emails if "@" not in e)
    if bad:
        raise SignatureAuthError(
            f"{ENV_ADMIN_EMAILS} contains entries that are not email addresses: "
            f"{', '.join(redact_for_log(b) for b in bad)}."
        )
    return emails


def assert_caller_allowed(caller_email: Optional[str]) -> None:
    """Refuse unless ``caller_email`` is on the allowlist (case-insensitive).

    ``None`` (no authenticated identity on the request) is always refused:
    these tools write to other people's mailboxes and must never run for an
    anonymous caller.
    """
    cleaned = (caller_email or "").strip().lower()
    if not cleaned:
        raise SignatureAuthError(
            "Signature tools need an authenticated caller and none was "
            "resolved on this request."
        )
    if cleaned not in allowed_admin_emails():
        logger.warning(
            "signature tool refused for caller %s (not on %s)",
            redact_for_log(cleaned),
            ENV_ADMIN_EMAILS,
        )
        raise SignatureAuthError(
            "This caller is not allowed to manage signatures. Only the "
            f"addresses in {ENV_ADMIN_EMAILS} may use these tools."
        )


# --- Logging -----------------------------------------------------------------


def redact_for_log(value: Optional[str]) -> str:
    """Shorten a path or address for a log line.

    Values of eight characters or fewer (and ``None``) become ``***``. Longer
    values keep two characters at each end, which is enough to tell two
    entries apart and not enough to reconstruct either, and the result is
    always shorter than the input. Never pass the key or
    the key JSON to this; the key is not logged at all, in any form.
    """
    if value is None:
        return _MASK
    text = str(value)
    if len(text) <= _REDACT_FULL_MASK_MAX_LEN:
        return _MASK
    return f"{text[:_REDACT_KEEP]}{_MASK}{text[-_REDACT_KEEP:]}"

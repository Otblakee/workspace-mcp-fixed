"""
Thin async wrappers over the Gmail settings and Admin Directory calls the
signature tools make.

Nothing here decides anything. Each function makes one kind of API call,
validates its inputs before sending, runs the request through
``gdrive.drive_batch.execute_with_backoff`` (Gmail and the Admin SDK raise
``HttpError`` in the same shape as Drive) and returns the raw resource. The
rendering and policy live in ``gsignatures/engine.py``; the credentials come
from ``gsignatures/sa_auth.py``.

Gmail API facts this module relies on (Gmail v1 discovery document bundled
with googleapiclient, ``resources.users.settings.sendAs``):

* ``sendAs.list`` returns the primary address and every custom "from" alias.
  Order is not guaranteed, so ``list_send_as`` puts the primary first.
* ``sendAs.patch`` and ``sendAs.update`` both accept the basic settings scope
  and the sharing settings scope; we hold the basic scope only. ``patch`` is
  used because it updates just the fields sent, so a body of
  ``{"signature": html}`` cannot disturb display name, reply-to or the
  default flag.
* Addresses other than the primary can only be updated by service account
  clients with domain-wide delegation. Every ``gmail`` object passed in here
  must therefore come from ``sa_auth.build_gmail_for_user``.
* Gmail sanitises the HTML signature before saving it. ``patch_signature``
  therefore returns the fresh ``sendAs.get`` read-back, not the patch
  response, so the caller can hash what Gmail actually stored.

Admin SDK facts (Directory API v1):

* ``users.list`` needs exactly one of ``customer`` or ``domain``.
  ``customer='my_customer'`` covers every domain in the account.
* The ``query`` parameter uses the Directory user-search syntax. Clauses are
  separated by a space and are implicitly ANDed together; values containing
  spaces are single-quoted, so an OU filter is ``orgUnitPath='/01 OTB'``.
  The OU clause matches that OU and everything under it.
* ``users.list`` caps ``maxResults`` at 500 per page; results are paginated
  with ``pageToken`` and drained fully here.
* ``members.list`` returns direct members only. Nested groups appear as
  members with ``type=GROUP`` and are not expanded.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from gdrive.drive_batch import execute_with_backoff, paginate

logger = logging.getLogger(__name__)

# Admin SDK page-size ceilings.
USERS_LIST_MAX_PAGE_SIZE = 500
MEMBERS_LIST_MAX_PAGE_SIZE = 200

_MY_CUSTOMER = "my_customer"


def _require_email(value: Optional[str], what: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned or "@" not in cleaned:
        raise ValueError(f"{what} must be an email address, got {value!r}.")
    return cleaned


# --- Gmail send-as -------------------------------------------------------------


async def list_send_as(gmail) -> List[Dict[str, Any]]:
    """All send-as entries for the impersonated user, primary first.

    The relative order of the aliases after the primary is the order Gmail
    returned them in.
    """
    response = await execute_with_backoff(
        lambda: gmail.users().settings().sendAs().list(userId="me"),
        label="gmail-sendas-list",
    )
    entries = list(response.get("sendAs") or [])
    # sorted() is stable, so aliases keep Gmail's order among themselves.
    return sorted(entries, key=lambda entry: not bool(entry.get("isPrimary")))


async def get_send_as(gmail, send_as_email: str) -> Dict[str, Any]:
    """One send-as entry, as Gmail currently stores it."""
    send_as_email = _require_email(send_as_email, "send_as_email")
    return await execute_with_backoff(
        lambda: (
            gmail.users()
            .settings()
            .sendAs()
            .get(userId="me", sendAsEmail=send_as_email)
        ),
        label="gmail-sendas-get",
    )


async def patch_signature(gmail, send_as_email: str, html: str) -> Dict[str, Any]:
    """Set the HTML signature on one send-as entry and return the read-back.

    The body is exactly ``{"signature": html}``: nothing else on the entry is
    touched. The return value is a fresh ``sendAs.get`` after the write, so
    ``signature`` in it is Gmail's sanitised version, which is what the
    ledger records. Setting the same signature twice is harmless, so the
    request is retried as idempotent.
    """
    send_as_email = _require_email(send_as_email, "send_as_email")
    if not isinstance(html, str):
        raise ValueError(
            f"html must be a string (an empty string clears the signature), "
            f"got {type(html).__name__}."
        )
    body = {"signature": html}
    await execute_with_backoff(
        lambda: (
            gmail.users()
            .settings()
            .sendAs()
            .patch(userId="me", sendAsEmail=send_as_email, body=body)
        ),
        label="gmail-sendas-patch",
        idempotent=True,
    )
    return await get_send_as(gmail, send_as_email)


# --- Admin Directory -------------------------------------------------------------


async def get_directory_user(directory, user_key: str) -> Dict[str, Any]:
    """One user with the full projection (name, organizations, phones, OU)."""
    user_key = (user_key or "").strip()
    if not user_key:
        raise ValueError("user_key (primary email or user ID) is required.")
    return await execute_with_backoff(
        lambda: directory.users().get(userKey=user_key, projection="full"),
        label="directory-users-get",
    )


def _validate_ou_path(ou_path: str) -> str:
    cleaned = ou_path.strip()
    if not cleaned.startswith("/"):
        raise ValueError(f"ou_path must start with '/', got {ou_path!r}.")
    if "'" in cleaned:
        # The value is single-quoted in the search query and the Directory
        # search syntax has no escape for a quote inside a quoted value.
        raise ValueError(f"ou_path may not contain a single quote: {ou_path!r}.")
    return cleaned


def build_users_query(
    *,
    ou_path: Optional[str] = None,
    query: Optional[str] = None,
    include_suspended: bool = False,
) -> Optional[str]:
    """Compose the Directory user-search query, or ``None`` for no filter.

    Clauses are joined with a space, which the Directory search syntax reads
    as AND. Exposed so the exact query can be unit-tested without a service.
    """
    clauses: List[str] = []
    if ou_path:
        clauses.append(f"orgUnitPath='{_validate_ou_path(ou_path)}'")
    if not include_suspended:
        clauses.append("isSuspended=false")
    if query and query.strip():
        clauses.append(query.strip())
    return " ".join(clauses) if clauses else None


async def list_directory_users(
    directory,
    *,
    ou_path: Optional[str] = None,
    domain: Optional[str] = None,
    query: Optional[str] = None,
    include_suspended: bool = False,
    max_results: int = USERS_LIST_MAX_PAGE_SIZE,
) -> List[Dict[str, Any]]:
    """Every matching user with the full projection, all pages drained.

    ``ou_path`` becomes an ``orgUnitPath='...'`` clause (that OU and its
    children). ``query`` is any extra Directory search clause and is ANDed
    with the generated ones. ``include_suspended=False`` (the default) adds
    ``isSuspended=false``. ``domain`` restricts to one domain in place of
    ``customer='my_customer'``. ``max_results`` is the page size, capped at
    the API's 500; it is not a cap on the total.
    """
    if not isinstance(max_results, int) or max_results < 1:
        raise ValueError(
            f"max_results must be a positive integer, got {max_results!r}."
        )
    search = build_users_query(
        ou_path=ou_path, query=query, include_suspended=include_suspended
    )
    params: Dict[str, Any] = {
        "projection": "full",
        "maxResults": min(max_results, USERS_LIST_MAX_PAGE_SIZE),
        "orderBy": "email",
    }
    domain = (domain or "").strip().lower()
    if domain:
        params["domain"] = domain
    else:
        params["customer"] = _MY_CUSTOMER
    if search:
        params["query"] = search

    def factory(page_token: Optional[str]):
        page_params = dict(params)
        if page_token:
            page_params["pageToken"] = page_token
        return directory.users().list(**page_params)

    return await paginate(factory, items_key="users", label="directory-users-list")


async def list_group_member_emails(directory, group_email: str) -> List[str]:
    """Lower-cased addresses of the group's direct USER members.

    Members of ``type`` GROUP (nested groups), CUSTOMER and anything else are
    dropped, and nested groups are NOT expanded: a user who is only in a
    sub-group is not returned. Expand sub-groups by calling this again for
    each of them if that matters for the run.
    """
    group_email = _require_email(group_email, "group_email")

    def factory(page_token: Optional[str]):
        page_params: Dict[str, Any] = {
            "groupKey": group_email,
            "maxResults": MEMBERS_LIST_MAX_PAGE_SIZE,
        }
        if page_token:
            page_params["pageToken"] = page_token
        return directory.members().list(**page_params)

    members = await paginate(
        factory, items_key="members", label="directory-members-list"
    )
    emails: List[str] = []
    for member in members:
        if (member.get("type") or "").upper() != "USER":
            continue
        email = (member.get("email") or "").strip().lower()
        if email:
            emails.append(email)
    return emails

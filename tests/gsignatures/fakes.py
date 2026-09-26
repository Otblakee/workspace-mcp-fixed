"""Shared Google service doubles for the gsignatures tests.

All fakes expose the googleapiclient call shape
(``service.users().settings().sendAs().patch(...).execute()``) so the code
under test is exercised end to end, with ``execute`` run in a worker thread
exactly as it is in production. No fake ever touches the network.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from googleapiclient.errors import HttpError


def request(result):
    req = MagicMock()
    if isinstance(result, Exception):
        req.execute.side_effect = result
    else:
        req.execute.return_value = result
    return req


def http_error(status: int, reason: str = "notFound") -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = reason
    body = f'{{"error": {{"errors": [{{"reason": "{reason}"}}]}}}}'.encode()
    return HttpError(resp, body)


def user(
    email: str,
    ou: str,
    *,
    given: str = "Test",
    family: str = "Person",
    full: Optional[str] = "Test Person",
    title: Optional[str] = "Director",
    phones: Optional[list] = None,
    suspended: bool = False,
    archived: bool = False,
) -> dict:
    """An Admin SDK ``users.get`` resource in the shape the engine reads."""
    name = {"givenName": given, "familyName": family}
    if full is not None:
        name["fullName"] = full
    organizations = []
    if title is not None:
        organizations.append({"title": title, "primary": True})
    return {
        "primaryEmail": email,
        "name": name,
        "organizations": organizations,
        "phones": phones or [],
        "orgUnitPath": ou,
        "suspended": suspended,
        "archived": archived,
    }


def send_as(email: str, *, primary: bool = False, signature: str = "") -> dict:
    return {
        "sendAsEmail": email,
        "isPrimary": primary,
        "isDefault": primary,
        "displayName": "",
        "signature": signature,
        "treatAsAlias": not primary,
        "verificationStatus": "accepted",
    }


class FakeGmail:
    """Gmail double for one mailbox, keyed by sendAsEmail.

    ``patch`` stores a sanitised copy of the signature (real Gmail sanitises
    on save) so the read-back differs from what was sent and the tests can
    tell the two apart. ``fail_patch_for`` names addresses whose patch raises.
    """

    def __init__(self, entries: Optional[List[dict]] = None):
        self.calls: List[tuple] = []
        self.send_as: Dict[str, dict] = {
            s["sendAsEmail"]: dict(s) for s in (entries or [])
        }
        self.sanitise = lambda html: html.replace("<!-- c -->", "")
        self.fail_patch_for: Dict[str, Exception] = {}
        # When set, every patch appends ("sendAs.patch", sendAsEmail) here.
        # Share one list with a FakeSheets to see writes in wall-clock order.
        self.timeline: Optional[List[tuple]] = None

    def call_names(self) -> List[str]:
        return [name for name, _ in self.calls]

    def users(self):
        parent = self

        class _SendAs:
            def list(self, **kwargs):
                parent.calls.append(("sendAs.list", kwargs))
                ordered = sorted(
                    parent.send_as.values(), key=lambda s: bool(s.get("isPrimary"))
                )
                return request({"sendAs": [dict(s) for s in ordered]})

            def get(self, **kwargs):
                parent.calls.append(("sendAs.get", kwargs))
                return request(dict(parent.send_as[kwargs["sendAsEmail"]]))

            def patch(self, **kwargs):
                parent.calls.append(("sendAs.patch", kwargs))
                address = kwargs["sendAsEmail"]
                if parent.timeline is not None:
                    parent.timeline.append(("sendAs.patch", address))
                if address in parent.fail_patch_for:
                    return request(parent.fail_patch_for[address])
                entry = parent.send_as[address]
                entry["signature"] = parent.sanitise(kwargs["body"]["signature"])
                return request({"sendAsEmail": address, "patched": True})

        class _Settings:
            def sendAs(self):
                return _SendAs()

        class _Users:
            def settings(self):
                return _Settings()

        return _Users()


class FakeGmailPool:
    """One FakeGmail per user; ``factory`` stands in for build_gmail_for_user."""

    def __init__(self):
        self.mailboxes: Dict[str, FakeGmail] = {}
        self.factory_calls: List[str] = []
        self.fail_for: Dict[str, Exception] = {}

    def add(self, email: str, entries: List[dict]) -> FakeGmail:
        box = FakeGmail(entries)
        self.mailboxes[email.lower()] = box
        return box

    def factory(self, email: str) -> FakeGmail:
        self.factory_calls.append(email)
        key = email.lower()
        if key in self.fail_for:
            raise self.fail_for[key]
        if key not in self.mailboxes:
            raise KeyError(f"no fake mailbox for {email}")
        return self.mailboxes[key]

    def patch_calls(self) -> List[tuple]:
        return [
            (email, c[1]["sendAsEmail"])
            for email, box in self.mailboxes.items()
            for c in box.calls
            if c[0] == "sendAs.patch"
        ]


def _ou_under(ou_path: str, prefix: str) -> bool:
    ou_path = ou_path.rstrip("/") or "/"
    prefix = prefix.rstrip("/") or "/"
    return ou_path == prefix or ou_path.startswith(prefix + "/")


class FakeDirectory:
    """Admin Directory double: users keyed by primaryEmail, groups by address.

    ``users.list`` honours the ``orgUnitPath='...'`` and ``isSuspended=false``
    query clauses and the ``domain`` parameter, in one page.
    """

    def __init__(self, users: Optional[List[dict]] = None, groups=None):
        self.calls: List[tuple] = []
        self.users_by_email: Dict[str, dict] = {
            u["primaryEmail"].lower(): u for u in (users or [])
        }
        self.groups: Dict[str, List[dict]] = {
            k.lower(): list(v) for k, v in (groups or {}).items()
        }
        self.fail_get_for: Dict[str, Exception] = {}

    def call_names(self) -> List[str]:
        return [name for name, _ in self.calls]

    def _matches(self, u: dict, params: Dict[str, Any]) -> bool:
        query = params.get("query") or ""
        if "isSuspended=false" in query and u.get("suspended"):
            return False
        if "orgUnitPath='" in query:
            ou = query.split("orgUnitPath='", 1)[1].split("'", 1)[0]
            if not _ou_under(str(u.get("orgUnitPath") or "/"), ou):
                return False
        domain = params.get("domain")
        if domain and not u["primaryEmail"].lower().endswith("@" + domain):
            return False
        return True

    def users(self):
        parent = self

        class _Users:
            def get(self, **kwargs):
                parent.calls.append(("users.get", kwargs))
                key = kwargs["userKey"].lower()
                if key in parent.fail_get_for:
                    return request(parent.fail_get_for[key])
                found = parent.users_by_email.get(key)
                if found is None:
                    return request(http_error(404))
                return request(dict(found))

            def list(self, **kwargs):
                parent.calls.append(("users.list", kwargs))
                matched = [
                    dict(u)
                    for u in sorted(
                        parent.users_by_email.values(),
                        key=lambda u: u["primaryEmail"],
                    )
                    if parent._matches(u, kwargs)
                ]
                return request({"users": matched})

        return _Users()

    def members(self):
        parent = self

        class _Members:
            def list(self, **kwargs):
                parent.calls.append(("members.list", kwargs))
                key = kwargs["groupKey"].lower()
                if key not in parent.groups:
                    return request(http_error(404))
                return request({"members": [dict(m) for m in parent.groups[key]]})

        return _Members()


def _tab_of(a1_range: str) -> str:
    name = a1_range.split("!")[0]
    if name.startswith("'") and name.endswith("'"):
        name = name[1:-1].replace("''", "'")
    return name


class FakeSheets:
    """Sheets double: ``tabs`` maps a tab title to its rows.

    ``fail_reads`` / ``fail_writes`` make every read or write raise, for the
    "ledger unreachable" cases. ``fail_append`` makes every append raise;
    ``fail_append_on_calls`` names the 1-based append calls that raise (for
    example ``{2}`` fails the completed row of the first address and nothing
    else). ``append_calls`` counts appends attempted, failed ones included.
    """

    def __init__(self, tabs=None):
        self.calls: List[tuple] = []
        self.tabs: Dict[str, List[list]] = {
            k: [list(r) for r in v] for k, v in (tabs or {}).items()
        }
        self.fail_reads: Optional[Exception] = None
        self.fail_writes: Optional[Exception] = None
        self.fail_append: Optional[Exception] = None
        self.fail_append_on_calls: Dict[int, Exception] = {}
        self.append_calls: int = 0
        # When set, every append records ("values.append", [send_as, ...])
        # here. Share one list with a FakeGmail to see writes in order.
        self.timeline: Optional[List[tuple]] = None

    def names(self) -> List[str]:
        return [c[0] for c in self.calls]

    def spreadsheets(self):
        parent = self

        class _Values:
            def get(self, **kwargs):
                parent.calls.append(("values.get", kwargs))
                if parent.fail_reads is not None:
                    return request(parent.fail_reads)
                tab = _tab_of(kwargs["range"])
                if tab not in parent.tabs:
                    return request(http_error(400, "badRequest"))
                rows = parent.tabs.get(tab, [])
                body: Dict[str, Any] = {"range": kwargs["range"]}
                if rows:
                    body["values"] = [list(r) for r in rows]
                return request(body)

            def append(self, **kwargs):
                parent.calls.append(("values.append", kwargs))
                parent.append_calls += 1
                if parent.timeline is not None:
                    from gsignatures.ledger import LEDGER_HEADER

                    send_as_col = LEDGER_HEADER.index("send_as_email")
                    readback_col = LEDGER_HEADER.index("readback_hash")
                    parent.timeline.append(
                        (
                            "values.append",
                            [
                                (row[send_as_col], row[readback_col])
                                for row in kwargs["body"]["values"]
                            ],
                        )
                    )
                if parent.fail_writes is not None:
                    return request(parent.fail_writes)
                if parent.fail_append is not None:
                    return request(parent.fail_append)
                if parent.append_calls in parent.fail_append_on_calls:
                    return request(parent.fail_append_on_calls[parent.append_calls])
                tab = _tab_of(kwargs["range"])
                parent.tabs.setdefault(tab, []).extend(kwargs["body"]["values"])
                return request(
                    {"updates": {"updatedRows": len(kwargs["body"]["values"])}}
                )

            def update(self, **kwargs):
                parent.calls.append(("values.update", kwargs))
                if parent.fail_writes is not None:
                    return request(parent.fail_writes)
                tab = _tab_of(kwargs["range"])
                values = kwargs["body"]["values"]
                existing = parent.tabs.setdefault(tab, [])
                for i, row in enumerate(values):
                    if i < len(existing):
                        existing[i] = list(row)
                    else:
                        existing.append(list(row))
                return request({"updatedRows": len(values)})

            def clear(self, **kwargs):
                parent.calls.append(("values.clear", kwargs))
                if parent.fail_writes is not None:
                    return request(parent.fail_writes)
                parent.tabs[_tab_of(kwargs["range"])] = []
                return request({})

        class _Spreadsheets:
            def get(self, **kwargs):
                parent.calls.append(("spreadsheets.get", kwargs))
                if parent.fail_reads is not None:
                    return request(parent.fail_reads)
                return request(
                    {
                        "sheets": [
                            {"properties": {"title": title}} for title in parent.tabs
                        ]
                    }
                )

            def batchUpdate(self, **kwargs):
                parent.calls.append(("spreadsheets.batchUpdate", kwargs))
                if parent.fail_writes is not None:
                    return request(parent.fail_writes)
                for req in kwargs["body"]["requests"]:
                    title = req["addSheet"]["properties"]["title"]
                    parent.tabs.setdefault(title, [])
                return request(
                    {"replies": [{"addSheet": {"properties": {"sheetId": 7}}}]}
                )

            def values(self):
                return _Values()

        return _Spreadsheets()

    def ledger_rows(self) -> List[dict]:
        """Ledger data rows as dicts (header excluded), pending rows included."""
        from gsignatures.ledger import LEDGER_HEADER, LEDGER_TAB

        rows = self.tabs.get(LEDGER_TAB, [])
        return [dict(zip(LEDGER_HEADER, r)) for r in rows[1:]]

    def completed_ledger_rows(self) -> List[dict]:
        """Ledger data rows whose apply completed (no ``pending`` rows)."""
        from gsignatures.ledger import is_pending_ledger_row

        return [r for r in self.ledger_rows() if not is_pending_ledger_row(r)]

    def pending_ledger_rows(self) -> List[dict]:
        """Only the ``pending`` rows an apply writes before its patch."""
        from gsignatures.ledger import is_pending_ledger_row

        return [r for r in self.ledger_rows() if is_pending_ledger_row(r)]

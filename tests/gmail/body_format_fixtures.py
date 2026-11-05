"""Fixture Gmail API payloads and a fake Gmail service for the body_format tests.

Five message shapes, named after the cases in the task:

(a) ``PLAIN``        text/plain only, body inline.
(b) ``ALTERNATIVE``  multipart/alternative with text/plain and text/html.
(c) ``MIXED``        multipart/mixed holding multipart/related, which holds a
                     multipart/alternative plus an inline image/png referenced
                     by ``cid:``; a PDF attachment sits at the top level.
(d) ``HTML_ATTACHED`` multipart/alternative whose text/html body is stored under
                     ``body.attachmentId`` (Gmail does this for large bodies).
(e) ``RAW``          same tree as (b) plus a ``raw`` RFC 2822 source.

The fake service answers ``messages.get`` by ``format`` (metadata, full, raw),
``messages.attachments.get`` by ``(messageId, id)`` and ``threads.get`` by
``format`` (full, minimal). It also supports ``new_batch_http_request``, so the
batch tool runs through its real batch path. Every call is recorded in
``service.calls`` so tests can assert which Gmail endpoints were hit.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

USER = "oliver@otbgroup.co.uk"


def b64url(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.urlsafe_b64encode(data).decode("ascii")


def _headers(**kv) -> List[Dict[str, str]]:
    return [{"name": k.replace("_", "-"), "value": v} for k, v in kv.items()]


BASE_HEADERS = dict(
    Subject="Signature test",
    From="Oli Blake <oliver@otbgroup.co.uk>",
    To="someone@example.com",
    Cc="cc@example.com",
    Date="Wed, 5 Nov 2026 09:00:00 +0000",
    Message_ID="<sig-test@otbgroup.co.uk>",
)

PLAIN_TEXT = "Hello from the plain text body.\nSecond line.\n"
ALT_TEXT = "Kind regards,\nOli\n[image: BIR Logo]\n[image: frame-artwork]\n"
ALT_HTML = (
    "<html><body><p>Kind regards,<br>Oli</p>"
    '<img src="https://cdn.example.com/bir-logo.png" alt="BIR Logo">'
    '<img src="https://cdn.example.com/frame-artwork.png" alt="frame-artwork">'
    "</body></html>"
)
RELATED_HTML = (
    "<html><body><p>Inline image follows.</p>"
    '<img src="cid:logo@bir" alt="BIR Logo">'
    '<img src="https://cdn.example.com/frame-artwork.png" alt="frame-artwork">'
    "</body></html>"
)
BIG_HTML = "<html><body>" + "<p>Stored under attachmentId.</p>" * 20 + "</body></html>"

RAW_SOURCE = (
    "From: Oli Blake <oliver@otbgroup.co.uk>\r\n"
    "To: someone@example.com\r\n"
    "Subject: Signature test\r\n"
    "Date: Wed, 5 Nov 2026 09:00:00 +0000\r\n"
    "Message-ID: <sig-test@otbgroup.co.uk>\r\n"
    "MIME-Version: 1.0\r\n"
    'Content-Type: multipart/alternative; boundary="b1"\r\n'
    "\r\n--b1\r\n"
    "Content-Type: text/plain; charset=UTF-8\r\n\r\n"
    + ALT_TEXT.replace("\n", "\r\n")
    + "--b1\r\n"
    "Content-Type: text/html; charset=UTF-8\r\n\r\n" + ALT_HTML + "\r\n--b1--\r\n"
)


def _part(
    mime,
    data=None,
    attachment_id=None,
    filename="",
    headers=None,
    parts=None,
    part_id="0",
):
    body: Dict[str, Any] = {}
    if data is not None:
        body = {"size": len(data), "data": b64url(data)}
    elif attachment_id is not None:
        body = {"size": 1234, "attachmentId": attachment_id}
    else:
        body = {"size": 0}
    part: Dict[str, Any] = {
        "partId": part_id,
        "mimeType": mime,
        "filename": filename,
        "headers": headers or [],
        "body": body,
    }
    if parts is not None:
        part["parts"] = parts
    return part


def _msg(mid: str, payload: Dict[str, Any], thread_id: str = "t1") -> Dict[str, Any]:
    payload = dict(payload)
    payload["headers"] = _headers(**BASE_HEADERS) + payload.get("headers", [])
    return {
        "id": mid,
        "threadId": thread_id,
        "labelIds": ["INBOX"],
        "snippet": "snippet",
        "payload": payload,
        "sizeEstimate": 1000,
    }


# (a) plain text only ------------------------------------------------------
PLAIN = _msg(
    "m-plain",
    _part(
        "text/plain",
        PLAIN_TEXT,
        headers=_headers(Content_Type="text/plain; charset=UTF-8"),
    ),
)

# (b) multipart/alternative ------------------------------------------------
ALTERNATIVE = _msg(
    "m-alt",
    _part(
        "multipart/alternative",
        parts=[
            _part(
                "text/plain",
                ALT_TEXT,
                part_id="0",
                headers=_headers(Content_Type="text/plain; charset=UTF-8"),
            ),
            _part(
                "text/html",
                ALT_HTML,
                part_id="1",
                headers=_headers(Content_Type="text/html; charset=UTF-8"),
            ),
        ],
    ),
)

# (c) multipart/mixed > multipart/related > multipart/alternative + cid image
MIXED = _msg(
    "m-mixed",
    _part(
        "multipart/mixed",
        parts=[
            _part(
                "multipart/related",
                part_id="0",
                parts=[
                    _part(
                        "multipart/alternative",
                        part_id="0.0",
                        parts=[
                            _part("text/plain", ALT_TEXT, part_id="0.0.0"),
                            _part("text/html", RELATED_HTML, part_id="0.0.1"),
                        ],
                    ),
                    _part(
                        "image/png",
                        attachment_id="ATT-LOGO",
                        filename="BIR Logo.png",
                        part_id="0.1",
                        headers=_headers(
                            Content_Type='image/png; name="BIR Logo.png"',
                            Content_Disposition='inline; filename="BIR Logo.png"',
                            Content_ID="<logo@bir>",
                            X_Attachment_Id="logo@bir",
                        ),
                    ),
                ],
            ),
            _part(
                "application/pdf",
                attachment_id="ATT-PDF",
                filename="quote.pdf",
                part_id="1",
                headers=_headers(
                    Content_Disposition='attachment; filename="quote.pdf"'
                ),
            ),
        ],
    ),
)

# (d) html body stored under attachmentId ----------------------------------
HTML_ATTACHED = _msg(
    "m-htmlatt",
    _part(
        "multipart/alternative",
        parts=[
            _part("text/plain", ALT_TEXT, part_id="0"),
            _part(
                "text/html",
                attachment_id="ATT-HTML",
                part_id="1",
                headers=_headers(Content_Type="text/html; charset=UTF-8"),
            ),
        ],
    ),
)

# (e) raw --------------------------------------------------------------------
RAW = _msg("m-raw", ALTERNATIVE["payload"])

ATTACHMENTS: Dict[tuple, Dict[str, Any]] = {
    ("m-htmlatt", "ATT-HTML"): {"size": len(BIG_HTML), "data": b64url(BIG_HTML)},
    ("m-mixed", "ATT-LOGO"): {"size": 1234, "data": b64url(b"\x89PNG fake")},
}

ALL_MESSAGES = {m["id"]: m for m in (PLAIN, ALTERNATIVE, MIXED, HTML_ATTACHED, RAW)}
RAW_SOURCES = {"m-raw": RAW_SOURCE, "m-alt": RAW_SOURCE}


def _metadata_view(message: Dict[str, Any]) -> Dict[str, Any]:
    payload = message["payload"]
    return {
        "id": message["id"],
        "threadId": message["threadId"],
        "payload": {"mimeType": payload["mimeType"], "headers": payload["headers"]},
    }


class _Request:
    def __init__(self, result):
        self._result = result

    def execute(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _Attachments:
    def __init__(self, service: "FakeGmailService"):
        self._service = service

    def get(self, userId, messageId, id):
        self._service.calls.append(
            ("attachments.get", {"messageId": messageId, "id": id})
        )
        att = self._service.attachments.get((messageId, id))
        if att is None:
            return _Request(KeyError(f"no attachment {id} on {messageId}"))
        return _Request(att)


class _Messages:
    def __init__(self, service: "FakeGmailService"):
        self._service = service

    def attachments(self):
        return _Attachments(self._service)

    def get(self, userId, id, format="full", metadataHeaders=None, fields=None):
        self._service.calls.append(("messages.get", {"id": id, "format": format}))
        message = self._service.messages.get(id)
        if message is None:
            return _Request(KeyError(f"no message {id}"))
        if format == "metadata":
            return _Request(_metadata_view(message))
        if format == "raw":
            source = self._service.raw_sources.get(id)
            if source is None:
                return _Request(KeyError(f"no raw source for {id}"))
            return _Request(
                {"id": id, "threadId": message["threadId"], "raw": b64url(source)}
            )
        return _Request(message)


class _Threads:
    def __init__(self, service: "FakeGmailService"):
        self._service = service

    def get(self, userId, id, format="full", metadataHeaders=None):
        self._service.calls.append(("threads.get", {"id": id, "format": format}))
        ids = self._service.threads.get(id)
        if ids is None:
            return _Request(KeyError(f"no thread {id}"))
        if format == "minimal":
            return _Request(
                {"id": id, "messages": [{"id": m, "threadId": id} for m in ids]}
            )
        if format == "metadata":
            return _Request(
                {
                    "id": id,
                    "messages": [
                        _metadata_view(self._service.messages[m]) for m in ids
                    ],
                }
            )
        return _Request(
            {"id": id, "messages": [self._service.messages[m] for m in ids]}
        )


class _Users:
    def __init__(self, service: "FakeGmailService"):
        self._service = service

    def messages(self):
        return _Messages(self._service)

    def threads(self):
        return _Threads(self._service)


class _Batch:
    def __init__(self, callback):
        self._callback = callback
        self._requests = []

    def add(self, request, request_id=None):
        self._requests.append((request_id, request))

    def execute(self):
        for request_id, request in self._requests:
            try:
                self._callback(request_id, request.execute(), None)
            except Exception as exc:  # pragma: no cover - mirrors googleapiclient
                self._callback(request_id, None, exc)


class FakeGmailService:
    def __init__(
        self,
        messages: Optional[Dict[str, Dict[str, Any]]] = None,
        attachments: Optional[Dict[tuple, Dict[str, Any]]] = None,
        raw_sources: Optional[Dict[str, str]] = None,
        threads: Optional[Dict[str, List[str]]] = None,
    ):
        self.messages = dict(ALL_MESSAGES if messages is None else messages)
        self.attachments = dict(ATTACHMENTS if attachments is None else attachments)
        self.raw_sources = dict(RAW_SOURCES if raw_sources is None else raw_sources)
        self.threads = dict(
            {"t1": ["m-plain", "m-alt", "m-mixed", "m-htmlatt"], "t-raw": ["m-raw"]}
            if threads is None
            else threads
        )
        self.calls: List[tuple] = []

    def users(self):
        return _Users(self)

    def new_batch_http_request(self, callback=None):
        return _Batch(callback)

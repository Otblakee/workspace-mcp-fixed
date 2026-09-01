"""Small, dependency-free redaction helpers shared by logging and audit code."""

from __future__ import annotations

import re

# Google's HttpError.__str__ embeds the failing request URL, and Drive/Gmail
# list URLs carry the search expression in the query string (``?q=...``).
_URL_QUERY_RE = re.compile(r"(https?://[^\s\"'<>]+?)\?[^\s\"'<>]*")


def scrub_url_queries(text: str) -> str:
    """Replace the query string of every URL in ``text`` with a marker."""
    return _URL_QUERY_RE.sub(r"\1?<redacted-query>", text or "")


def strip_query_string(path: str) -> str:
    """``/oauth2callback?code=…`` -> ``/oauth2callback``."""
    if not isinstance(path, str):
        return path
    return path.split("?", 1)[0]

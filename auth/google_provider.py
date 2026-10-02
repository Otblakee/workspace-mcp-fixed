"""GoogleProvider subclass whose 401 challenge advertises the full scope set.

FastMCP 4 puts ``scope="..."`` in the ``WWW-Authenticate`` header of every
401, filled from ``required_scopes``. This server keeps ``required_scopes``
narrow on purpose (identity only, see ``core/server.py``) so least-privilege
clients pass the front-door gate, and carries the full enabled-service scope
list as ``valid_scopes``. Clients such as claude.ai request exactly the scopes
the challenge names, so with the stock provider a fresh sign-in asked Google
for identity only and every Workspace tool then failed with "lack required
scopes" (2026-09-28). FastMCP 3 sent no scope hint, which is why this never
showed before the upgrade.

The fix: the challenge names the full ``valid_scopes`` list while the
verifier gate stays ``required_scopes``. Metadata (``scopes_supported``) was
already the full list.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

import httpx2
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.google import GoogleProvider, GoogleTokenVerifier
from fastmcp.utilities.token_cache import TokenCache

logger = logging.getLogger(__name__)


# google-auth treats a token as expired REFRESH_THRESHOLD (3 min 45 s) before
# its real expiry and tries to refresh it inside the API client. The per-request
# credentials this server builds carry no refresh token (the proxy owns
# refreshing), so any call in that last window raised RefreshError and the tool
# failed with "sign in again" (seen 18, 23 and 30 September 2026). With this
# threshold the proxy refreshes the upstream Google token itself whenever a
# request arrives within five minutes of expiry, so google-auth never sees a
# stale token. Five minutes clears google-auth's 3 min 45 s with margin.
DEFAULT_TOKEN_EXPIRY_THRESHOLD_SECONDS = 300

# The OAuth proxy swaps the FastMCP JWT for the upstream Google token on
# every request and then asks GoogleTokenVerifier to verify it. The stock
# verifier makes two sequential Google calls (tokeninfo, then userinfo) with
# a fresh httpx client each time, on every POST /mcp, including tools/list.
# Measured on Render: 150 to 290 ms per MCP request, before the tool runs.
# FastMCP 3.3.1 did the same; 4.x only made it visible because the calls
# moved to the httpx2 logger. The GitHub provider in FastMCP ships a
# TokenCache for exactly this; Google's does not, so this subclass adds one.
# Only successful verifications are cached, keyed by SHA-256 of the token,
# and an entry never outlives the token's own expiry.
DEFAULT_VERIFY_CACHE_TTL_SECONDS = 300
VERIFY_CACHE_TTL_ENV = "OAUTH_VERIFY_CACHE_TTL_S"


def verify_cache_ttl_seconds() -> int:
    """TTL for cached upstream-token verifications, from the environment.

    ``OAUTH_VERIFY_CACHE_TTL_S`` unset or blank means the default (300 s);
    ``0`` disables the cache; a negative or non-numeric value falls back to
    the default with a warning rather than crashing start-up.
    """
    raw = os.getenv(VERIFY_CACHE_TTL_ENV, "").strip()
    if not raw:
        return DEFAULT_VERIFY_CACHE_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using %d",
            VERIFY_CACHE_TTL_ENV,
            raw,
            DEFAULT_VERIFY_CACHE_TTL_SECONDS,
        )
        return DEFAULT_VERIFY_CACHE_TTL_SECONDS
    if value < 0:
        logger.warning(
            "%s=%d is negative; using %d",
            VERIFY_CACHE_TTL_ENV,
            value,
            DEFAULT_VERIFY_CACHE_TTL_SECONDS,
        )
        return DEFAULT_VERIFY_CACHE_TTL_SECONDS
    return value


class CachedGoogleTokenVerifier(GoogleTokenVerifier):
    """GoogleTokenVerifier with a TTL cache and one shared HTTP client.

    A cache hit costs no network at all. A miss costs the two Google calls
    the stock verifier always made, but over a pooled ``httpx2.AsyncClient``
    (created lazily inside the running event loop) so the TLS handshake and
    the certificate-store load are not repeated per request.

    Trade-off: a Google token revoked upstream still passes the MCP gate for
    up to ``ttl_seconds``. Every Google API tool then fails on its own
    (Google refuses the token), so the exposure is limited to tools that act
    on the caller's identity alone. ``OAUTH_VERIFY_CACHE_TTL_S=0`` turns the
    cache off; a lower value narrows the window.
    """

    def __init__(self, *args, ttl_seconds: int, **kwargs):
        super().__init__(*args, **kwargs)
        self._cache = TokenCache(ttl_seconds=ttl_seconds)
        self._owned_client: Optional[httpx2.AsyncClient] = None

    @property
    def cache(self) -> TokenCache:
        return self._cache

    def _client(self) -> httpx2.AsyncClient:
        # Built on first use so it binds to the loop that serves requests,
        # not whichever loop (or none) was current at start-up.
        if self._http_client is not None:
            return self._http_client
        if self._owned_client is None:
            self._owned_client = httpx2.AsyncClient(timeout=self.timeout_seconds)
        return self._owned_client

    async def verify_token(self, token: str) -> AccessToken | None:
        hit, cached = self._cache.get(token)
        if hit:
            return cached
        # The base class uses self._http_client when set and otherwise opens
        # a fresh client per call; point it at the shared one for this call.
        shared = self._client()
        previous = self._http_client
        self._http_client = shared
        try:
            result = await super().verify_token(token)
        finally:
            self._http_client = previous
        if result is not None:
            self._cache.set(token, result)
        return result


class WorkspaceGoogleProvider(GoogleProvider):
    """GoogleProvider that challenges with the full valid scope set and
    refreshes the upstream token before google-auth's early-expiry window."""

    def __init__(self, *args, valid_scopes: Optional[List[str]] = None, **kwargs):
        kwargs.setdefault(
            "token_expiry_threshold_seconds", DEFAULT_TOKEN_EXPIRY_THRESHOLD_SECONDS
        )
        super().__init__(*args, valid_scopes=valid_scopes, **kwargs)
        self._workspace_challenge_scopes: List[str] = list(
            valid_scopes or self.required_scopes or []
        )
        self._install_cached_verifier()

    def _install_cached_verifier(self) -> None:
        """Swap the proxy's verifier for the cached one, keeping its settings."""
        stock = self._token_validator
        if not isinstance(stock, GoogleTokenVerifier):  # pragma: no cover
            return
        self._token_validator = CachedGoogleTokenVerifier(
            required_scopes=list(stock.required_scopes or []) or None,
            timeout_seconds=stock.timeout_seconds,
            http_client=stock._http_client,
            audience=stock.audience,
            ttl_seconds=verify_cache_ttl_seconds(),
        )

    @property
    def token_verifier(self) -> GoogleTokenVerifier:
        """The verifier the proxy consults on every request (tests, diagnostics)."""
        return self._token_validator

    def get_challenge_scopes(
        self, required_scopes: Optional[List[str]] = None
    ) -> List[str]:
        """Scopes a client should request, as named in the 401 challenge.

        The default challenge (no explicit scope set, or the gate's own
        ``required_scopes``) is widened to the full valid list so a fresh
        sign-in consents to every enabled service. An explicit narrower set,
        as used for an ``insufficient_scope`` error on one request, passes
        through unchanged.
        """
        if required_scopes is None or list(required_scopes) == list(
            self.required_scopes or []
        ):
            return list(self._workspace_challenge_scopes)
        return super().get_challenge_scopes(required_scopes)

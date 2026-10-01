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

from typing import List, Optional

from fastmcp.server.auth.providers.google import GoogleProvider


# google-auth treats a token as expired REFRESH_THRESHOLD (3 min 45 s) before
# its real expiry and tries to refresh it inside the API client. The per-request
# credentials this server builds carry no refresh token (the proxy owns
# refreshing), so any call in that last window raised RefreshError and the tool
# failed with "sign in again" (seen 18, 23 and 30 September 2026). With this
# threshold the proxy refreshes the upstream Google token itself whenever a
# request arrives within five minutes of expiry, so google-auth never sees a
# stale token. Five minutes clears google-auth's 3 min 45 s with margin.
DEFAULT_TOKEN_EXPIRY_THRESHOLD_SECONDS = 300


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

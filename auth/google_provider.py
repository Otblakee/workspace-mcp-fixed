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


class WorkspaceGoogleProvider(GoogleProvider):
    """GoogleProvider that challenges with the full valid scope set."""

    def __init__(self, *args, valid_scopes: Optional[List[str]] = None, **kwargs):
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

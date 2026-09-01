"""FastMCP middleware that enforces the group-based tool access policy.

Sits directly after ``AuthInfoMiddleware`` in the chain (see
``core/server.py``): that middleware resolves the caller's verified identity
into ``authenticated_user_email`` on the request context; this one turns the
identity into a tool allow-list via ``core.access_policy`` and applies it in
two places:

* ``tools/list`` - tools the caller may not use are removed from the
  listing, so an MCP client never even offers them.
* ``tools/call`` - a call to a tool outside the allow-list is refused with
  an ``AuthorizationError`` *and* recorded in the audit sheet as
  ``status=denied`` (the audited tool wrapper never runs for a refused call,
  so the row has to be written here).

Transport rules match FastMCP's own ``AuthMiddleware``: stdio has no OAuth
identity, so the policy is skipped there. Under streamable-http the policy
is fail-closed: no identity means no tools.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional, Sequence

from fastmcp.exceptions import AuthorizationError
from fastmcp.server.middleware import Middleware, MiddlewareContext

from core.access_policy import (
    ALWAYS_ALLOWED_TOOLS,
    AccessDecision,
    AccessPolicyEngine,
    PolicyError,
    get_engine,
)

logger = logging.getLogger(__name__)


def _transport_is_stdio(context: MiddlewareContext) -> bool:
    """stdio has no OAuth identity, so the policy is skipped there.

    Prefer the request's own transport (FastMCP stamps it on the Context);
    fall back to the process-wide mode only when the context does not carry
    one. Anything unknown counts as *not* stdio, so the policy applies.
    """
    ctx = getattr(context, "fastmcp_context", None)
    transport = getattr(ctx, "transport", None) if ctx is not None else None
    if isinstance(transport, str):
        return transport == "stdio"
    from core.config import get_transport_mode

    return get_transport_mode() == "stdio"


async def _identity(context: MiddlewareContext) -> Optional[str]:
    ctx = context.fastmcp_context
    if ctx is None:
        return None
    try:
        return await ctx.get_state("authenticated_user_email")
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("access policy: could not read identity from context: %s", exc)
        return None


# Sentinel for rows with no verified identity. Never empty: an empty user is
# rewritten to DEFAULT_USER by the writers, which would attribute an
# anonymous refused call to the owner.
UNAUTHENTICATED_USER = "<unauthenticated>"


def _service_for(tool_name: str) -> str:
    """Service label for a denied row, resolved the same way the audited
    wrapper does for success rows (by the tool's source module) so the two
    never disagree in a per-service pivot."""
    from core.audit import _service

    module = ""
    try:
        from core.server import server
        from core.tool_registry import get_tool_components

        component = get_tool_components(server).get(tool_name)
        fn = getattr(component, "fn", component)
        while hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        module = getattr(fn, "__module__", "") or ""
    except Exception:  # pragma: no cover - defensive
        module = ""
    return _service(tool_name, module)


def _audit_denied(
    tool_name: str, decision: AccessDecision, started: float, reason: str
) -> None:
    """Record a refused call in the audit sheet. Fail-soft, like every other
    audit write: a broken audit path never changes the access decision."""
    try:
        from core.audit import (
            _redact,
            _resolve_client,
            _scrub_error_text,
            logger as audit_logger,
        )

        audit_logger().submit(
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "user": decision.email or UNAUTHENTICATED_USER,
                "service": _service_for(tool_name),
                "tool": tool_name,
                "params_summary": _redact(
                    {
                        "policy_source": decision.source,
                        "groups": sorted(decision.groups),
                    }
                ),
                "resource_id": "",
                "status": "denied",
                "error": _scrub_error_text(f"policy: {reason}")[:300],
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "client": _resolve_client(),
            }
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("access policy: audit submit for denial failed: %s", exc)


# Per-user call caps on the tools whose repetition is itself the damage
# (bulk soft-delete, bulk trash, mass mail, mass sharing). Applied only under
# enforce mode; (max_calls, window_seconds). Override with MCP_TOOL_RATE_LIMITS
# as JSON, e.g. {"soft_delete_drive_file": [50, 600]}; a limit of 0 disables.
DEFAULT_RATE_LIMITS: dict = {
    "soft_delete_drive_file": (20, 600),
    "modify_gmail_message_labels": (60, 600),
    "send_gmail_message": (30, 600),
    "update_drive_file": (60, 600),
    "share_calendar": (5, 600),
    "set_drive_permission": (20, 600),
    "create_gmail_filter": (5, 600),
    "create_event": (60, 600),
}
RATE_LIMITS_ENV = "MCP_TOOL_RATE_LIMITS"


def _load_rate_limits() -> dict:
    import json
    import os

    limits = dict(DEFAULT_RATE_LIMITS)
    raw = (os.getenv(RATE_LIMITS_ENV) or "").strip()
    if not raw:
        return limits
    try:
        override = json.loads(raw)
        if not isinstance(override, dict):
            raise ValueError("must be a JSON object")
        for tool, spec in override.items():
            if spec in (0, None, [], [0, 0]):
                limits.pop(tool, None)
                continue
            count, window = spec
            limits[str(tool)] = (int(count), float(window))
    except Exception as exc:
        logger.error("access policy: ignoring invalid %s (%s)", RATE_LIMITS_ENV, exc)
    return limits


class _RateLimiter:
    """Sliding-window counter per (user, tool). Process-local."""

    def __init__(self, limits: Optional[dict] = None, clock=time.monotonic):
        self.limits = limits if limits is not None else _load_rate_limits()
        self._clock = clock
        self._hits: dict = {}

    def check(self, user: str, tool: str) -> Optional[str]:
        """Record a call; return a refusal reason when over the cap."""
        spec = self.limits.get(tool)
        if not spec:
            return None
        max_calls, window = spec
        now = self._clock()
        key = (user, tool)
        hits = [t for t in self._hits.get(key, []) if now - t < window]
        if len(hits) >= max_calls:
            self._hits[key] = hits
            return (
                f"rate limit: {max_calls} calls of {tool} per {int(window)}s "
                "reached; wait or ask an admin to do this in bulk"
            )
        hits.append(now)
        self._hits[key] = hits
        return None


class AccessPolicyMiddleware(Middleware):
    """Filter ``tools/list`` and gate ``tools/call`` by group policy."""

    def __init__(
        self,
        engine: Optional[AccessPolicyEngine] = None,
        rate_limiter: Optional[_RateLimiter] = None,
    ):
        super().__init__()
        self._engine_override = engine
        self._engine_error: Optional[str] = None
        self._rate_limiter = rate_limiter

    def _limiter(self) -> _RateLimiter:
        if self._rate_limiter is None:
            self._rate_limiter = _RateLimiter()
        return self._rate_limiter

    def _engine(self) -> Optional[AccessPolicyEngine]:
        """The engine, or ``None`` when the policy failed to load.

        A policy that does not load is treated as *enforce with nothing
        allowed*: a typo in the YAML must not turn into "everyone gets
        everything". The error is logged once at ERROR and surfaced in every
        denial message so it is impossible to miss.
        """
        if self._engine_override is not None:
            return self._engine_override
        try:
            return get_engine()
        except Exception as exc:
            # PolicyError carries a message written for the operator (a
            # selector typo, a missing file); anything else is an unexpected
            # crash whose text may embed paths, so only its type is shown.
            shown = str(exc) if isinstance(exc, PolicyError) else type(exc).__name__
            if self._engine_error != shown:
                self._engine_error = shown
                logger.error(
                    "access policy: policy failed to load, denying all tool "
                    "access until it is fixed: %s",
                    exc,
                    exc_info=not isinstance(exc, PolicyError),
                )
            return None

    async def on_list_tools(self, context: MiddlewareContext, call_next) -> Sequence:
        tools = await call_next(context)
        if _transport_is_stdio(context):
            return tools
        engine = self._engine()
        if engine is None:
            return [t for t in tools if t.name in ALWAYS_ALLOWED_TOOLS]
        if not engine.enabled:
            return tools
        email = await _identity(context)
        decision = await engine.decide(email, {t.name for t in tools})
        kept = [t for t in tools if t.name in decision.allowed]
        logger.info(
            "access policy: tools/list for %s -> %d of %d tools (source=%s, groups=%s)",
            decision.email or "<unauthenticated>",
            len(kept),
            len(tools),
            decision.source,
            ",".join(sorted(decision.groups)) or "-",
        )
        return kept

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        if _transport_is_stdio(context):
            return await call_next(context)
        started = time.perf_counter()
        tool_name = getattr(context.message, "name", None) or ""
        engine = self._engine()
        if engine is None:
            if tool_name in ALWAYS_ALLOWED_TOOLS:
                return await call_next(context)
            email = await _identity(context)
            decision = AccessDecision(
                email=email,
                groups=frozenset(),
                allowed=frozenset(),
                source="policy_load_error",
                reason=self._engine_error or "policy failed to load",
            )
            _audit_denied(tool_name, decision, started, decision.reason)
            raise AuthorizationError(
                f"Tool '{tool_name}' refused: the access policy failed to load. "
                "An administrator must fix core/group_policy.yaml (details are "
                "in the server log) before any tool can run."
            )
        if not engine.enabled:
            return await call_next(context)

        email = await _identity(context)
        decision = await engine.decide(email, {tool_name})
        if decision.permits(tool_name):
            if decision.source != "breakglass":
                refusal = self._limiter().check(decision.email or "", tool_name)
                if refusal:
                    _audit_denied(tool_name, decision, started, refusal)
                    logger.warning(
                        "access policy: RATE-LIMITED %s for %s",
                        tool_name,
                        decision.email,
                    )
                    raise AuthorizationError(f"Tool '{tool_name}' refused: {refusal}.")
            return await call_next(context)

        if decision.source == "unauthenticated":
            reason = "no verified identity on the request"
        elif decision.source == "lookup_failed":
            reason = f"membership lookup unavailable ({decision.reason})"
        else:
            reason = (
                f"not granted to {decision.email} by any policy group "
                f"(groups: {', '.join(sorted(decision.groups)) or 'none'})"
            )
        _audit_denied(tool_name, decision, started, reason)
        logger.warning(
            "access policy: DENIED %s for %s (%s)",
            tool_name,
            decision.email or "<unauthenticated>",
            reason,
        )
        raise AuthorizationError(
            f"Tool '{tool_name}' is not permitted for this account: {reason}. "
            "Ask a Workspace admin to add you to the right Google Group, or "
            "call get_my_access to see what you are allowed."
        )

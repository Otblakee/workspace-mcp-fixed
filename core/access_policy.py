"""Group-based tool access policy.

Google Workspace group membership decides which MCP tools a signed-in user
can see (``tools/list``) and call (``tools/call``).

Where things live
-----------------
* **Who is in which group** is administered in exactly one place: the Google
  Admin console. HR joiners/leavers, role changes and cover arrangements are
  all a group edit there, with Google's own audit trail.
* **What each group may do** is the version-controlled file
  ``core/group_policy.yaml`` (override the path with
  ``MCP_GROUP_POLICY_FILE``). Changing a capability is a reviewed PR, not a
  console click, which is the right speed for a change that widens what the
  assistant can do on someone's behalf.

What this is NOT
----------------
This narrows what the assistant may *do* on a user's behalf. It never widens
what a user can *see*: every tool still runs with the caller's own OAuth
token, so Google's own permissions remain the outer boundary. A user who is
allowed ``search_drive_files`` still only finds files Google would show them.

Selector grammar (``allow`` / ``deny`` lists)
---------------------------------------------
* ``"*"`` - every registered tool.
* ``"<service>.*"`` - every tool the service declares in ``tool_tiers.yaml``.
* ``"<service>.<tier>"`` - the service's tools up to and including that
  tier (``core`` < ``extended`` < ``complete``), the same cumulative meaning
  ``--tool-tier`` has.
* ``"<tool_name>"`` - one tool. Must exist in ``tool_tiers.yaml`` (typo
  protection: a policy that names a tool that does not exist fails to load
  rather than silently granting nothing).

A user's allowed set is the **union** over every matched group of
``(allow - deny)`` for that group, plus the ``default`` rule (applied to
every authenticated user), plus ``ALWAYS_ALLOWED_TOOLS``. A ``deny`` only
subtracts from its own group's grant; the hard, global stop for a tool is
``core/tool_policy.BLOCKED_TOOLS``, which this module never overrides.

Membership lookup
-----------------
``members.hasMember`` on the Admin SDK Directory API, once per policy group
per user, cached for ``MCP_GROUP_POLICY_CACHE_TTL_S`` (default 300 s).
``hasMember`` reports direct *and* nested membership within the domain, so a
group can contain a group. The lookup identity is a dedicated service
account (``MCP_GROUP_POLICY_SA_JSON_FILE`` or ``..._SA_JSON_B64``) that holds
only the Groups Reader admin role, never a user's token: a user's own
credentials must not be the thing that decides that user's permissions.

Failure behaviour is fail-closed: if the Directory cannot be reached and
there is no cached answer (or it is older than
``MCP_GROUP_POLICY_STALE_TTL_S``), the user gets ``ALWAYS_ALLOWED_TOOLS``
only. ``MCP_GROUP_POLICY_BREAKGLASS_EMAILS`` names accounts that bypass the
lookup entirely (full access, loudly logged) so an outage of the Directory
API cannot lock the owner out of their own server.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Set

import yaml

from core.tool_policy import BLOCKED_TOOLS
from core.tool_tier_loader import ToolTierLoader

logger = logging.getLogger(__name__)

# --- environment -----------------------------------------------------------

MODE_ENV = "MCP_GROUP_POLICY_MODE"  # "off" (default) | "enforce"
FILE_ENV = "MCP_GROUP_POLICY_FILE"
SA_JSON_FILE_ENV = "MCP_GROUP_POLICY_SA_JSON_FILE"
SA_JSON_B64_ENV = "MCP_GROUP_POLICY_SA_JSON_B64"
SUBJECT_ENV = "MCP_GROUP_POLICY_SUBJECT"
STATIC_MEMBERS_ENV = "MCP_GROUP_POLICY_STATIC_MEMBERS"
BREAKGLASS_ENV = "MCP_GROUP_POLICY_BREAKGLASS_EMAILS"
CACHE_TTL_ENV = "MCP_GROUP_POLICY_CACHE_TTL_S"
STALE_TTL_ENV = "MCP_GROUP_POLICY_STALE_TTL_S"

DEFAULT_POLICY_PATH = Path(__file__).parent / "group_policy.yaml"
DEFAULT_CACHE_TTL_S = 300
DEFAULT_STALE_TTL_S = 3600

# The one Directory scope ``members.hasMember`` needs. Keep this list exact:
# a domain-wide-delegation grant (if MCP_GROUP_POLICY_SUBJECT is used) must
# list precisely the scopes the service account requests.
DIRECTORY_MEMBER_READ_SCOPE = (
    "https://www.googleapis.com/auth/admin.directory.group.member.readonly"
)
LOOKUP_SCOPES = [DIRECTORY_MEMBER_READ_SCOPE]

# Tools every authenticated user may always call, whatever their groups.
# ``get_my_access`` only reports the caller's own decision; it reads nothing
# from Google.
ALWAYS_ALLOWED_TOOLS: FrozenSet[str] = frozenset({"get_my_access"})

TIER_ORDER = ("core", "extended", "complete")


class PolicyError(ValueError):
    """The policy file is malformed or references something that does not exist."""


class MembershipLookupError(RuntimeError):
    """The membership source could not answer (privilege, network, quota)."""


def _norm_email(value: Any) -> str:
    return str(value or "").strip().lower()


# --- tool catalogue --------------------------------------------------------


class ToolCatalogue:
    """Read-only view of ``core/tool_tiers.yaml`` used to expand selectors."""

    def __init__(self, loader: Optional[ToolTierLoader] = None):
        self._loader = loader or ToolTierLoader()
        self._by_service: Optional[Dict[str, Dict[str, List[str]]]] = None

    def _load(self) -> Dict[str, Dict[str, List[str]]]:
        if self._by_service is None:
            raw = self._loader._load_config()
            self._by_service = {
                svc: {tier: list(tools or []) for tier, tools in (cfg or {}).items()}
                for svc, cfg in raw.items()
            }
        return self._by_service

    def services(self) -> Set[str]:
        return set(self._load().keys())

    def tools_for(self, service: str, up_to_tier: Optional[str] = None) -> Set[str]:
        cfg = self._load().get(service)
        if cfg is None:
            raise PolicyError(f"unknown service '{service}' in policy selector")
        if up_to_tier is None:
            tiers: Iterable[str] = TIER_ORDER
        else:
            if up_to_tier not in TIER_ORDER:
                raise PolicyError(
                    f"unknown tier '{up_to_tier}' (expected one of {TIER_ORDER})"
                )
            tiers = TIER_ORDER[: TIER_ORDER.index(up_to_tier) + 1]
        out: Set[str] = set()
        for tier in tiers:
            out.update(cfg.get(tier) or [])
        return out

    def all_tools(self) -> Set[str]:
        out: Set[str] = set()
        for svc in self._load():
            out.update(self.tools_for(svc))
        return out


# --- policy model ----------------------------------------------------------


@dataclass(frozen=True)
class GroupRule:
    group: str
    allow: tuple
    deny: tuple
    description: str = ""


@dataclass
class GroupPolicy:
    version: int
    groups: Dict[str, GroupRule]
    default: GroupRule
    source: str

    @property
    def group_emails(self) -> FrozenSet[str]:
        return frozenset(self.groups.keys())

    def is_policy_group(self, email: str) -> bool:
        return _norm_email(email) in self.groups


def _as_selector_list(value: Any, *, where: str) -> tuple:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise PolicyError(f"{where} must be a list of selectors")
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PolicyError(f"{where} contains a non-string or empty selector")
        out.append(item.strip())
    return tuple(out)


def _validate_selector(selector: str, catalogue: ToolCatalogue, *, where: str) -> None:
    """Raise PolicyError unless ``selector`` names something that exists."""
    if selector == "*":
        return
    if "." in selector:
        service, _, tail = selector.partition(".")
        if service not in catalogue.services():
            raise PolicyError(f"{where}: unknown service '{service}' in '{selector}'")
        if tail != "*" and tail not in TIER_ORDER:
            raise PolicyError(
                f"{where}: '{selector}' must be '<service>.*' or '<service>.<tier>'"
            )
        return
    if selector in BLOCKED_TOOLS:
        raise PolicyError(
            f"{where}: '{selector}' is in core/tool_policy.BLOCKED_TOOLS and can "
            "never be registered; remove it from the policy"
        )
    if selector in ALWAYS_ALLOWED_TOOLS:
        return
    if selector not in catalogue.all_tools():
        raise PolicyError(
            f"{where}: unknown tool '{selector}' (not in core/tool_tiers.yaml)"
        )


def _rule_from_mapping(
    group: str, raw: Any, catalogue: ToolCatalogue, *, where: str
) -> GroupRule:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PolicyError(f"{where} must be a mapping with allow/deny keys")
    unknown = set(raw) - {"allow", "deny", "description"}
    if unknown:
        raise PolicyError(f"{where}: unknown keys {sorted(unknown)}")
    allow = _as_selector_list(raw.get("allow"), where=f"{where}.allow")
    deny = _as_selector_list(raw.get("deny"), where=f"{where}.deny")
    for sel in allow:
        _validate_selector(sel, catalogue, where=f"{where}.allow")
    for sel in deny:
        _validate_selector(sel, catalogue, where=f"{where}.deny")
    return GroupRule(
        group=group,
        allow=allow,
        deny=deny,
        description=str(raw.get("description") or ""),
    )


def parse_policy(
    data: Any, *, source: str, catalogue: Optional[ToolCatalogue] = None
) -> GroupPolicy:
    catalogue = catalogue or ToolCatalogue()
    if not isinstance(data, dict):
        raise PolicyError(f"{source}: top level must be a mapping")
    version = data.get("version", 1)
    if version != 1:
        raise PolicyError(f"{source}: unsupported policy version {version!r}")
    unknown = set(data) - {"version", "groups", "default"}
    if unknown:
        raise PolicyError(f"{source}: unknown top-level keys {sorted(unknown)}")

    groups_raw = data.get("groups") or {}
    if not isinstance(groups_raw, dict):
        raise PolicyError(
            f"{source}: 'groups' must be a mapping of group email -> rule"
        )
    groups: Dict[str, GroupRule] = {}
    for key, raw in groups_raw.items():
        email = _norm_email(key)
        if "@" not in email:
            raise PolicyError(
                f"{source}: group key '{key}' must be the group's email address"
            )
        if email in groups:
            raise PolicyError(f"{source}: duplicate group '{email}'")
        groups[email] = _rule_from_mapping(
            email, raw, catalogue, where=f"{source}: groups[{email}]"
        )

    default = _rule_from_mapping(
        "default", data.get("default"), catalogue, where=f"{source}: default"
    )
    return GroupPolicy(version=1, groups=groups, default=default, source=source)


def load_policy_file(
    path: Optional[os.PathLike | str] = None,
    *,
    catalogue: Optional[ToolCatalogue] = None,
) -> GroupPolicy:
    resolved = Path(path or os.getenv(FILE_ENV) or DEFAULT_POLICY_PATH)
    if not resolved.exists():
        raise PolicyError(f"policy file not found: {resolved}")
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PolicyError(f"{resolved}: invalid YAML: {exc}") from exc
    return parse_policy(data, source=str(resolved), catalogue=catalogue)


# --- selector expansion ----------------------------------------------------


def _expand(
    selectors: Iterable[str], catalogue: ToolCatalogue, candidates: Set[str]
) -> Set[str]:
    """Expand selectors to concrete tool names, restricted to ``candidates``."""
    out: Set[str] = set()
    for sel in selectors:
        if sel == "*":
            out |= candidates
        elif "." in sel:
            service, _, tail = sel.partition(".")
            names = catalogue.tools_for(service, None if tail == "*" else tail)
            out |= names & candidates
        elif sel in candidates:
            out.add(sel)
    return out


def allowed_tools(
    policy: GroupPolicy,
    groups: Iterable[str],
    candidates: Iterable[str],
    *,
    catalogue: Optional[ToolCatalogue] = None,
) -> FrozenSet[str]:
    """Tools from ``candidates`` a member of ``groups`` may use."""
    catalogue = catalogue or ToolCatalogue()
    cand = set(candidates)
    rules = [policy.default] + [
        policy.groups[g] for g in {_norm_email(g) for g in groups} if g in policy.groups
    ]
    result: Set[str] = set()
    for rule in rules:
        result |= _expand(rule.allow, catalogue, cand) - _expand(
            rule.deny, catalogue, cand
        )
    result |= ALWAYS_ALLOWED_TOOLS & cand
    result -= BLOCKED_TOOLS
    return frozenset(result)


# --- membership sources ----------------------------------------------------


class MembershipSource:
    """Answers "is ``email`` a (direct or nested) member of ``group``?"."""

    name = "abstract"

    async def is_member(self, email: str, group: str) -> bool:  # pragma: no cover
        raise NotImplementedError


class StaticMembershipSource(MembershipSource):
    """Membership from a JSON mapping ``{group: [emails]}``.

    For local development and tests only: it duplicates the source of truth
    the Admin console is supposed to be. The engine logs a warning whenever
    this source is active under ``enforce``.
    """

    name = "static"

    def __init__(self, members: Dict[str, Iterable[str]]):
        self._members = {
            _norm_email(g): {_norm_email(m) for m in (emails or [])}
            for g, emails in (members or {}).items()
        }

    async def is_member(self, email: str, group: str) -> bool:
        return _norm_email(email) in self._members.get(_norm_email(group), set())


class DirectoryMembershipSource(MembershipSource):
    """``members.hasMember`` via a service account.

    Two supported identities:

    * The service account itself, holding a Workspace admin role with the
      Groups > Read privilege (assign it under Admin console > Account >
      Admin roles > the role > "Assign service accounts"). No domain-wide
      delegation needed. This is the recommended shape.
    * Domain-wide delegation impersonating ``subject`` (an admin user). Only
      if the first option is unavailable; the DWD grant in the Admin console
      must list exactly ``LOOKUP_SCOPES``.
    """

    name = "directory"

    def __init__(
        self,
        service_account_info: Dict[str, Any],
        *,
        subject: Optional[str] = None,
        build_service: Optional[Callable[[], Any]] = None,
    ):
        self._info = service_account_info
        self._subject = _norm_email(subject) or None
        self._build_service = build_service or self._default_build
        self._missing_groups_reported: Set[str] = set()

    def _default_build(self):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(
            self._info, scopes=LOOKUP_SCOPES
        )
        if self._subject:
            creds = creds.with_subject(self._subject)
        return build(
            "admin",
            "directory_v1",
            credentials=creds,
            cache_discovery=False,
            static_discovery=True,
        )

    def _has_member_sync(self, email: str, group: str) -> bool:
        from googleapiclient.errors import HttpError

        service = self._build_service()
        try:
            resp = (
                service.members().hasMember(groupKey=group, memberKey=email).execute()
            )
            return bool(resp.get("isMember"))
        except HttpError as error:
            status = getattr(getattr(error, "resp", None), "status", None)
            if status == 404:
                if group not in self._missing_groups_reported:
                    self._missing_groups_reported.add(group)
                    logger.warning(
                        "group policy: group '%s' does not exist in the Directory; "
                        "treating as no members",
                        group,
                    )
                return False
            if status == 400:
                # Google answers 400 "Invalid input" when memberKey is not an
                # entity in the domain (an external address, a typo). That is
                # a definite "not a member", not an outage.
                return False
            raise MembershipLookupError(
                f"Directory hasMember({group}, {email}) failed with HTTP {status}: {error}"
            ) from error
        except Exception as error:  # network, TLS, auth
            raise MembershipLookupError(
                f"Directory hasMember({group}, {email}) failed: {error}"
            ) from error
        finally:
            close = getattr(service, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # pragma: no cover - defensive
                    pass

    async def is_member(self, email: str, group: str) -> bool:
        return await asyncio.to_thread(self._has_member_sync, email, group)


# --- resolver with cache ---------------------------------------------------


@dataclass
class _CacheEntry:
    groups: FrozenSet[str]
    fetched_at: float


class MembershipResolver:
    """Resolves a user's policy-group memberships, with TTL + stale fallback."""

    def __init__(
        self,
        source: MembershipSource,
        policy_groups: Iterable[str],
        *,
        cache_ttl_s: float = DEFAULT_CACHE_TTL_S,
        stale_ttl_s: float = DEFAULT_STALE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.source = source
        self.policy_groups = tuple(sorted({_norm_email(g) for g in policy_groups}))
        self.cache_ttl_s = float(cache_ttl_s)
        self.stale_ttl_s = float(stale_ttl_s)
        self._clock = clock
        self._cache: Dict[str, _CacheEntry] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def invalidate(self, email: Optional[str] = None) -> None:
        if email is None:
            self._cache.clear()
        else:
            self._cache.pop(_norm_email(email), None)

    def _lock_for(self, email: str) -> asyncio.Lock:
        lock = self._locks.get(email)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[email] = lock
        return lock

    async def _lookup(self, email: str) -> FrozenSet[str]:
        if not self.policy_groups:
            return frozenset()
        results = await asyncio.gather(
            *(self.source.is_member(email, g) for g in self.policy_groups)
        )
        return frozenset(g for g, is_in in zip(self.policy_groups, results) if is_in)

    async def groups_for(self, email: str) -> FrozenSet[str]:
        """Policy groups ``email`` belongs to. Raises MembershipLookupError
        when the source fails and no usable (fresh or stale) answer exists."""
        email = _norm_email(email)
        now = self._clock()
        entry = self._cache.get(email)
        if entry is not None and now - entry.fetched_at < self.cache_ttl_s:
            return entry.groups

        async with self._lock_for(email):
            # Re-check: another coroutine may have refreshed while we waited.
            entry = self._cache.get(email)
            now = self._clock()
            if entry is not None and now - entry.fetched_at < self.cache_ttl_s:
                return entry.groups
            try:
                groups = await self._lookup(email)
            except MembershipLookupError as exc:
                if entry is not None and now - entry.fetched_at < self.stale_ttl_s:
                    logger.warning(
                        "group policy: membership lookup for %s failed (%s); "
                        "serving cached groups from %.0fs ago",
                        email,
                        exc,
                        now - entry.fetched_at,
                    )
                    return entry.groups
                raise
            self._cache[email] = _CacheEntry(groups=groups, fetched_at=self._clock())
            return groups


# --- decision --------------------------------------------------------------


@dataclass(frozen=True)
class AccessDecision:
    email: Optional[str]
    groups: FrozenSet[str]
    allowed: FrozenSet[str]
    source: str  # disabled | policy | breakglass | unauthenticated | lookup_failed
    reason: str = ""

    def permits(self, tool_name: str) -> bool:
        return tool_name in self.allowed


@dataclass
class AccessPolicyEngine:
    """Everything the middleware needs, built once from the environment."""

    mode: str
    policy: Optional[GroupPolicy] = None
    resolver: Optional[MembershipResolver] = None
    breakglass: FrozenSet[str] = frozenset()
    catalogue: ToolCatalogue = field(default_factory=ToolCatalogue)
    source_name: str = "none"

    @property
    def enabled(self) -> bool:
        return self.mode == "enforce"

    async def decide(
        self, email: Optional[str], candidates: Iterable[str]
    ) -> AccessDecision:
        cand = frozenset(candidates)
        if not self.enabled:
            return AccessDecision(
                email=_norm_email(email) or None,
                groups=frozenset(),
                allowed=cand,
                source="disabled",
            )
        norm = _norm_email(email)
        if not norm:
            return AccessDecision(
                email=None,
                groups=frozenset(),
                allowed=frozenset(),
                source="unauthenticated",
                reason="no verified identity on the request",
            )
        if norm in self.breakglass:
            logger.warning(
                "group policy: BREAK-GLASS access for %s (full tool set, no lookup)",
                norm,
            )
            return AccessDecision(
                email=norm,
                groups=frozenset(),
                allowed=cand - BLOCKED_TOOLS,
                source="breakglass",
                reason=f"{BREAKGLASS_ENV}",
            )
        if self.policy is None or self.resolver is None:
            logger.error(
                "group policy: enforce mode but no membership source is "
                "configured; denying %s everything except %s",
                norm,
                sorted(ALWAYS_ALLOWED_TOOLS),
            )
            return AccessDecision(
                email=norm,
                groups=frozenset(),
                allowed=ALWAYS_ALLOWED_TOOLS & cand,
                source="lookup_failed",
                reason="no membership source configured",
            )
        try:
            groups = await self.resolver.groups_for(norm)
        except MembershipLookupError as exc:
            logger.error(
                "group policy: membership lookup failed for %s and no cached "
                "answer is usable; failing closed: %s",
                norm,
                exc,
            )
            return AccessDecision(
                email=norm,
                groups=frozenset(),
                allowed=ALWAYS_ALLOWED_TOOLS & cand,
                source="lookup_failed",
                reason=str(exc),
            )
        allowed = allowed_tools(self.policy, groups, cand, catalogue=self.catalogue)
        return AccessDecision(
            email=norm, groups=groups, allowed=allowed, source="policy"
        )

    # -- construction --------------------------------------------------------

    @classmethod
    def disabled(cls, reason: str = "") -> "AccessPolicyEngine":
        return cls(mode="off", source_name=reason or "none")

    @classmethod
    def from_env(cls, environ: Optional[Dict[str, str]] = None) -> "AccessPolicyEngine":
        env = os.environ if environ is None else environ
        mode = (env.get(MODE_ENV) or "off").strip().lower()
        if mode not in ("off", "enforce"):
            raise PolicyError(f"{MODE_ENV} must be 'off' or 'enforce', got {mode!r}")

        breakglass = frozenset(
            _norm_email(e)
            for e in (env.get(BREAKGLASS_ENV) or "").split(",")
            if _norm_email(e)
        )

        if mode == "off":
            logger.info(
                "group policy: %s=off; every authenticated user gets every "
                "registered tool (single-user behaviour)",
                MODE_ENV,
            )
            return cls(mode="off", breakglass=breakglass, source_name="none")

        catalogue = ToolCatalogue()
        policy = load_policy_file(env.get(FILE_ENV), catalogue=catalogue)
        # Validate the tunables up front so a typo fails at startup rather
        # than the first time a membership source happens to be configured.
        cache_ttl_s = _float_env(env, CACHE_TTL_ENV, DEFAULT_CACHE_TTL_S)
        stale_ttl_s = _float_env(env, STALE_TTL_ENV, DEFAULT_STALE_TTL_S)

        source: Optional[MembershipSource] = None
        source_name = "none"
        sa_info = _load_service_account_info(env)
        if sa_info is not None:
            source = DirectoryMembershipSource(
                sa_info, subject=env.get(SUBJECT_ENV) or None
            )
            source_name = "directory"
        elif env.get(STATIC_MEMBERS_ENV):
            try:
                members = json.loads(env[STATIC_MEMBERS_ENV])
            except json.JSONDecodeError as exc:
                raise PolicyError(
                    f"{STATIC_MEMBERS_ENV} is not valid JSON: {exc}"
                ) from exc
            if not isinstance(members, dict):
                raise PolicyError(f"{STATIC_MEMBERS_ENV} must be a JSON object")
            source = StaticMembershipSource(members)
            source_name = "static"
            logger.warning(
                "group policy: using STATIC membership from %s; this duplicates "
                "the Admin console and is meant for development only",
                STATIC_MEMBERS_ENV,
            )

        resolver: Optional[MembershipResolver] = None
        if source is not None:
            resolver = MembershipResolver(
                source,
                policy.group_emails,
                cache_ttl_s=cache_ttl_s,
                stale_ttl_s=stale_ttl_s,
            )
            logger.info(
                "group policy: ENFORCING %s (%d groups, membership via %s, "
                "cache %ss, stale %ss, break-glass %d)",
                policy.source,
                len(policy.groups),
                source_name,
                resolver.cache_ttl_s,
                resolver.stale_ttl_s,
                len(breakglass),
            )
        else:
            logger.error(
                "group policy: %s=enforce but no membership source is set (%s / "
                "%s / %s). Every user except break-glass accounts will be "
                "denied all tools.",
                MODE_ENV,
                SA_JSON_FILE_ENV,
                SA_JSON_B64_ENV,
                STATIC_MEMBERS_ENV,
            )

        return cls(
            mode="enforce",
            policy=policy,
            resolver=resolver,
            breakglass=breakglass,
            catalogue=catalogue,
            source_name=source_name,
        )


def _float_env(env: Dict[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise PolicyError(f"{key} must be a number, got {raw!r}") from exc
    if value < 0:
        raise PolicyError(f"{key} must be >= 0")
    return value


def _load_service_account_info(env: Dict[str, str]) -> Optional[Dict[str, Any]]:
    path = (env.get(SA_JSON_FILE_ENV) or "").strip()
    raw: Optional[str] = None
    if path:
        p = Path(path).expanduser()
        if not p.exists():
            raise PolicyError(f"{SA_JSON_FILE_ENV} points to a missing file: {p}")
        raw = p.read_text(encoding="utf-8")
    elif (env.get(SA_JSON_B64_ENV) or "").strip():
        try:
            raw = base64.b64decode(env[SA_JSON_B64_ENV].strip()).decode("utf-8")
        except Exception as exc:
            raise PolicyError(f"{SA_JSON_B64_ENV} is not valid base64") from exc
    if raw is None:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PolicyError("service account JSON is not valid JSON") from exc
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise PolicyError("service account JSON must be a Google service_account key")
    return info


# --- process-wide engine ---------------------------------------------------

_engine: Optional[AccessPolicyEngine] = None


def get_engine() -> AccessPolicyEngine:
    """Lazily build the engine from the environment (once per process)."""
    global _engine
    if _engine is None:
        _engine = AccessPolicyEngine.from_env()
    return _engine


def set_engine(engine: Optional[AccessPolicyEngine]) -> None:
    """Replace (or clear, with ``None``) the process-wide engine. Tests."""
    global _engine
    _engine = engine


def is_policy_group(email: str) -> bool:
    """True when ``email`` is a group named in the loaded policy.

    Used by the ``gadmin_write`` tools to refuse membership edits on the
    groups that decide MCP access: those are managed in the Admin console
    only, so a user who can add group members cannot add themselves to the
    admin group through this server.
    """
    try:
        engine = get_engine()
    except PolicyError as exc:
        logger.error("group policy: cannot load policy to check %s: %s", email, exc)
        # Fail closed for the guard: an unloadable policy must not become a
        # way to edit policy groups.
        return True
    policy = engine.policy
    if policy is None:
        # Mode off: still protect the groups named in the shipped file so the
        # guard holds before enforcement is switched on.
        try:
            policy = load_policy_file(catalogue=engine.catalogue)
        except PolicyError:
            return False
    return policy.is_policy_group(email)

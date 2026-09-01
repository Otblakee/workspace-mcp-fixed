"""Tests for the group-based tool access policy (core/access_policy.py).

Covers the policy model (selector grammar, validation, union/deny semantics),
the membership resolver (TTL cache, stale fallback, coalesced lookups), the
Directory source's HTTP-status handling, the decision engine's fail-closed
behaviour, environment construction, and the shipped core/group_policy.yaml.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from googleapiclient.errors import HttpError  # noqa: E402

from core import access_policy as ap  # noqa: E402
from core.tool_policy import BLOCKED_TOOLS  # noqa: E402

ADMINS = "mcp-admins@otbgroup.co.uk"
MANAGERS = "mcp-managers@otbgroup.co.uk"
STAFF = "mcp-staff@otbgroup.co.uk"


def _http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    return HttpError(resp, b'{"error": {"message": "x"}}')


def _policy(data: dict) -> ap.GroupPolicy:
    return ap.parse_policy(data, source="<test>")


# ---------------------------------------------------------------------------
# Catalogue + selector grammar
# ---------------------------------------------------------------------------


class TestCatalogue:
    def test_tier_is_cumulative(self):
        cat = ap.ToolCatalogue()
        core = cat.tools_for("gmail", "core")
        extended = cat.tools_for("gmail", "extended")
        everything = cat.tools_for("gmail")
        assert "search_gmail_messages" in core
        assert core < extended <= everything
        assert "draft_gmail_message" in extended and "draft_gmail_message" not in core

    def test_unknown_service_or_tier_rejected(self):
        cat = ap.ToolCatalogue()
        with pytest.raises(ap.PolicyError):
            cat.tools_for("nope")
        with pytest.raises(ap.PolicyError):
            cat.tools_for("gmail", "ultra")


class TestParsePolicy:
    def test_minimal_valid(self):
        p = _policy({"version": 1, "groups": {ADMINS: {"allow": ["*"]}}})
        assert p.groups[ADMINS].allow == ("*",)
        assert p.default.allow == ()
        assert p.is_policy_group("MCP-Admins@OTBGroup.co.uk")

    @pytest.mark.parametrize(
        "bad",
        [
            {"version": 2},
            {"groups": {"not-an-email": {"allow": ["*"]}}},
            {"groups": {ADMINS: {"allow": ["no_such_tool"]}}},
            {"groups": {ADMINS: {"allow": ["gmail.ultra"]}}},
            {"groups": {ADMINS: {"allow": ["nosuchservice.*"]}}},
            {"groups": {ADMINS: {"allow": ["transfer_drive_ownership"]}}},
            {"groups": {ADMINS: {"deny": ["delete_event"]}}},
            {"groups": {ADMINS: {"permit": ["*"]}}},
            {"groups": {ADMINS: {"allow": "gmail.core", "extra": 1}}},
            {"groups": {ADMINS: {"allow": [""]}}},
            {"groups": {ADMINS: {"allow": [42]}}},
            {"groups": {ADMINS: "gmail.core"}},
            {"groups": [ADMINS]},
            {"bogus": 1},
            [],
        ],
    )
    def test_invalid_policies_fail_to_load(self, bad):
        with pytest.raises(ap.PolicyError):
            _policy(bad)

    def test_selector_string_is_accepted_as_single_item(self):
        p = _policy({"groups": {STAFF: {"allow": "gmail.core"}}})
        assert p.groups[STAFF].allow == ("gmail.core",)

    def test_always_allowed_tool_may_be_named(self):
        p = _policy({"groups": {STAFF: {"allow": ["get_my_access"]}}})
        assert p.groups[STAFF].allow == ("get_my_access",)

    def test_duplicate_group_after_normalisation_rejected(self):
        with pytest.raises(ap.PolicyError, match="duplicate"):
            _policy({"groups": {STAFF: {}, STAFF.upper(): {}}})


# ---------------------------------------------------------------------------
# allowed_tools semantics
# ---------------------------------------------------------------------------


class TestAllowedTools:
    CANDIDATES = {
        "search_gmail_messages",
        "send_gmail_message",
        "draft_gmail_message",
        "search_drive_files",
        "create_shared_drive",
        "get_my_access",
        "transfer_drive_ownership",  # blocked; must never appear
    }

    def test_no_groups_gets_default_plus_always_allowed(self):
        p = _policy({"default": {"allow": ["search_drive_files"]}})
        out = ap.allowed_tools(p, [], self.CANDIDATES)
        assert out == {"search_drive_files", "get_my_access"}

    def test_no_groups_no_default_is_always_allowed_only(self):
        p = _policy({})
        assert ap.allowed_tools(p, [], self.CANDIDATES) == {"get_my_access"}

    def test_star_never_includes_blocked_tools(self):
        p = _policy({"groups": {ADMINS: {"allow": ["*"]}}})
        out = ap.allowed_tools(p, [ADMINS], self.CANDIDATES)
        assert "transfer_drive_ownership" not in out
        assert out == self.CANDIDATES - BLOCKED_TOOLS

    def test_union_across_groups(self):
        p = _policy(
            {
                "groups": {
                    STAFF: {"allow": ["search_gmail_messages"]},
                    MANAGERS: {"allow": ["send_gmail_message"]},
                }
            }
        )
        out = ap.allowed_tools(p, [STAFF, MANAGERS], self.CANDIDATES)
        assert {"search_gmail_messages", "send_gmail_message"} <= out

    def test_deny_only_subtracts_from_its_own_group(self):
        p = _policy(
            {
                "groups": {
                    STAFF: {"allow": ["gmail.core"], "deny": ["send_gmail_message"]},
                    MANAGERS: {"allow": ["send_gmail_message"]},
                }
            }
        )
        staff_only = ap.allowed_tools(p, [STAFF], self.CANDIDATES)
        assert "send_gmail_message" not in staff_only
        assert "search_gmail_messages" in staff_only
        both = ap.allowed_tools(p, [STAFF, MANAGERS], self.CANDIDATES)
        assert "send_gmail_message" in both

    def test_service_wildcard_and_tier(self):
        p = _policy({"groups": {STAFF: {"allow": ["drive.core"]}}})
        out = ap.allowed_tools(p, [STAFF], self.CANDIDATES)
        assert "search_drive_files" in out
        assert "create_shared_drive" not in out  # extended tier
        p2 = _policy({"groups": {STAFF: {"allow": ["drive.*"]}}})
        out2 = ap.allowed_tools(p2, [STAFF], self.CANDIDATES)
        assert "create_shared_drive" in out2

    def test_unknown_group_membership_is_ignored(self):
        p = _policy({"groups": {STAFF: {"allow": ["gmail.core"]}}})
        out = ap.allowed_tools(p, ["someone-else@otbgroup.co.uk"], self.CANDIDATES)
        assert out == {"get_my_access"}

    def test_result_is_subset_of_candidates(self):
        p = _policy({"groups": {ADMINS: {"allow": ["*"]}}})
        out = ap.allowed_tools(p, [ADMINS], {"search_gmail_messages"})
        assert out == {"search_gmail_messages"}


# ---------------------------------------------------------------------------
# Membership sources
# ---------------------------------------------------------------------------


class TestStaticSource:
    @pytest.mark.asyncio
    async def test_case_insensitive(self):
        src = ap.StaticMembershipSource({STAFF.upper(): ["Katie@OTBGroup.co.uk"]})
        assert await src.is_member("katie@otbgroup.co.uk", STAFF)
        assert not await src.is_member("other@otbgroup.co.uk", STAFF)
        assert not await src.is_member("katie@otbgroup.co.uk", ADMINS)


class _FakeDirectory:
    def __init__(self, outcome):
        self.outcome = outcome
        self.closed = False
        self.calls = []

    def members(self):
        outer = self

        class _Members:
            def hasMember(self, groupKey, memberKey):
                outer.calls.append((groupKey, memberKey))
                req = MagicMock()
                if isinstance(outer.outcome, Exception):
                    req.execute.side_effect = outer.outcome
                else:
                    req.execute.return_value = {"isMember": outer.outcome}
                return req

        return _Members()

    def close(self):
        self.closed = True


class TestDirectorySource:
    def _source(self, outcome):
        directory = _FakeDirectory(outcome)
        src = ap.DirectoryMembershipSource(
            {"type": "service_account"}, build_service=lambda: directory
        )
        return src, directory

    @pytest.mark.asyncio
    async def test_member_true_and_false(self):
        src, d = self._source(True)
        assert await src.is_member("a@otbgroup.co.uk", STAFF) is True
        assert d.calls == [(STAFF, "a@otbgroup.co.uk")]
        assert d.closed
        src, _ = self._source(False)
        assert await src.is_member("a@otbgroup.co.uk", STAFF) is False

    @pytest.mark.asyncio
    async def test_404_missing_group_is_not_member_and_warns_once(self, caplog):
        src, _ = self._source(_http_error(404))
        with caplog.at_level("WARNING", logger="core.access_policy"):
            assert await src.is_member("a@otbgroup.co.uk", STAFF) is False
            assert await src.is_member("b@otbgroup.co.uk", STAFF) is False
        warnings = [r for r in caplog.records if "does not exist" in r.message]
        assert len(warnings) == 1

    @pytest.mark.asyncio
    async def test_400_invalid_input_is_not_member(self):
        src, _ = self._source(_http_error(400))
        assert await src.is_member("outsider@example.com", STAFF) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
    async def test_privilege_and_server_errors_raise(self, status):
        src, d = self._source(_http_error(status))
        with pytest.raises(ap.MembershipLookupError):
            await src.is_member("a@otbgroup.co.uk", STAFF)
        assert d.closed

    @pytest.mark.asyncio
    async def test_network_error_raises(self):
        src, _ = self._source(ConnectionError("dns"))
        with pytest.raises(ap.MembershipLookupError):
            await src.is_member("a@otbgroup.co.uk", STAFF)

    def test_default_build_uses_member_readonly_scope_and_subject(self, monkeypatch):
        from google.oauth2 import service_account
        import googleapiclient.discovery as discovery

        captured = {}
        creds = MagicMock()
        creds.with_subject.return_value = "delegated-creds"

        def fake_from_info(info, scopes=None):
            captured["info"] = info
            captured["scopes"] = scopes
            return creds

        def fake_build(name, version, **kwargs):
            captured["build"] = (name, version, kwargs)
            return "service"

        monkeypatch.setattr(
            service_account.Credentials, "from_service_account_info", fake_from_info
        )
        monkeypatch.setattr(discovery, "build", fake_build)

        src = ap.DirectoryMembershipSource(
            {"type": "service_account", "client_email": "sa@x.iam"},
            subject="Oliver@OTBGroup.co.uk",
        )
        assert src._default_build() == "service"
        assert captured["scopes"] == [ap.DIRECTORY_MEMBER_READ_SCOPE]
        creds.with_subject.assert_called_once_with("oliver@otbgroup.co.uk")
        name, version, kwargs = captured["build"]
        assert (name, version) == ("admin", "directory_v1")
        assert kwargs["credentials"] == "delegated-creds"
        assert kwargs["static_discovery"] is True

        # No subject -> service account's own identity, no impersonation.
        creds.with_subject.reset_mock()
        ap.DirectoryMembershipSource({"type": "service_account"})._default_build()
        creds.with_subject.assert_not_called()


# ---------------------------------------------------------------------------
# Resolver: cache, stale fallback, coalescing
# ---------------------------------------------------------------------------


class _CountingSource(ap.MembershipSource):
    def __init__(self, answers=None, error=None, delay=0.0):
        self.answers = answers or {}
        self.error = error
        self.delay = delay
        self.calls = 0

    async def is_member(self, email, group):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return group in self.answers.get(email, set())


class TestResolver:
    @pytest.mark.asyncio
    async def test_caches_within_ttl_and_refetches_after(self):
        now = [1000.0]
        src = _CountingSource({"a@otbgroup.co.uk": {STAFF}})
        r = ap.MembershipResolver(
            src,
            [STAFF, ADMINS],
            cache_ttl_s=300,
            stale_ttl_s=3600,
            clock=lambda: now[0],
        )
        assert await r.groups_for("A@OTBGroup.co.uk") == {STAFF}
        assert src.calls == 2  # one hasMember per policy group
        assert await r.groups_for("a@otbgroup.co.uk") == {STAFF}
        assert src.calls == 2  # served from cache
        now[0] += 301
        assert await r.groups_for("a@otbgroup.co.uk") == {STAFF}
        assert src.calls == 4

    @pytest.mark.asyncio
    async def test_stale_served_on_error_within_stale_ttl_then_raises(self, caplog):
        now = [0.0]
        src = _CountingSource({"a@otbgroup.co.uk": {STAFF}})
        r = ap.MembershipResolver(
            src, [STAFF], cache_ttl_s=10, stale_ttl_s=100, clock=lambda: now[0]
        )
        assert await r.groups_for("a@otbgroup.co.uk") == {STAFF}
        src.error = ap.MembershipLookupError("directory down")
        now[0] = 50
        with caplog.at_level("WARNING", logger="core.access_policy"):
            assert await r.groups_for("a@otbgroup.co.uk") == {STAFF}
        assert any("serving cached groups" in rec.message for rec in caplog.records)
        now[0] = 150
        with pytest.raises(ap.MembershipLookupError):
            await r.groups_for("a@otbgroup.co.uk")

    @pytest.mark.asyncio
    async def test_error_with_no_cache_raises(self):
        src = _CountingSource(error=ap.MembershipLookupError("403"))
        r = ap.MembershipResolver(src, [STAFF])
        with pytest.raises(ap.MembershipLookupError):
            await r.groups_for("a@otbgroup.co.uk")

    @pytest.mark.asyncio
    async def test_concurrent_lookups_for_same_user_are_coalesced(self):
        src = _CountingSource({"a@otbgroup.co.uk": {STAFF}}, delay=0.01)
        r = ap.MembershipResolver(src, [STAFF])
        results = await asyncio.gather(
            *(r.groups_for("a@otbgroup.co.uk") for _ in range(5))
        )
        assert all(res == {STAFF} for res in results)
        assert src.calls == 1

    @pytest.mark.asyncio
    async def test_invalidate(self):
        src = _CountingSource({"a@otbgroup.co.uk": {STAFF}})
        r = ap.MembershipResolver(src, [STAFF])
        await r.groups_for("a@otbgroup.co.uk")
        r.invalidate("a@otbgroup.co.uk")
        await r.groups_for("a@otbgroup.co.uk")
        assert src.calls == 2

    @pytest.mark.asyncio
    async def test_no_policy_groups_means_no_lookup(self):
        src = _CountingSource()
        r = ap.MembershipResolver(src, [])
        assert await r.groups_for("a@otbgroup.co.uk") == frozenset()
        assert src.calls == 0


# ---------------------------------------------------------------------------
# Engine decisions
# ---------------------------------------------------------------------------


def _engine(policy=None, source=None, breakglass=(), mode="enforce"):
    policy = policy or _policy(
        {
            "groups": {
                ADMINS: {"allow": ["*"]},
                STAFF: {"allow": ["gmail.core"], "deny": ["send_gmail_message"]},
            }
        }
    )
    resolver = (
        ap.MembershipResolver(source, policy.group_emails)
        if source is not None
        else None
    )
    return ap.AccessPolicyEngine(
        mode=mode,
        policy=policy,
        resolver=resolver,
        breakglass=frozenset(breakglass),
        source_name="test",
    )


CANDS = {
    "search_gmail_messages",
    "send_gmail_message",
    "create_shared_drive",
    "get_my_access",
}


class TestEngineDecide:
    @pytest.mark.asyncio
    async def test_mode_off_allows_everything(self):
        eng = _engine(mode="off")
        d = await eng.decide("anyone@otbgroup.co.uk", CANDS)
        assert d.source == "disabled" and d.allowed == CANDS
        assert not eng.enabled

    @pytest.mark.asyncio
    async def test_unauthenticated_gets_nothing(self):
        eng = _engine(source=_CountingSource())
        for email in (None, "", "   "):
            d = await eng.decide(email, CANDS)
            assert d.source == "unauthenticated" and d.allowed == frozenset()
            assert not d.permits("get_my_access")

    @pytest.mark.asyncio
    async def test_staff_member(self):
        src = _CountingSource({"k@otbgroup.co.uk": {STAFF}})
        d = await _engine(source=src).decide("K@otbgroup.co.uk", CANDS)
        assert d.source == "policy"
        assert d.groups == {STAFF}
        assert d.allowed == {"search_gmail_messages", "get_my_access"}

    @pytest.mark.asyncio
    async def test_admin_member(self):
        src = _CountingSource({"o@otbgroup.co.uk": {ADMINS}})
        d = await _engine(source=src).decide("o@otbgroup.co.uk", CANDS)
        assert d.allowed == CANDS

    @pytest.mark.asyncio
    async def test_no_group_gets_only_always_allowed(self):
        src = _CountingSource({})
        d = await _engine(source=src).decide("nobody@otbgroup.co.uk", CANDS)
        assert d.source == "policy" and d.allowed == {"get_my_access"}

    @pytest.mark.asyncio
    async def test_breakglass_bypasses_lookup(self, caplog):
        src = _CountingSource(error=ap.MembershipLookupError("down"))
        eng = _engine(source=src, breakglass=["oliver@otbgroup.co.uk"])
        with caplog.at_level("WARNING", logger="core.access_policy"):
            d = await eng.decide("Oliver@OTBGroup.co.uk", CANDS | {"delete_event"})
        assert d.source == "breakglass"
        assert d.allowed == CANDS  # blocked tools still excluded
        assert src.calls == 0
        assert any("BREAK-GLASS" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_lookup_failure_fails_closed(self, caplog):
        src = _CountingSource(error=ap.MembershipLookupError("403"))
        with caplog.at_level("ERROR", logger="core.access_policy"):
            d = await _engine(source=src).decide("k@otbgroup.co.uk", CANDS)
        assert d.source == "lookup_failed"
        assert d.allowed == {"get_my_access"}
        assert any("failing closed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_source_configured_fails_closed(self):
        d = await _engine(source=None).decide("k@otbgroup.co.uk", CANDS)
        assert d.source == "lookup_failed" and d.allowed == {"get_my_access"}


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


SA_KEY = {"type": "service_account", "client_email": "policy-reader@x.iam"}


class TestFromEnv:
    def test_default_is_off(self):
        eng = ap.AccessPolicyEngine.from_env({})
        assert not eng.enabled and eng.policy is None

    def test_invalid_mode_rejected(self):
        with pytest.raises(ap.PolicyError):
            ap.AccessPolicyEngine.from_env({ap.MODE_ENV: "maybe"})

    def test_enforce_with_static_members(self, caplog):
        env = {
            ap.MODE_ENV: "enforce",
            ap.STATIC_MEMBERS_ENV: json.dumps({STAFF: ["k@otbgroup.co.uk"]}),
            ap.BREAKGLASS_ENV: " Oliver@OTBGroup.co.uk , ",
            ap.CACHE_TTL_ENV: "60",
            ap.STALE_TTL_ENV: "120",
        }
        with caplog.at_level("WARNING", logger="core.access_policy"):
            eng = ap.AccessPolicyEngine.from_env(env)
        assert eng.enabled and eng.source_name == "static"
        assert eng.breakglass == {"oliver@otbgroup.co.uk"}
        assert eng.resolver.cache_ttl_s == 60 and eng.resolver.stale_ttl_s == 120
        assert eng.policy.is_policy_group(STAFF)
        assert any("STATIC membership" in r.message for r in caplog.records)

    def test_enforce_with_service_account_b64_prefers_directory(self):
        env = {
            ap.MODE_ENV: "enforce",
            ap.SA_JSON_B64_ENV: base64.b64encode(json.dumps(SA_KEY).encode()).decode(),
            ap.SUBJECT_ENV: "oliver@otbgroup.co.uk",
            ap.STATIC_MEMBERS_ENV: "{}",  # ignored when a service account exists
        }
        eng = ap.AccessPolicyEngine.from_env(env)
        assert eng.source_name == "directory"
        assert isinstance(eng.resolver.source, ap.DirectoryMembershipSource)
        assert eng.resolver.source._subject == "oliver@otbgroup.co.uk"

    def test_enforce_with_service_account_file(self, tmp_path):
        key = tmp_path / "sa.json"
        key.write_text(json.dumps(SA_KEY))
        eng = ap.AccessPolicyEngine.from_env(
            {ap.MODE_ENV: "enforce", ap.SA_JSON_FILE_ENV: str(key)}
        )
        assert eng.source_name == "directory"
        assert eng.resolver.source._subject is None

    def test_enforce_without_source_logs_error_and_has_no_resolver(self, caplog):
        with caplog.at_level("ERROR", logger="core.access_policy"):
            eng = ap.AccessPolicyEngine.from_env({ap.MODE_ENV: "enforce"})
        assert eng.enabled and eng.resolver is None
        assert any("no membership source" in r.message for r in caplog.records)

    @pytest.mark.parametrize(
        "extra",
        [
            {ap.SA_JSON_FILE_ENV: "/nonexistent/sa.json"},
            {ap.SA_JSON_B64_ENV: "!!not-base64!!"},
            {ap.SA_JSON_B64_ENV: base64.b64encode(b"{not json").decode()},
            {ap.SA_JSON_B64_ENV: base64.b64encode(b'{"type": "user"}').decode()},
            {ap.STATIC_MEMBERS_ENV: "{bad json"},
            {ap.STATIC_MEMBERS_ENV: "[]"},
            {ap.CACHE_TTL_ENV: "soon"},
            {ap.STALE_TTL_ENV: "-1"},
            {ap.FILE_ENV: "/nonexistent/policy.yaml"},
        ],
    )
    def test_bad_configuration_raises(self, extra):
        with pytest.raises(ap.PolicyError):
            ap.AccessPolicyEngine.from_env({ap.MODE_ENV: "enforce", **extra})

    def test_custom_policy_file(self, tmp_path):
        f = tmp_path / "p.yaml"
        f.write_text(f"groups:\n  {STAFF}:\n    allow: [gmail.core]\n")
        eng = ap.AccessPolicyEngine.from_env(
            {ap.MODE_ENV: "enforce", ap.FILE_ENV: str(f), ap.STATIC_MEMBERS_ENV: "{}"}
        )
        assert set(eng.policy.groups) == {STAFF}


# ---------------------------------------------------------------------------
# is_policy_group + process-wide engine
# ---------------------------------------------------------------------------


class TestIsPolicyGroup:
    def test_uses_shipped_file_when_mode_off(self, monkeypatch):
        monkeypatch.delenv(ap.MODE_ENV, raising=False)
        ap.set_engine(None)
        try:
            assert ap.is_policy_group("MCP-ADMINS@otbgroup.co.uk")
            assert not ap.is_policy_group("drivers@otbgroup.co.uk")
        finally:
            ap.set_engine(None)

    def test_uses_loaded_policy_when_enforcing(self):
        p = _policy({"groups": {"ops-acl@otbgroup.co.uk": {"allow": ["*"]}}})
        ap.set_engine(ap.AccessPolicyEngine(mode="enforce", policy=p))
        try:
            assert ap.is_policy_group("ops-acl@otbgroup.co.uk")
            assert not ap.is_policy_group(ADMINS)
        finally:
            ap.set_engine(None)

    def test_unloadable_policy_fails_closed(self, monkeypatch):
        monkeypatch.setenv(ap.MODE_ENV, "enforce")
        monkeypatch.setenv(ap.FILE_ENV, "/nonexistent/policy.yaml")
        ap.set_engine(None)
        try:
            assert ap.is_policy_group("anything@otbgroup.co.uk") is True
        finally:
            ap.set_engine(None)


# ---------------------------------------------------------------------------
# The shipped policy file
# ---------------------------------------------------------------------------


class TestShippedPolicy:
    def test_loads_and_names_expected_groups(self):
        p = ap.load_policy_file()
        assert {ADMINS, MANAGERS, STAFF} <= set(p.groups)
        assert p.default.allow == ()

    def test_staff_cannot_send_share_or_delete(self):
        p = ap.load_policy_file()
        cat = ap.ToolCatalogue()
        staff = ap.allowed_tools(p, [STAFF], cat.all_tools(), catalogue=cat)
        for tool in (
            "send_gmail_message",
            "create_gmail_filter",
            "modify_gmail_message_labels",
            "manage_gmail_label",
            "share_calendar",
            "get_drive_shareable_link",
            "set_drive_permission",
            "revoke_drive_permission",
            "soft_delete_drive_file",
            "create_shared_drive",
            "update_drive_file",
            "list_users",
            "create_group",
        ):
            assert tool not in staff, tool
        for tool in (
            "search_gmail_messages",
            "draft_gmail_message",
            "search_drive_files",
        ):
            assert tool in staff, tool

    def test_managers_can_send_but_not_administer(self):
        p = ap.load_policy_file()
        cat = ap.ToolCatalogue()
        managers = ap.allowed_tools(p, [MANAGERS], cat.all_tools(), catalogue=cat)
        assert "send_gmail_message" in managers
        assert "create_gmail_filter" not in managers
        for tool in (
            "create_shared_drive",
            "set_drive_permission",
            "list_users",
            "walk_drive",
        ):
            assert tool not in managers, tool

    def test_admins_get_everything_registered(self):
        p = ap.load_policy_file()
        cat = ap.ToolCatalogue()
        admins = ap.allowed_tools(p, [ADMINS], cat.all_tools(), catalogue=cat)
        assert admins == frozenset(cat.all_tools()) - BLOCKED_TOOLS

    def test_no_blocked_tool_is_referenced(self):
        p = ap.load_policy_file()
        for rule in [p.default, *p.groups.values()]:
            for sel in rule.allow + rule.deny:
                assert sel not in BLOCKED_TOOLS

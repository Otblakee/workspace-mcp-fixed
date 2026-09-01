"""Group capabilities (parameter-level permissions), the tool guards that
consult them, filter-action validation, and per-user rate caps."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import access_policy as ap  # noqa: E402
from core.utils import UserInputError  # noqa: E402

ADMINS = "mcp-admins@otbgroup.co.uk"
MANAGERS = "mcp-managers@otbgroup.co.uk"
STAFF = "mcp-staff@otbgroup.co.uk"


def _unwrap(fn):
    fn = fn.fn if hasattr(fn, "fn") else fn
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


class _Source(ap.MembershipSource):
    def __init__(self, members):
        self.members = members

    async def is_member(self, email, group):
        return group in self.members.get(email, set())


POLICY = {
    "groups": {
        ADMINS: {
            "allow": ["*"],
            "capabilities": ["url_fetch", "external_share", "external_recipients"],
        },
        MANAGERS: {
            "allow": ["gmail.extended"],
            "capabilities": ["external_recipients"],
        },
        STAFF: {"allow": ["gmail.core"]},
    }
}


def _engine(members, mode="enforce", breakglass=()):
    policy = ap.parse_policy(POLICY, source="<t>")
    return ap.AccessPolicyEngine(
        mode=mode,
        policy=policy,
        resolver=ap.MembershipResolver(_Source(members), policy.group_emails),
        breakglass=frozenset(breakglass),
    )


@pytest.fixture
def internal_domain(monkeypatch):
    monkeypatch.setenv("OAUTH_ALLOWED_EMAIL_DOMAINS", "otbgroup.co.uk")


@pytest.fixture
def as_user(monkeypatch):
    """Install an enforce-mode engine and a fake caller identity."""

    def _install(email, members):
        ap.set_engine(_engine(members))
        monkeypatch.setattr(ap, "current_user_email", AsyncMock(return_value=email))

    yield _install
    ap.set_engine(None)


class TestPolicyCapabilities:
    def test_parse_and_union(self):
        policy = ap.parse_policy(POLICY, source="<t>")
        assert ap.capabilities_for(policy, [STAFF]) == frozenset()
        assert ap.capabilities_for(policy, [MANAGERS]) == {"external_recipients"}
        assert ap.capabilities_for(policy, [STAFF, ADMINS]) == ap.KNOWN_CAPABILITIES

    def test_unknown_capability_rejected(self):
        with pytest.raises(ap.PolicyError, match="unknown capability"):
            ap.parse_policy(
                {"groups": {STAFF: {"capabilities": ["root"]}}}, source="<t>"
            )

    def test_shipped_policy_capabilities(self):
        p = ap.load_policy_file()
        assert set(p.groups[ADMINS].capabilities) == ap.KNOWN_CAPABILITIES
        assert p.groups[MANAGERS].capabilities == ("external_recipients",)
        assert p.groups[STAFF].capabilities == ()

    @pytest.mark.asyncio
    async def test_engine_capabilities(self):
        eng = _engine({"m@otbgroup.co.uk": {MANAGERS}}, breakglass=["o@otbgroup.co.uk"])
        assert await eng.capabilities("m@otbgroup.co.uk") == {"external_recipients"}
        assert await eng.capabilities("nobody@otbgroup.co.uk") == frozenset()
        assert await eng.capabilities(None) == frozenset()
        assert await eng.capabilities("o@otbgroup.co.uk") == ap.KNOWN_CAPABILITIES
        assert await _engine({}, mode="off").capabilities(None) == ap.KNOWN_CAPABILITIES

    @pytest.mark.asyncio
    async def test_engine_capabilities_fail_closed_on_lookup_error(self):
        class Broken(ap.MembershipSource):
            async def is_member(self, email, group):
                raise ap.MembershipLookupError("403")

        policy = ap.parse_policy(POLICY, source="<t>")
        eng = ap.AccessPolicyEngine(
            mode="enforce",
            policy=policy,
            resolver=ap.MembershipResolver(Broken(), policy.group_emails),
        )
        assert await eng.capabilities("m@otbgroup.co.uk") == frozenset()


class TestExternalAddresses:
    def test_inert_without_domains(self, monkeypatch):
        monkeypatch.delenv("OAUTH_ALLOWED_EMAIL_DOMAINS", raising=False)
        assert ap.external_addresses(["x@evil.example"]) == []

    def test_classification(self, internal_domain):
        assert ap.external_addresses(
            ["Katie@OTBGroup.co.uk", "supplier@example.com", "", None, "bare"]
        ) == ["supplier@example.com", "bare"]


class TestRequireCapability:
    @pytest.mark.asyncio
    async def test_off_mode_grants_everything(self, monkeypatch):
        ap.set_engine(_engine({}, mode="off"))
        try:
            await ap.require_capability("url_fetch", action="x")
        finally:
            ap.set_engine(None)

    @pytest.mark.asyncio
    async def test_enforce_denies_staff_and_allows_admin(self, as_user):
        as_user("s@otbgroup.co.uk", {"s@otbgroup.co.uk": {STAFF}})
        with pytest.raises(ap.CapabilityDenied, match="url_fetch"):
            await ap.require_capability("url_fetch", action="Fetching")
        as_user("a@otbgroup.co.uk", {"a@otbgroup.co.uk": {ADMINS}})
        await ap.require_capability("url_fetch", action="Fetching")

    @pytest.mark.asyncio
    async def test_capability_denied_is_a_user_input_error(self):
        assert issubclass(ap.CapabilityDenied, UserInputError)

    @pytest.mark.asyncio
    async def test_unknown_capability_name_is_a_programming_error(self):
        with pytest.raises(ValueError):
            await ap.caller_has_capability("teleport")


class TestGmailGuards:
    @pytest.mark.asyncio
    async def test_staff_cannot_email_outside(self, internal_domain, as_user):
        from gmail import gmail_tools

        as_user("s@otbgroup.co.uk", {"s@otbgroup.co.uk": {STAFF}})
        with pytest.raises(ap.CapabilityDenied, match="external_recipients"):
            await gmail_tools._require_internal_recipients(
                "Boss <boss@otbgroup.co.uk>, supplier@example.com", None, None
            )
        await gmail_tools._require_internal_recipients(
            "boss@otbgroup.co.uk", "k@otbgroup.co.uk", ""
        )

    @pytest.mark.asyncio
    async def test_manager_can_email_outside(self, internal_domain, as_user):
        from gmail import gmail_tools

        as_user("m@otbgroup.co.uk", {"m@otbgroup.co.uk": {MANAGERS}})
        await gmail_tools._require_internal_recipients(
            "supplier@example.com", None, "bcc@example.org"
        )

    @pytest.mark.asyncio
    async def test_send_tool_checks_recipients_before_building_message(
        self, internal_domain, as_user
    ):
        from gmail import gmail_tools

        as_user("s@otbgroup.co.uk", {"s@otbgroup.co.uk": {STAFF}})
        service = MagicMock()
        with pytest.raises(ap.CapabilityDenied):
            await _unwrap(gmail_tools.send_gmail_message)(
                service, "s@otbgroup.co.uk", to="x@example.com", subject="s", body="b"
            )
        service.users.assert_not_called()

    def test_filter_action_validation(self):
        from gmail.gmail_tools import _validate_filter_action

        with pytest.raises(UserInputError, match="forward"):
            _validate_filter_action({"forward": "attacker@example.com"})
        with pytest.raises(UserInputError, match="TRASH"):
            _validate_filter_action({"addLabelIds": ["Label_1", "trash"]})
        with pytest.raises(UserInputError):
            _validate_filter_action("not-an-object")
        _validate_filter_action(
            {"addLabelIds": ["Label_1"], "removeLabelIds": ["INBOX"]}
        )


class TestCalendarGuards:
    @pytest.mark.asyncio
    async def test_staff_cannot_invite_outside(self, internal_domain, as_user):
        from gcalendar import calendar_tools

        as_user("s@otbgroup.co.uk", {"s@otbgroup.co.uk": {STAFF}})
        with pytest.raises(ap.CapabilityDenied):
            await calendar_tools._require_internal_attendees(
                ["k@otbgroup.co.uk", {"email": "dispatch@partner.example"}]
            )
        await calendar_tools._require_internal_attendees(["k@otbgroup.co.uk"])

    @pytest.mark.asyncio
    async def test_share_calendar_owner_needs_external_share(
        self, internal_domain, as_user
    ):
        from gcalendar import calendar_tools

        as_user("m@otbgroup.co.uk", {"m@otbgroup.co.uk": {MANAGERS}})
        service = MagicMock()
        share = _unwrap(calendar_tools.share_calendar)
        with pytest.raises(ap.CapabilityDenied, match="external_share"):
            await share(
                service,
                "m@otbgroup.co.uk",
                calendar_id="primary",
                share_with_email="k@otbgroup.co.uk",
                role="owner",
            )
        with pytest.raises(ap.CapabilityDenied):
            await share(
                service,
                "m@otbgroup.co.uk",
                calendar_id="primary",
                share_with_email="x@example.com",
                role="reader",
            )
        service.acl.assert_not_called()


class TestDriveGuards:
    @pytest.mark.asyncio
    async def test_set_drive_permission_individual_needs_external_share(
        self, internal_domain, as_user
    ):
        from gdrive import shared_drive_tools

        as_user("m@otbgroup.co.uk", {"m@otbgroup.co.uk": {MANAGERS}})
        service = MagicMock()
        tool = _unwrap(shared_drive_tools.set_drive_permission)
        with pytest.raises(ap.CapabilityDenied):
            await tool(
                service,
                "m@otbgroup.co.uk",
                file_or_drive_id="f",
                principal="k@otbgroup.co.uk",
                role="reader",
                allow_individual=True,
            )
        with pytest.raises(ap.CapabilityDenied):
            await tool(
                service,
                "m@otbgroup.co.uk",
                file_or_drive_id="f",
                principal="ext-group@example.com",
                role="reader",
            )
        service.permissions.assert_not_called()

    @pytest.mark.asyncio
    async def test_url_fetch_guard_on_create_drive_file(self, as_user, monkeypatch):
        from gdrive import drive_tools

        as_user("s@otbgroup.co.uk", {"s@otbgroup.co.uk": {STAFF}})
        monkeypatch.setattr(
            drive_tools, "resolve_folder_id", AsyncMock(return_value="root")
        )
        service = MagicMock()
        with pytest.raises(ap.CapabilityDenied, match="url_fetch"):
            await _unwrap(drive_tools.create_drive_file)(
                service,
                "s@otbgroup.co.uk",
                file_name="x.pdf",
                fileUrl="https://example.com/x.pdf",
            )
        service.files.assert_not_called()

    @pytest.mark.asyncio
    async def test_url_fetch_guard_on_import(self, as_user, monkeypatch):
        from gdrive import drive_tools

        as_user("s@otbgroup.co.uk", {"s@otbgroup.co.uk": {STAFF}})
        monkeypatch.setattr(
            drive_tools, "resolve_folder_id", AsyncMock(return_value="root")
        )
        with pytest.raises(ap.CapabilityDenied, match="url_fetch"):
            await _unwrap(drive_tools.import_to_google_doc)(
                MagicMock(),
                "s@otbgroup.co.uk",
                file_name="doc",
                file_url="https://example.com/a.md",
            )

    @pytest.mark.asyncio
    async def test_destination_guard(self, internal_domain, as_user):
        from gdrive.drive_helpers import assert_internal_destination

        def service_with_owner(owner, drive_id=None):
            service = MagicMock()
            meta = {"id": "F", "owners": [{"emailAddress": owner}]}
            if drive_id:
                meta["driveId"] = drive_id
            service.files.return_value.get.return_value.execute = MagicMock(
                return_value=meta
            )
            return service

        as_user("s@otbgroup.co.uk", {"s@otbgroup.co.uk": {STAFF}})
        with pytest.raises(UserInputError, match="external_share"):
            await assert_internal_destination(
                service_with_owner("attacker@gmail.com"),
                "F",
                action="create_drive_file",
            )
        await assert_internal_destination(
            service_with_owner("k@otbgroup.co.uk"), "F", action="x"
        )
        await assert_internal_destination(
            service_with_owner("attacker@gmail.com", drive_id="D"), "F", action="x"
        )
        await assert_internal_destination(
            service_with_owner("attacker@gmail.com"), "root", action="x"
        )

        as_user("a@otbgroup.co.uk", {"a@otbgroup.co.uk": {ADMINS}})
        await assert_internal_destination(
            service_with_owner("attacker@gmail.com"), "F", action="x"
        )

    @pytest.mark.asyncio
    async def test_destination_guard_inert_without_domains(self, monkeypatch, as_user):
        from gdrive.drive_helpers import assert_internal_destination

        monkeypatch.delenv("OAUTH_ALLOWED_EMAIL_DOMAINS", raising=False)
        as_user("s@otbgroup.co.uk", {"s@otbgroup.co.uk": {STAFF}})
        service = MagicMock()
        await assert_internal_destination(service, "F", action="x")
        service.files.assert_not_called()


class TestRateLimits:
    def test_limiter_window(self):
        from auth.access_policy_middleware import _RateLimiter

        now = [0.0]
        rl = _RateLimiter({"soft_delete_drive_file": (2, 60)}, clock=lambda: now[0])
        assert rl.check("u", "soft_delete_drive_file") is None
        assert rl.check("u", "soft_delete_drive_file") is None
        assert "rate limit" in rl.check("u", "soft_delete_drive_file")
        assert rl.check("other", "soft_delete_drive_file") is None  # per user
        assert rl.check("u", "search_gmail_messages") is None  # uncapped tool
        now[0] = 61
        assert rl.check("u", "soft_delete_drive_file") is None

    def test_env_override(self, monkeypatch):
        from auth.access_policy_middleware import _load_rate_limits

        monkeypatch.setenv(
            "MCP_TOOL_RATE_LIMITS",
            '{"send_gmail_message": [5, 60], "share_calendar": 0}',
        )
        limits = _load_rate_limits()
        assert limits["send_gmail_message"] == (5, 60.0)
        assert "share_calendar" not in limits
        monkeypatch.setenv("MCP_TOOL_RATE_LIMITS", "not json")
        assert _load_rate_limits()["send_gmail_message"] == (30, 600)

    @pytest.mark.asyncio
    async def test_middleware_refuses_over_cap_and_audits(self, monkeypatch):
        from auth import access_policy_middleware as apm
        from core import audit
        from fastmcp.exceptions import AuthorizationError

        fake = MagicMock()
        monkeypatch.setattr(audit, "logger", lambda: fake)
        monkeypatch.setattr(apm, "_transport_is_stdio", lambda ctx: False)
        eng = _engine({"a@otbgroup.co.uk": {ADMINS}})
        mw = apm.AccessPolicyMiddleware(
            eng, rate_limiter=apm._RateLimiter({"send_gmail_message": (1, 600)})
        )

        class Ctx:
            transport = "http"

            async def get_state(self, key):
                return "a@otbgroup.co.uk" if key == "authenticated_user_email" else None

        context = SimpleNamespace(
            fastmcp_context=Ctx(), message=SimpleNamespace(name="send_gmail_message")
        )
        call_next = AsyncMock(return_value="ok")
        assert await mw.on_call_tool(context, call_next) == "ok"
        with pytest.raises(AuthorizationError, match="rate limit"):
            await mw.on_call_tool(context, call_next)
        row = fake.submit.call_args.args[0]
        assert row["status"] == "denied" and "rate limit" in row["error"]

    @pytest.mark.asyncio
    async def test_breakglass_is_exempt(self, monkeypatch):
        from auth import access_policy_middleware as apm

        monkeypatch.setattr(apm, "_transport_is_stdio", lambda ctx: False)
        eng = _engine({}, breakglass=["o@otbgroup.co.uk"])
        mw = apm.AccessPolicyMiddleware(
            eng, rate_limiter=apm._RateLimiter({"send_gmail_message": (1, 600)})
        )

        class Ctx:
            transport = "http"

            async def get_state(self, key):
                return "o@otbgroup.co.uk" if key == "authenticated_user_email" else None

        context = SimpleNamespace(
            fastmcp_context=Ctx(), message=SimpleNamespace(name="send_gmail_message")
        )
        for _ in range(3):
            assert await mw.on_call_tool(context, AsyncMock(return_value="ok")) == "ok"

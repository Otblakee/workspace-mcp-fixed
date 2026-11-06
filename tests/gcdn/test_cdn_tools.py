"""Tests for the opt-in ``gcdn`` service (v1.19.0).

The Cloudflare REST API and the public CDN are stood in for by an
``httpx.MockTransport`` that records every request, so the exact URLs,
headers and bodies the client sends are asserted and nothing touches the
network. The caller gate, the versioned-name rule, the no-overwrite rule,
the verification report and the read-only marker are covered; so is the
wiring (opt-in, tool tiers, no OAuth scope, audit service tag).
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.utils import UserInputError  # noqa: E402
from gcdn import cdn_tools, r2_client  # noqa: E402
from gcdn.r2_client import (  # noqa: E402
    CdnConfigError,
    CdnKeyError,
    CdnSettings,
    R2Client,
    load_settings,
    normalise_etag,
    validate_key,
    verify_public,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OWNER = "oliver@otbgroup.co.uk"
ACCOUNT = "acct123"
BUCKET = "otb-cdn"
TOKEN = "cf-token-SECRET"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
KEY = "bir/bir-logo-black-v1.png"
PUBLIC = f"https://cdn.otbgroup.co.uk/{KEY}"
API_OBJECTS = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/r2/buckets/{BUCKET}/objects"


def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


upload_cdn_asset = _unwrap(cdn_tools.upload_cdn_asset)
list_cdn_assets = _unwrap(cdn_tools.list_cdn_assets)


class FakeCloudflare:
    """Records requests; serves the objects it holds at the API and the CDN."""

    def __init__(self, objects: Optional[Dict[str, bytes]] = None):
        self.objects: Dict[str, bytes] = dict(objects or {})
        self.requests: List[httpx.Request] = []
        self.put_status = 200
        self.put_result: Optional[dict] = None
        self.head_status = 200
        self.head_etag_override: Optional[str] = None
        self.serve_bytes_override: Optional[bytes] = None
        self.list_status = 200

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _item(self, key: str) -> dict:
        data = self.objects[key]
        return {
            "key": key,
            "size": len(data),
            "etag": hashlib.md5(data).hexdigest(),
            "uploaded": "2026-11-06T09:00:00.000Z",
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url).split("?", 1)[0]
        if url.startswith(API_OBJECTS):
            assert request.headers.get("authorization") == f"Bearer {TOKEN}"
            if request.method == "GET" and url == API_OBJECTS:
                if self.list_status != 200:
                    return httpx.Response(
                        self.list_status,
                        json={
                            "success": False,
                            "errors": [
                                {"code": 10000, "message": "Authentication error"}
                            ],
                        },
                    )
                prefix = request.url.params.get("prefix", "")
                items = [
                    self._item(k) for k in sorted(self.objects) if k.startswith(prefix)
                ]
                per_page = int(request.url.params.get("per_page", "1000"))
                cursor = request.url.params.get("cursor")
                start = int(cursor) if cursor else 0
                page = items[start : start + per_page]
                truncated = start + per_page < len(items)
                info = {"per_page": per_page, "is_truncated": truncated}
                if truncated:
                    info["cursor"] = str(start + per_page)
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "errors": [],
                        "result": page,
                        "result_info": info,
                    },
                )
            if request.method == "PUT":
                key = url[len(API_OBJECTS) + 1 :]
                if self.put_status != 200:
                    return httpx.Response(
                        self.put_status,
                        json={
                            "success": False,
                            "errors": [{"code": 10041, "message": "object too large"}],
                        },
                    )
                self.objects[key] = request.content
                result = (
                    self.put_result if self.put_result is not None else self._item(key)
                )
                return httpx.Response(
                    200, json={"success": True, "errors": [], "result": result}
                )
            return httpx.Response(405)
        if url.startswith("https://cdn.otbgroup.co.uk/"):
            assert "authorization" not in request.headers
            key = url[len("https://cdn.otbgroup.co.uk/") :]
            if key not in self.objects or self.head_status != 200:
                return httpx.Response(self.head_status if key in self.objects else 404)
            data = (
                self.serve_bytes_override
                if self.serve_bytes_override is not None
                else self.objects[key]
            )
            etag = self.head_etag_override or f'"{hashlib.md5(data).hexdigest()}"'
            headers = {
                "etag": etag,
                "content-length": str(len(data)),
                "content-type": "image/png",
            }
            if request.method == "HEAD":
                return httpx.Response(200, headers=headers)
            return httpx.Response(200, headers=headers, content=data)
        return httpx.Response(500, text=f"unexpected {request.method} {url}")


@pytest.fixture
def cf(monkeypatch):
    fake = FakeCloudflare()
    monkeypatch.setenv(r2_client.ENV_ACCOUNT_ID, ACCOUNT)
    monkeypatch.setenv(r2_client.ENV_BUCKET, BUCKET)
    monkeypatch.setenv(r2_client.ENV_TOKEN, TOKEN)
    monkeypatch.delenv(r2_client.ENV_PUBLIC_BASE, raising=False)
    monkeypatch.delenv(r2_client.ENV_MAX_BYTES, raising=False)
    monkeypatch.setattr(
        cdn_tools,
        "build_client",
        lambda settings: R2Client(settings, transport=fake.transport()),
    )
    return fake


@pytest.fixture
def as_owner(monkeypatch):
    monkeypatch.delenv(cdn_tools.ENV_ADMIN_EMAILS, raising=False)
    monkeypatch.setattr(
        cdn_tools, "_resolve_caller_email", AsyncMock(return_value=OWNER)
    )
    monkeypatch.setattr(
        cdn_tools, "_resolve_caller_auth_path", AsyncMock(return_value="fastmcp_oauth")
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestValidateKey:
    @pytest.mark.parametrize(
        "key, expected",
        [
            ("bir/bir-logo-black-v1.png", "bir/bir-logo-black-v1.png"),
            ("/otb/otb-logo-shimmer-v2.PNG", "otb/otb-logo-shimmer-v2.PNG"),
            ("logo-v10.svg", "logo-v10.svg"),
            ("a/b/c/name_1.2-v3.webp", "a/b/c/name_1.2-v3.webp"),
        ],
    )
    def test_accepts_versioned_names(self, key, expected):
        assert validate_key(key) == expected

    @pytest.mark.parametrize(
        "key",
        [
            "",
            "bir/bir-logo.png",
            "bir/bir-logo-v.png",
            "bir/bir-logo-v1",
            "bir/logo-version1.png",
            "bir/../otb/x-v1.png",
            "bir//x-v1.png",
            "bir/.hidden-v1.png",
            "bir/my logo-v1.png",
            "bir/lögo-v1.png",
        ],
    )
    def test_refuses_unversioned_or_unsafe_names(self, key):
        with pytest.raises(CdnKeyError):
            validate_key(key)

    def test_message_explains_the_rule(self):
        with pytest.raises(CdnKeyError, match="name-vN.ext"):
            validate_key("bir/bir-logo.png")


class TestSettings:
    def test_missing_vars_are_named_without_the_token(self):
        with pytest.raises(CdnConfigError) as excinfo:
            load_settings({})
        message = str(excinfo.value)
        for name in ("CF_ACCOUNT_ID", "CF_R2_BUCKET", "CF_R2_TOKEN"):
            assert name in message
        assert "Workers R2 Storage: Edit" in message

    def test_defaults_and_overrides(self):
        s = load_settings(
            {"CF_ACCOUNT_ID": "a", "CF_R2_BUCKET": "b", "CF_R2_TOKEN": "t"}
        )
        assert s.public_base == "https://cdn.otbgroup.co.uk"
        assert s.max_bytes == r2_client.DEFAULT_MAX_BYTES
        s = load_settings(
            {
                "CF_ACCOUNT_ID": "a",
                "CF_R2_BUCKET": "b",
                "CF_R2_TOKEN": "t",
                "CDN_PUBLIC_BASE_URL": "https://assets.example.test/",
                "CDN_MAX_UPLOAD_BYTES": "1024",
            }
        )
        assert s.public_base == "https://assets.example.test"
        assert s.max_bytes == 1024

    def test_bad_optional_values_refused(self):
        base = {"CF_ACCOUNT_ID": "a", "CF_R2_BUCKET": "b", "CF_R2_TOKEN": "t"}
        with pytest.raises(CdnConfigError):
            load_settings({**base, "CDN_PUBLIC_BASE_URL": "http://plain.example"})
        with pytest.raises(CdnConfigError):
            load_settings({**base, "CDN_MAX_UPLOAD_BYTES": "lots"})

    def test_urls(self):
        s = CdnSettings("acct", "bkt", "t", "https://cdn.example", 10)
        assert r2_client.object_url(s, "a/b-v1.png") == (
            "https://api.cloudflare.com/client/v4/accounts/acct/r2/buckets/bkt/objects/a/b-v1.png"
        )
        assert r2_client.public_url(s, "a/b-v1.png") == "https://cdn.example/a/b-v1.png"

    def test_normalise_etag(self):
        assert normalise_etag('"ABC"') == "abc"
        assert normalise_etag('W/"abc"') == "abc"
        assert normalise_etag(None) == ""


# ---------------------------------------------------------------------------
# R2Client against the fake API
# ---------------------------------------------------------------------------


def _settings() -> CdnSettings:
    return CdnSettings(
        ACCOUNT, BUCKET, TOKEN, "https://cdn.otbgroup.co.uk", 1024 * 1024
    )


class TestR2Client:
    def test_list_follows_cursor_and_filters_prefix(self):
        fake = FakeCloudflare(
            {f"bir/x-v{i}.png": b"%d" % i for i in range(5)} | {"otb/y-v1.png": b"y"}
        )
        with R2Client(_settings(), transport=fake.transport()) as client:
            items = client.list_objects(prefix="bir/", limit=3)
            assert [i["key"] for i in items] == [
                "bir/x-v0.png",
                "bir/x-v1.png",
                "bir/x-v2.png",
            ]
            fake.requests.clear()
            everything = client.list_objects(prefix="bir/", limit=1000)
        assert len(everything) == 5
        assert all(r.url.params.get("prefix") == "bir/" for r in fake.requests)

    def test_find_object_is_exact(self):
        fake = FakeCloudflare({"bir/logo-v1.png": b"a", "bir/logo-v10.png": b"b"})
        with R2Client(_settings(), transport=fake.transport()) as client:
            assert client.find_object("bir/logo-v1.png")["key"] == "bir/logo-v1.png"
            assert client.find_object("bir/logo-v2.png") is None

    def test_put_sends_bearer_content_type_and_bytes(self):
        fake = FakeCloudflare()
        with R2Client(_settings(), transport=fake.transport()) as client:
            result = client.put_object(KEY, PNG, "image/png")
        put = [r for r in fake.requests if r.method == "PUT"][0]
        assert str(put.url) == f"{API_OBJECTS}/{KEY}"
        assert put.headers["content-type"] == "image/png"
        assert put.headers["content-length"] == str(len(PNG))
        assert put.content == PNG
        assert result["key"] == KEY
        assert fake.objects[KEY] == PNG

    def test_api_error_names_the_code_not_the_token(self):
        fake = FakeCloudflare()
        fake.put_status = 413
        with R2Client(_settings(), transport=fake.transport()) as client:
            with pytest.raises(r2_client.CdnApiError) as excinfo:
                client.put_object(KEY, PNG, "image/png")
        assert "HTTP 413" in str(excinfo.value) and "10041" in str(excinfo.value)
        assert TOKEN not in str(excinfo.value)

    def test_verify_public_by_etag(self):
        fake = FakeCloudflare({KEY: PNG})
        with R2Client(_settings(), transport=fake.transport()) as client:
            report = verify_public(client, _settings(), KEY, PNG)
        assert report["verified"] and report["etag_matches_md5"]
        assert report["etag"] == hashlib.md5(PNG).hexdigest()
        assert report["sha256"] == hashlib.sha256(PNG).hexdigest()
        assert report["body_sha256_matches"] is None
        assert [r.method for r in fake.requests] == ["HEAD"]

    def test_verify_public_falls_back_to_sha256_when_etag_is_not_md5(self):
        fake = FakeCloudflare({KEY: PNG})
        fake.head_etag_override = '"abc-2"'
        with R2Client(_settings(), transport=fake.transport()) as client:
            report = verify_public(client, _settings(), KEY, PNG)
        assert report["verified"] and not report["etag_matches_md5"]
        assert report["body_sha256_matches"] is True
        assert [r.method for r in fake.requests] == ["HEAD", "GET"]

    def test_verify_public_reports_mismatch_and_404(self):
        fake = FakeCloudflare({KEY: PNG})
        fake.head_etag_override = '"abc-2"'
        fake.serve_bytes_override = b"other"
        with R2Client(_settings(), transport=fake.transport()) as client:
            report = verify_public(client, _settings(), KEY, PNG)
        assert not report["verified"] and report["body_sha256_matches"] is False
        fake = FakeCloudflare()
        with R2Client(_settings(), transport=fake.transport()) as client:
            report = verify_public(client, _settings(), KEY, PNG)
        assert not report["verified"] and report["status"] == 404


# ---------------------------------------------------------------------------
# upload_cdn_asset
# ---------------------------------------------------------------------------


class TestUpload:
    @pytest.mark.asyncio
    async def test_happy_path_base64(self, cf, as_owner):
        out = await upload_cdn_asset(KEY, "image/png", base64_content=_b64(PNG))
        assert out.startswith(f"Uploaded {KEY}")
        assert f"public URL: {PUBLIC}" in out
        assert f"sha256: {hashlib.sha256(PNG).hexdigest()}" in out
        assert f"md5: {hashlib.md5(PNG).hexdigest()}" in out
        assert f"ETag {hashlib.md5(PNG).hexdigest()}" in out
        assert "ETag matches MD5: yes" in out
        assert "verified: yes" in out
        assert TOKEN not in out
        assert cf.objects[KEY] == PNG
        methods = [(r.method, str(r.url).split("?")[0]) for r in cf.requests]
        assert methods == [
            ("GET", API_OBJECTS),  # existence check
            ("PUT", f"{API_OBJECTS}/{KEY}"),
            ("HEAD", PUBLIC),
        ]

    @pytest.mark.asyncio
    async def test_happy_path_local_file(self, cf, as_owner, tmp_path, monkeypatch):
        monkeypatch.setenv("ALLOWED_FILE_DIRS", str(tmp_path))
        path = tmp_path / "bir-logo-black-v1.png"
        path.write_bytes(PNG)
        out = await upload_cdn_asset(KEY, "image/png", local_file=str(path))
        assert "verified: yes" in out
        assert cf.objects[KEY] == PNG

    @pytest.mark.asyncio
    async def test_refuses_existing_key_before_any_put(self, cf, as_owner):
        cf.objects[KEY] = b"old"
        with pytest.raises(UserInputError, match="already exists"):
            await upload_cdn_asset(KEY, "image/png", base64_content=_b64(PNG))
        assert [r.method for r in cf.requests] == ["GET"]
        assert cf.objects[KEY] == b"old"

    @pytest.mark.asyncio
    async def test_refuses_unversioned_name_before_any_call(self, cf, as_owner):
        with pytest.raises(UserInputError, match="name-vN.ext"):
            await upload_cdn_asset(
                "bir/bir-logo.png", "image/png", base64_content=_b64(PNG)
            )
        assert cf.requests == []

    @pytest.mark.asyncio
    async def test_exactly_one_source(self, cf, as_owner, tmp_path):
        with pytest.raises(UserInputError, match="exactly one"):
            await upload_cdn_asset(KEY, "image/png")
        with pytest.raises(UserInputError, match="exactly one"):
            await upload_cdn_asset(
                KEY,
                "image/png",
                base64_content=_b64(PNG),
                local_file=str(tmp_path / "x"),
            )
        assert cf.requests == []

    @pytest.mark.asyncio
    async def test_bad_base64_and_content_type(self, cf, as_owner):
        with pytest.raises(UserInputError, match="base64"):
            await upload_cdn_asset(KEY, "image/png", base64_content="***not base64***")
        with pytest.raises(UserInputError, match="content_type"):
            await upload_cdn_asset(KEY, "png", base64_content=_b64(PNG))
        assert cf.requests == []

    @pytest.mark.asyncio
    async def test_size_cap(self, cf, as_owner, monkeypatch):
        monkeypatch.setenv(r2_client.ENV_MAX_BYTES, "16")
        with pytest.raises(UserInputError, match="over the CDN upload limit"):
            await upload_cdn_asset(KEY, "image/png", base64_content=_b64(PNG))
        assert cf.requests == []

    @pytest.mark.asyncio
    async def test_missing_config_is_a_user_input_error(
        self, cf, as_owner, monkeypatch
    ):
        monkeypatch.delenv(r2_client.ENV_TOKEN)
        with pytest.raises(UserInputError, match="CF_R2_TOKEN"):
            await upload_cdn_asset(KEY, "image/png", base64_content=_b64(PNG))

    @pytest.mark.asyncio
    async def test_api_refusal_surfaces_without_the_token(self, cf, as_owner):
        cf.put_status = 403
        with pytest.raises(UserInputError) as excinfo:
            await upload_cdn_asset(KEY, "image/png", base64_content=_b64(PNG))
        assert "HTTP 403" in str(excinfo.value)
        assert TOKEN not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_failed_verification_is_reported_not_hidden(self, cf, as_owner):
        cf.head_status = 503
        out = await upload_cdn_asset(KEY, "image/png", base64_content=_b64(PNG))
        assert out.startswith("Uploaded, but verification FAILED")
        assert "HTTP 503" in out and "verified: NO" in out

    @pytest.mark.asyncio
    async def test_read_only_server_refuses_in_body(self, cf, as_owner, monkeypatch):
        from auth.scopes import set_read_only

        set_read_only(True)
        try:
            with pytest.raises(UserInputError, match="read-only"):
                await upload_cdn_asset(KEY, "image/png", base64_content=_b64(PNG))
        finally:
            set_read_only(False)
        assert cf.requests == []


# ---------------------------------------------------------------------------
# list_cdn_assets
# ---------------------------------------------------------------------------


class TestList:
    @pytest.mark.asyncio
    async def test_lists_with_public_urls(self, cf, as_owner):
        cf.objects.update(
            {"bir/a-v1.png": b"a", "bir/b-v1.png": b"bb", "otb/c-v1.png": b"c"}
        )
        out = await list_cdn_assets(prefix="bir/")
        assert "2 shown" in out
        assert (
            "- bir/a-v1.png" in out and "https://cdn.otbgroup.co.uk/bir/a-v1.png" in out
        )
        assert "otb/c-v1.png" not in out
        assert all(r.method == "GET" for r in cf.requests)

    @pytest.mark.asyncio
    async def test_says_when_more_exist(self, cf, as_owner):
        cf.objects.update({f"x/a-v{i}.png": b"a" for i in range(4)})
        out = await list_cdn_assets(max_results=2)
        assert "2 shown (more exist" in out

    @pytest.mark.asyncio
    async def test_empty(self, cf, as_owner):
        assert "(no objects)" in await list_cdn_assets()

    @pytest.mark.asyncio
    async def test_bad_max_results(self, cf, as_owner):
        with pytest.raises(UserInputError):
            await list_cdn_assets(max_results=0)


# ---------------------------------------------------------------------------
# Caller gate
# ---------------------------------------------------------------------------


class TestCallerGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("email", [None, "", "someone@otbgroup.co.uk"])
    async def test_refused_callers(self, cf, monkeypatch, email):
        monkeypatch.delenv(cdn_tools.ENV_ADMIN_EMAILS, raising=False)
        monkeypatch.setattr(
            cdn_tools, "_resolve_caller_email", AsyncMock(return_value=email)
        )
        monkeypatch.setattr(
            cdn_tools,
            "_resolve_caller_auth_path",
            AsyncMock(return_value="fastmcp_oauth"),
        )
        with pytest.raises(UserInputError):
            await list_cdn_assets()
        with pytest.raises(UserInputError):
            await upload_cdn_asset(KEY, "image/png", base64_content=_b64(PNG))
        assert cf.requests == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("via", ["bearer_token", "stdio", None])
    async def test_refused_auth_paths(self, cf, monkeypatch, via):
        monkeypatch.delenv(cdn_tools.ENV_ADMIN_EMAILS, raising=False)
        monkeypatch.setattr(
            cdn_tools, "_resolve_caller_email", AsyncMock(return_value=OWNER)
        )
        monkeypatch.setattr(
            cdn_tools, "_resolve_caller_auth_path", AsyncMock(return_value=via)
        )
        with pytest.raises(UserInputError, match="OAuth 2.1"):
            await list_cdn_assets()
        assert cf.requests == []

    @pytest.mark.asyncio
    async def test_allowlist_env_var(self, cf, monkeypatch):
        monkeypatch.setenv(
            cdn_tools.ENV_ADMIN_EMAILS, "Other@otbgroup.co.uk, second@otbgroup.co.uk"
        )
        monkeypatch.setattr(
            cdn_tools,
            "_resolve_caller_email",
            AsyncMock(return_value="other@otbgroup.co.uk"),
        )
        monkeypatch.setattr(
            cdn_tools,
            "_resolve_caller_auth_path",
            AsyncMock(return_value="mcp_session_binding"),
        )
        assert "(no objects)" in await list_cdn_assets()
        monkeypatch.setattr(
            cdn_tools, "_resolve_caller_email", AsyncMock(return_value=OWNER)
        )
        with pytest.raises(UserInputError):
            await list_cdn_assets()

    def test_accepted_paths_match_the_signature_tools(self):
        from gsignatures.signature_tools import ACCEPTED_AUTH_PATHS

        assert cdn_tools.ACCEPTED_AUTH_PATHS == ACCEPTED_AUTH_PATHS

    @pytest.mark.asyncio
    async def test_resolver_reads_the_request_context(self, monkeypatch):
        import fastmcp.server.dependencies as deps

        class Ctx:
            async def get_state(self, key):
                return {
                    "authenticated_user_email": OWNER,
                    "authenticated_via": "fastmcp_oauth",
                }[key]

        monkeypatch.setattr(deps, "get_context", lambda: Ctx())
        assert await cdn_tools._resolve_caller_email() == OWNER
        assert await cdn_tools._resolve_caller_auth_path() == "fastmcp_oauth"
        monkeypatch.setattr(deps, "get_context", lambda: None)
        assert await cdn_tools._resolve_caller_email() is None


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestWiring:
    TOOLS = {"upload_cdn_asset", "list_cdn_assets"}

    def test_registered_with_write_marker_on_upload_only(self):
        from core.server import server
        from core.tool_registry import get_tool_components

        registered = get_tool_components(server)
        assert self.TOOLS <= set(registered)
        assert (
            getattr(registered["upload_cdn_asset"].fn, "_workspace_write_tool", False)
            is True
        )
        assert not getattr(
            registered["list_cdn_assets"].fn, "_workspace_write_tool", False
        )

    def test_read_only_mode_removes_the_upload_tool(self):
        from auth.scopes import set_read_only
        from core.server import server
        from core.tool_registry import filter_server_tools, get_tool_components

        before = get_tool_components(server)
        set_read_only(True)
        try:
            filter_server_tools(server)
            after = get_tool_components(server)
            assert "upload_cdn_asset" not in after
            assert "list_cdn_assets" in after
        finally:
            set_read_only(False)
            for name, tool in before.items():
                if name not in get_tool_components(server):
                    server.local_provider.add_tool(tool)

    def test_opt_in_tiers_scopes_policy_audit(self):
        import main
        from auth import scopes
        from core.audit import _service
        from core.tool_policy import BLOCKED_TOOLS

        assert "gcdn" in main.OPT_IN_TOOLS
        src = (REPO_ROOT / "main.py").read_text()
        assert '"gcdn": lambda: import_module("gcdn.cdn_tools")' in src
        data = yaml.safe_load((REPO_ROOT / "core" / "tool_tiers.yaml").read_text())
        assert set(data["gcdn"]["core"]) == self.TOOLS
        assert scopes.TOOL_SCOPES_MAP["gcdn"] == []
        assert scopes.TOOL_READONLY_SCOPES_MAP["gcdn"] == []
        assert not (self.TOOLS & BLOCKED_TOOLS)
        assert _service("upload_cdn_asset", "gcdn.cdn_tools") == "cdn"
        assert "gcdn" not in (REPO_ROOT / "fastmcp_server.py").read_text()

    def test_no_s3_endpoint_anywhere_in_the_package(self):
        for path in (REPO_ROOT / "gcdn").glob("*.py"):
            text = path.read_text()
            assert "r2.cloudflarestorage.com" not in text, path.name
            assert "boto" not in text, path.name

    def test_signatures_match_the_spec(self):
        params = inspect.signature(upload_cdn_asset).parameters
        assert list(params) == ["path", "content_type", "base64_content", "local_file"]
        params = inspect.signature(list_cdn_assets).parameters
        assert list(params) == ["prefix", "max_results"]
        assert params["prefix"].default == ""

    def test_readme_documents_the_token_scope(self):
        readme = (REPO_ROOT / "README.md").read_text()
        assert "Workers R2 Storage" in readme
        assert re.search(r"CF_R2_TOKEN", readme)
        assert "upload_cdn_asset" in readme and "list_cdn_assets" in readme

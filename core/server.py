import functools
import logging
import os
from typing import Any, Dict, List, Mapping, Optional
from importlib import metadata

from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from starlette.applications import Starlette
from starlette.requests import Request

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.auth.providers.google import GoogleProvider

from auth.oauth21_session_store import get_oauth21_session_store, set_auth_provider
from auth.google_auth import handle_auth_callback, start_auth_flow, check_client_secrets
from auth.oauth_config import is_oauth21_enabled, is_external_oauth21_provider
from auth.oauth_responses import (
    create_error_response,
    create_success_response,
    create_server_error_response,
)
from auth.auth_info_middleware import AuthInfoMiddleware
from auth.access_policy_middleware import AccessPolicyMiddleware
from auth.scopes import SCOPES, get_current_scopes  # noqa
from core.config import (
    USER_GOOGLE_EMAIL,
    get_transport_mode,
    set_transport_mode as _set_transport_mode,
    get_oauth_redirect_uri as get_oauth_redirect_uri_for_current_mode,
)
from core.audit import audit_log, logger as audit_logger
from core.tool_policy import BLOCKED_TOOLS

logger = logging.getLogger(__name__)


def get_package_version() -> str:
    """Resolve the installed distribution version.

    This fork is distributed as ``workspace-mcp-fixed`` (see pyproject.toml);
    the upstream name ``workspace-mcp`` is kept as a fallback so the lookup
    still works on environments installed from the upstream package. Returns
    "dev" only when neither distribution is installed (e.g. running from a
    plain source checkout without ``uv sync`` / ``pip install``).
    """
    for dist_name in ("workspace-mcp-fixed", "workspace-mcp"):
        try:
            return metadata.version(dist_name)
        except metadata.PackageNotFoundError:
            continue
    return "dev"


_auth_provider: Optional[GoogleProvider] = None
_legacy_callback_registered = False


# Custom FastMCP that adds secure middleware stack for OAuth 2.1
class SecureFastMCP(FastMCP):
    def http_app(self, **kwargs) -> "Starlette":
        """Override to register HTTP-level startup hooks.

        We previously inserted a Starlette ``BaseHTTPMiddleware`` here to
        populate a session-scoped ContextVar. That middleware is removed:
        ``BaseHTTPMiddleware`` wraps responses through an anyio task-group
        queue that interleaves badly with the streamable-HTTP / SSE
        long-poll the MCP transport relies on, and under concurrent
        sessions can route a response intended for one session into
        another session's stream. The ContextVar it populated had no
        readers in the codebase, so dropping it is purely upside.
        Per-tool authentication state is populated by ``AuthInfoMiddleware``
        (a FastMCP-protocol middleware, not a Starlette HTTP one) keyed by
        the FastMCP session_id; that path stays intact.
        """
        app = super().http_app(**kwargs)
        app.add_event_handler("startup", _ensure_audit_started)
        # Graceful-shutdown drain: Render sends SIGTERM on every redeploy;
        # uvicorn translates that into Starlette's shutdown event. Without
        # this hook, up to AUDIT_FLUSH_INTERVAL_S of queued audit rows plus
        # any backlog were silently dropped on each deploy.
        app.add_event_handler("shutdown", _ensure_audit_stopped)
        return app


_audit_started = False


async def _ensure_audit_started() -> None:
    """Idempotent, fail-soft start of the audit background flusher.

    Called from the HTTP startup event AND lazily from each audited tool
    invocation, so the flusher runs under both streamable-http and stdio
    transports. Catches all errors (e.g. malformed AUDIT_SA_JSON_B64) so
    audit init never aborts server startup or tool calls.
    """
    global _audit_started
    if _audit_started:
        return
    _audit_started = True
    try:
        await audit_logger().start()
    except Exception as e:
        logger.error("Audit logger failed to start; continuing without audit: %s", e)


async def _ensure_audit_stopped() -> None:
    """Idempotent, fail-soft graceful shutdown of the audit flusher.

    Cancels the background flusher task and runs one final bounded
    drain+flush of everything queued (AuditLogger.stop). Rows that can't
    be written in time are dumped to stdout as AUDIT_FALLBACK rather than
    silently lost with the process.
    """
    global _audit_started
    if not _audit_started:
        return
    _audit_started = False
    try:
        await audit_logger().stop()
    except Exception as e:
        logger.error("Audit logger shutdown drain failed (non-fatal): %s", e)


server = SecureFastMCP(
    name="google_workspace",
    auth=None,
)

# Audit logging: wrap every registered tool with audit_log() before any
# @server.tool() decorator fires (including start_google_auth below) and
# before wrap_server_tool_method() runs in either entrypoint.
_original_server_tool = server.tool


def _audited_tool(*args, **kwargs):
    register = _original_server_tool(*args, **kwargs)

    def apply(fn):
        # Hard denylist, enforced at the single registration chokepoint every
        # tool passes through (both entrypoints import tool modules after this
        # patch is installed). A blocked tool's decorator becomes a no-op: it
        # is never registered, never listed, never callable. Fail-closed and
        # independent of tier / --tools / --read-only / env.
        if fn.__name__ in BLOCKED_TOOLS:
            logger.warning(
                "tool_policy: refusing to register blocked tool '%s'", fn.__name__
            )
            return fn

        audited = audit_log()(fn)

        @functools.wraps(audited)
        async def with_lazy_audit_start(*a, **kw):
            await _ensure_audit_started()
            return await audited(*a, **kw)

        return register(with_lazy_audit_start)

    return apply


server.tool = _audited_tool
logger.info("Audit logging: server.tool patched")

# Add the AuthInfo middleware to inject authentication into FastMCP context
auth_info_middleware = AuthInfoMiddleware()
server.add_middleware(auth_info_middleware)

# Group-based tool access policy. Must be added AFTER AuthInfoMiddleware:
# FastMCP runs middleware in registration order (first added = outermost),
# and this one reads the identity the auth middleware puts on the context.
# Inert until MCP_GROUP_POLICY_MODE=enforce; see core/access_policy.py.
access_policy_middleware = AccessPolicyMiddleware()
server.add_middleware(access_policy_middleware)


def _parse_bool_env(value: str) -> bool:
    """Parse environment variable string to boolean."""
    return value.lower() in ("1", "true", "yes", "on")


ALLOWED_CLIENT_REDIRECT_URIS_ENV = "MCP_ALLOWED_CLIENT_REDIRECT_URIS"


def _allowed_client_redirect_uris() -> Optional[List[str]]:
    """Comma-separated redirect URI patterns for dynamic client registration.

    Unset or blank -> ``None`` (FastMCP default: every redirect URI accepted).
    """
    raw = os.getenv(ALLOWED_CLIENT_REDIRECT_URIS_ENV, "")
    patterns = [p.strip() for p in raw.split(",") if p.strip()]
    return patterns or None


REFRESH_TOKEN_TTL_ENV = "MCP_OAUTH_REFRESH_TOKEN_TTL_S"
ALLOWED_EMAIL_DOMAINS_ENV = "OAUTH_ALLOWED_EMAIL_DOMAINS"


def _provider_hardening_kwargs(
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Extra ``GoogleProvider`` kwargs derived from the environment.

    * ``extra_authorize_params={"hd": <domain>}`` when
      ``OAUTH_ALLOWED_EMAIL_DOMAINS`` names exactly one domain. Google's
      sign-in page then pre-selects (or asks for) an account on that Workspace
      domain. It is a hint, not a control: the domain policy in
      ``AuthInfoMiddleware`` still rejects anything else. What it buys is that
      a staff member signed into a personal Google account in the same browser
      is not offered that account by default, so the wrong account's refresh
      token never lands in the OAuth store in the first place.
    * ``fallback_refresh_token_expiry_seconds`` from
      ``MCP_OAUTH_REFRESH_TOKEN_TTL_S``: how long a client may stay signed in
      to this server without the user consenting again. FastMCP's default is
      one year; unset keeps it. A positive integer number of seconds.
    """
    env = os.environ if environ is None else environ
    kwargs: Dict[str, Any] = {}

    domains = [
        d.strip().lower()
        for d in (env.get(ALLOWED_EMAIL_DOMAINS_ENV) or "").split(",")
        if d.strip()
    ]
    if len(domains) == 1:
        kwargs["extra_authorize_params"] = {"hd": domains[0]}

    raw_ttl = (env.get(REFRESH_TOKEN_TTL_ENV) or "").strip()
    if raw_ttl:
        try:
            ttl = int(raw_ttl)
            if ttl <= 0:
                raise ValueError("must be positive")
            kwargs["fallback_refresh_token_expiry_seconds"] = ttl
        except ValueError as exc:
            logger.warning(
                "%s=%r ignored (%s); FastMCP default refresh-token lifetime applies",
                REFRESH_TOKEN_TTL_ENV,
                raw_ttl,
                exc,
            )
    return kwargs


def set_transport_mode(mode: str):
    """Sets the transport mode for the server."""
    _set_transport_mode(mode)
    logger.info(f"Transport: {mode}")


def _ensure_legacy_callback_route() -> None:
    global _legacy_callback_registered
    if _legacy_callback_registered:
        return
    server.custom_route("/oauth2callback", methods=["GET"])(legacy_oauth2_callback)
    _legacy_callback_registered = True


def configure_server_for_http():
    """
    Configures the authentication provider for HTTP transport.
    This must be called BEFORE server.run().
    """
    global _auth_provider

    transport_mode = get_transport_mode()

    if transport_mode != "streamable-http":
        return

    # Use centralized OAuth configuration
    from auth.oauth_config import get_oauth_config

    config = get_oauth_config()

    # Check if OAuth 2.1 is enabled via centralized config
    oauth21_enabled = config.is_oauth21_enabled()

    if oauth21_enabled:
        if not config.is_configured():
            logger.warning("OAuth 2.1 enabled but OAuth credentials not configured")
            return

        def validate_and_derive_jwt_key(
            jwt_signing_key_override: str | None, client_secret: str
        ) -> bytes:
            """Validate JWT signing key override and derive the final JWT key."""
            if jwt_signing_key_override:
                if len(jwt_signing_key_override) < 12:
                    logger.warning(
                        "OAuth 2.1: FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY is less than 12 characters; "
                        "use a longer secret to improve key derivation strength."
                    )
                return derive_jwt_key(
                    low_entropy_material=jwt_signing_key_override,
                    salt="fastmcp-jwt-signing-key",
                )
            else:
                return derive_jwt_key(
                    high_entropy_material=client_secret,
                    salt="fastmcp-jwt-signing-key",
                )

        try:
            # Import common dependencies for storage backends
            from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
            from cryptography.fernet import Fernet
            from fastmcp.server.auth.jwt_issuer import derive_jwt_key

            required_scopes: List[str] = sorted(get_current_scopes())

            client_storage = None
            jwt_signing_key_override = (
                os.getenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", "").strip()
                or None
            )
            storage_backend = (
                os.getenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "")
                .strip()
                .lower()
            )
            valkey_host = os.getenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST", "").strip()

            # Determine storage backend: valkey, disk, memory (default)
            use_valkey = storage_backend == "valkey" or bool(valkey_host)
            use_disk = storage_backend == "disk"

            if use_valkey:
                try:
                    from key_value.aio.stores.valkey import ValkeyStore

                    valkey_port_raw = os.getenv(
                        "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_PORT", "6379"
                    ).strip()
                    valkey_db_raw = os.getenv(
                        "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_DB", "0"
                    ).strip()

                    valkey_port = int(valkey_port_raw)
                    valkey_db = int(valkey_db_raw)
                    valkey_use_tls_raw = os.getenv(
                        "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USE_TLS", ""
                    ).strip()
                    valkey_use_tls = (
                        _parse_bool_env(valkey_use_tls_raw)
                        if valkey_use_tls_raw
                        else valkey_port == 6380
                    )

                    valkey_request_timeout_ms_raw = os.getenv(
                        "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_REQUEST_TIMEOUT_MS", ""
                    ).strip()
                    valkey_connection_timeout_ms_raw = os.getenv(
                        "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_CONNECTION_TIMEOUT_MS", ""
                    ).strip()

                    valkey_request_timeout_ms = (
                        int(valkey_request_timeout_ms_raw)
                        if valkey_request_timeout_ms_raw
                        else None
                    )
                    valkey_connection_timeout_ms = (
                        int(valkey_connection_timeout_ms_raw)
                        if valkey_connection_timeout_ms_raw
                        else None
                    )

                    valkey_username = (
                        os.getenv(
                            "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USERNAME", ""
                        ).strip()
                        or None
                    )
                    valkey_password = (
                        os.getenv(
                            "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_PASSWORD", ""
                        ).strip()
                        or None
                    )

                    if not valkey_host:
                        valkey_host = "localhost"

                    client_storage = ValkeyStore(
                        host=valkey_host,
                        port=valkey_port,
                        db=valkey_db,
                        username=valkey_username,
                        password=valkey_password,
                    )

                    # Configure TLS and timeouts on the underlying Glide client config.
                    # ValkeyStore currently doesn't expose these settings directly.
                    glide_config = getattr(client_storage, "_client_config", None)
                    if glide_config is not None:
                        glide_config.use_tls = valkey_use_tls

                        is_remote_host = valkey_host not in {"localhost", "127.0.0.1"}
                        if valkey_request_timeout_ms is None and (
                            valkey_use_tls or is_remote_host
                        ):
                            # Glide defaults to 250ms if unset; increase for remote/TLS endpoints.
                            valkey_request_timeout_ms = 5000
                        if valkey_request_timeout_ms is not None:
                            glide_config.request_timeout = valkey_request_timeout_ms

                        if valkey_connection_timeout_ms is None and (
                            valkey_use_tls or is_remote_host
                        ):
                            valkey_connection_timeout_ms = 10000
                        if valkey_connection_timeout_ms is not None:
                            from glide_shared.config import (
                                AdvancedGlideClientConfiguration,
                            )

                            glide_config.advanced_config = (
                                AdvancedGlideClientConfiguration(
                                    connection_timeout=valkey_connection_timeout_ms
                                )
                            )

                    jwt_signing_key = validate_and_derive_jwt_key(
                        jwt_signing_key_override, config.client_secret
                    )

                    storage_encryption_key = derive_jwt_key(
                        high_entropy_material=jwt_signing_key.decode(),
                        salt="fastmcp-storage-encryption-key",
                    )

                    # A record encrypted under a previous key (client secret
                    # or JWT signing key rotated) must read as a miss, not
                    # raise: with the default, the first OAuth request after a
                    # rotation fails for every client until the store is wiped
                    # by hand. A miss makes the client re-register and the user
                    # re-consent, which is what a rotation is meant to do.
                    client_storage = FernetEncryptionWrapper(
                        key_value=client_storage,
                        fernet=Fernet(key=storage_encryption_key),
                        raise_on_decryption_error=False,
                    )
                    logger.info(
                        "OAuth 2.1: Using ValkeyStore for FastMCP OAuth proxy client_storage (host=%s, port=%s, db=%s, tls=%s)",
                        valkey_host,
                        valkey_port,
                        valkey_db,
                        valkey_use_tls,
                    )
                    if valkey_request_timeout_ms is not None:
                        logger.info(
                            "OAuth 2.1: Valkey request timeout set to %sms",
                            valkey_request_timeout_ms,
                        )
                    if valkey_connection_timeout_ms is not None:
                        logger.info(
                            "OAuth 2.1: Valkey connection timeout set to %sms",
                            valkey_connection_timeout_ms,
                        )
                    logger.info(
                        "OAuth 2.1: Applied Fernet encryption wrapper to Valkey client_storage (key derived from FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY or GOOGLE_OAUTH_CLIENT_SECRET)."
                    )
                except ImportError as exc:
                    logger.warning(
                        "OAuth 2.1: Valkey client_storage requested but Valkey dependencies are not installed (%s). "
                        "Install 'workspace-mcp[valkey]' (or 'py-key-value-aio[valkey]', which includes 'valkey-glide') "
                        "or unset WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND/WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST.",
                        exc,
                    )
                except ValueError as exc:
                    logger.warning(
                        "OAuth 2.1: Invalid Valkey configuration; falling back to default storage (%s).",
                        exc,
                    )
            elif use_disk:
                try:
                    from key_value.aio.stores.disk import DiskStore

                    disk_directory = os.getenv(
                        "WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY", ""
                    ).strip()
                    if not disk_directory:
                        # Default to FASTMCP_HOME/oauth-proxy or ~/.fastmcp/oauth-proxy
                        fastmcp_home = os.getenv("FASTMCP_HOME", "").strip()
                        if fastmcp_home:
                            disk_directory = os.path.join(fastmcp_home, "oauth-proxy")
                        else:
                            disk_directory = os.path.expanduser(
                                "~/.fastmcp/oauth-proxy"
                            )

                    client_storage = DiskStore(directory=disk_directory)

                    jwt_signing_key = validate_and_derive_jwt_key(
                        jwt_signing_key_override, config.client_secret
                    )

                    storage_encryption_key = derive_jwt_key(
                        high_entropy_material=jwt_signing_key.decode(),
                        salt="fastmcp-storage-encryption-key",
                    )

                    # A record encrypted under a previous key (client secret
                    # or JWT signing key rotated) must read as a miss, not
                    # raise: with the default, the first OAuth request after a
                    # rotation fails for every client until the store is wiped
                    # by hand. A miss makes the client re-register and the user
                    # re-consent, which is what a rotation is meant to do.
                    client_storage = FernetEncryptionWrapper(
                        key_value=client_storage,
                        fernet=Fernet(key=storage_encryption_key),
                        raise_on_decryption_error=False,
                    )
                    logger.info(
                        "OAuth 2.1: Using DiskStore for FastMCP OAuth proxy client_storage (directory=%s)",
                        disk_directory,
                    )
                except ImportError as exc:
                    logger.warning(
                        "OAuth 2.1: Disk storage requested but dependencies not available (%s). "
                        "Falling back to default storage.",
                        exc,
                    )
            elif storage_backend == "memory":
                from key_value.aio.stores.memory import MemoryStore

                client_storage = MemoryStore()
                logger.info(
                    "OAuth 2.1: Using MemoryStore for FastMCP OAuth proxy client_storage"
                )
            # else: client_storage remains None, FastMCP uses its default

            # Ensure JWT signing key is always derived for all storage backends
            if "jwt_signing_key" not in locals():
                jwt_signing_key = validate_and_derive_jwt_key(
                    jwt_signing_key_override, config.client_secret
                )

            # Check if external OAuth provider is configured
            if config.is_external_oauth21_provider():
                # External OAuth mode: use custom provider that handles ya29.* access tokens
                from auth.external_oauth_provider import ExternalOAuthProvider

                provider = ExternalOAuthProvider(
                    client_id=config.client_id,
                    client_secret=config.client_secret,
                    base_url=config.get_oauth_base_url(),
                    redirect_path=config.redirect_path,
                    required_scopes=required_scopes,
                    resource_server_url=config.get_oauth_base_url(),
                )
                server.auth = provider

                logger.info("OAuth 2.1 enabled with EXTERNAL provider mode")
                logger.info(
                    "Expecting Authorization bearer tokens in tool call headers"
                )
                logger.info(
                    "Protected resource metadata points to Google's authorization server"
                )
            else:
                # Standard OAuth 2.1 mode: use FastMCP's GoogleProvider.
                #
                # The protocol-level auth gate (`required_scopes`) is
                # deliberately narrowed to identity scopes only. FastMCP's
                # verifier rejects any token whose granted scopes are not a
                # *superset* of `required_scopes`
                # (GoogleTokenVerifier.verify_token / JWTVerifier do
                # `required_scopes.issubset(token_scopes)` and return None →
                # 401 invalid_token otherwise). Passing the full multi-service
                # scope list here rejected every least-privilege client — e.g.
                # a front-end that only requests `drive.file` — at /mcp, even
                # though its token was freshly and correctly issued.
                #
                # `valid_scopes` keeps the *full* enabled-service scope set
                # advertised through the well-known endpoints and used as the
                # DCR/registration default, so broad-consent clients are
                # unchanged and any client may still request any service scope.
                # The specific Google API scope each tool needs is still
                # enforced per-call by @require_google_service
                # (auth/service_decorator.py) — this relaxes only the coarse
                # front-door gate, not real authorization.
                #
                # The identity floor is BASE_SCOPES (openid + email + profile)
                # because the server attributes every request to a user by
                # email (audit log + per-user credential store); a token
                # without it can't be mapped to a user regardless of the gate.
                from auth.scopes import BASE_SCOPES

                identity_gate_scopes = sorted(set(BASE_SCOPES))
                provider_kwargs = {}
                allowed_redirects = _allowed_client_redirect_uris()
                if allowed_redirects is not None:
                    # Restrict dynamic client registration to known MCP
                    # clients. FastMCP's default (None) accepts ANY redirect
                    # URI, so an attacker could register their own "client"
                    # against this server and phish a staff member into
                    # authorising it. Patterns support wildcards, e.g.
                    # "http://localhost:*" for Claude Desktop / Claude Code.
                    provider_kwargs["allowed_client_redirect_uris"] = allowed_redirects
                hardening_kwargs = _provider_hardening_kwargs()
                provider_kwargs.update(hardening_kwargs)
                provider = GoogleProvider(
                    client_id=config.client_id,
                    client_secret=config.client_secret,
                    base_url=config.get_oauth_base_url(),
                    redirect_path=config.redirect_path,
                    required_scopes=identity_gate_scopes,
                    valid_scopes=required_scopes,
                    client_storage=client_storage,
                    jwt_signing_key=jwt_signing_key,
                    **provider_kwargs,
                )
                # Enable protocol-level auth
                server.auth = provider
                logger.info(
                    "OAuth 2.1 enabled using FastMCP GoogleProvider with protocol-level auth"
                )
                logger.info(
                    "OAuth 2.1 gate: required_scopes=%s; advertised valid_scopes=%d",
                    identity_gate_scopes,
                    len(required_scopes),
                )
                if allowed_redirects is None:
                    logger.warning(
                        "OAuth 2.1: MCP_ALLOWED_CLIENT_REDIRECT_URIS is unset, so "
                        "dynamic client registration accepts any redirect URI. "
                        "Set it before adding more users."
                    )
                else:
                    logger.info(
                        "OAuth 2.1: client redirect URIs restricted to %s",
                        allowed_redirects,
                    )
                if "extra_authorize_params" in hardening_kwargs:
                    logger.info(
                        "OAuth 2.1: Google sign-in hinted to hd=%s",
                        hardening_kwargs["extra_authorize_params"]["hd"],
                    )
                if "fallback_refresh_token_expiry_seconds" in hardening_kwargs:
                    logger.info(
                        "OAuth 2.1: refresh-token lifetime capped at %ss",
                        hardening_kwargs["fallback_refresh_token_expiry_seconds"],
                    )

                # Explicitly mount well-known routes from the OAuth provider
                # These should be auto-mounted but we ensure they're available
                try:
                    well_known_routes = provider.get_well_known_routes()
                    for route in well_known_routes:
                        logger.info(f"Mounting OAuth well-known route: {route.path}")
                        server.custom_route(route.path, methods=list(route.methods))(
                            route.endpoint
                        )
                except Exception as e:
                    logger.warning(f"Could not mount well-known routes: {e}")

            # Always set auth provider for token validation in middleware
            set_auth_provider(provider)
            _auth_provider = provider
        except Exception as exc:
            logger.error(
                "Failed to initialize FastMCP GoogleProvider: %s", exc, exc_info=True
            )
            raise
    else:
        logger.info("OAuth 2.0 mode - Server will use legacy authentication.")
        server.auth = None
        _auth_provider = None
        set_auth_provider(None)
        _ensure_legacy_callback_route()


def get_auth_provider() -> Optional[GoogleProvider]:
    """Gets the global authentication provider instance."""
    return _auth_provider


@server.custom_route("/", methods=["GET"])
@server.custom_route("/health", methods=["GET"])
async def health_check(request: Request):
    version = get_package_version()
    return JSONResponse(
        {
            "status": "healthy",
            "service": "workspace-mcp",
            "version": version,
            "transport": get_transport_mode(),
        }
    )


@server.custom_route("/attachments/{file_id}", methods=["GET"])
async def serve_attachment(request: Request):
    """Serve a stored attachment file.

    FastMCP custom routes are plain Starlette routes: Starlette calls the
    endpoint with the Request as the only argument, so path params must be
    read from ``request.path_params``.
    """
    from core.attachment_storage import get_attachment_storage

    file_id = request.path_params["file_id"]
    storage = get_attachment_storage()
    metadata = storage.get_attachment_metadata(file_id)

    instance = os.getenv("RENDER_INSTANCE_ID", "unknown")
    hint = (
        "Gmail attachments are now returned inline or via Drive transfer; "
        "re-run get_gmail_attachment_content instead of using /attachments/ URLs."
    )

    if not metadata:
        return JSONResponse(
            {
                "error": "Attachment not found or expired",
                "instance": instance,
                "hint": hint,
            },
            status_code=404,
        )

    file_path = storage.get_attachment_path(file_id)
    if not file_path:
        return JSONResponse(
            {
                "error": "Attachment file not found",
                "instance": instance,
                "hint": hint,
            },
            status_code=404,
        )

    # Capability URL: possession of the UUID is the only access control,
    # so make sure intermediaries never cache the response.
    return FileResponse(
        path=str(file_path),
        filename=metadata["filename"],
        media_type=metadata["mime_type"],
        headers={"Cache-Control": "no-store"},
    )


async def legacy_oauth2_callback(request: Request) -> HTMLResponse:
    state = request.query_params.get("state")
    code = request.query_params.get("code")
    error = request.query_params.get("error")

    if error:
        msg = (
            f"Authentication failed: Google returned an error: {error}. State: {state}."
        )
        logger.error(msg)
        return create_error_response(msg)

    if not code:
        msg = "Authentication failed: No authorization code received from Google."
        logger.error(msg)
        return create_error_response(msg)

    try:
        error_message = check_client_secrets()
        if error_message:
            return create_server_error_response(error_message)

        logger.info("OAuth callback: Received authorization code.")

        mcp_session_id = None
        if hasattr(request, "state") and hasattr(request.state, "session_id"):
            mcp_session_id = request.state.session_id

        verified_user_id, credentials = handle_auth_callback(
            scopes=get_current_scopes(),
            authorization_response=str(request.url),
            redirect_uri=get_oauth_redirect_uri_for_current_mode(),
            session_id=mcp_session_id,
        )

        logger.info(
            f"OAuth callback: Successfully authenticated user: {verified_user_id}."
        )

        try:
            store = get_oauth21_session_store()

            store.store_session(
                user_email=verified_user_id,
                access_token=credentials.token,
                refresh_token=credentials.refresh_token,
                token_uri=credentials.token_uri,
                client_id=credentials.client_id,
                client_secret=credentials.client_secret,
                scopes=credentials.scopes,
                expiry=credentials.expiry,
                session_id=f"google-{state}",
                mcp_session_id=mcp_session_id,
            )
            logger.info(
                f"Stored Google credentials in OAuth 2.1 session store for {verified_user_id}"
            )
        except Exception as e:
            logger.error(f"Failed to store credentials in OAuth 2.1 store: {e}")

        return create_success_response(verified_user_id)
    except Exception as e:
        logger.error(f"Error processing OAuth callback: {str(e)}", exc_info=True)
        return create_server_error_response(str(e))


@server.tool()
async def get_my_access() -> str:
    """Show which tools the group access policy allows for the signed-in user.

    Reports the caller's verified email, the policy groups they belong to,
    where the decision came from (policy, break-glass, lookup failure, or
    policy disabled) and the resulting tool list. Reads nothing from Google
    Workspace and is always callable, so a user who has been denied a tool
    can find out why without an administrator.
    """
    from core.access_policy import get_engine
    from core.tool_registry import get_tool_components

    email = None
    try:
        ctx = get_context()
        if ctx is not None:
            email = await ctx.get_state("authenticated_user_email")
    except Exception as exc:
        logger.debug(f"[get_my_access] could not read identity: {exc}")

    registered = sorted(get_tool_components(server).keys())
    try:
        engine = get_engine()
    except Exception as exc:
        return (
            f"Signed in as: {email or '<unknown>'}\n"
            f"Access policy: FAILED TO LOAD ({exc}). No tools are permitted "
            "until an administrator fixes core/group_policy.yaml."
        )

    decision = await engine.decide(email, registered)
    lines = [f"Signed in as: {decision.email or '<unknown>'}"]
    if not engine.enabled:
        lines.append(
            "Access policy: off (MCP_GROUP_POLICY_MODE is not 'enforce'); every "
            "registered tool is available."
        )
    else:
        lines.append(
            f"Access policy: enforce (membership via {engine.source_name}); "
            f"decision source: {decision.source}"
        )
        if decision.reason:
            lines.append(f"Reason: {decision.reason}")
        lines.append("Policy groups: " + (", ".join(sorted(decision.groups)) or "none"))
    lines.append(f"Allowed tools ({len(decision.allowed)} of {len(registered)}):")
    lines.extend(f"  - {name}" for name in sorted(decision.allowed))
    return "\n".join(lines)


@server.tool()
async def start_google_auth(
    service_name: str, user_google_email: str = USER_GOOGLE_EMAIL
) -> str:
    """
    Manually initiate Google OAuth authentication flow.

    NOTE: This is a legacy OAuth 2.0 tool and is disabled when OAuth 2.1 is enabled.
    The authentication system automatically handles credential checks and prompts for
    authentication when needed. Only use this tool if:
    1. You need to re-authenticate with different credentials
    2. You want to proactively authenticate before using other tools
    3. The automatic authentication flow failed and you need to retry

    In most cases, simply try calling the Google Workspace tool you need - it will
    automatically handle authentication if required.
    """
    if is_oauth21_enabled():
        if is_external_oauth21_provider():
            return (
                "start_google_auth is disabled when OAuth 2.1 is enabled. "
                "Provide a valid OAuth 2.1 bearer token in the Authorization header "
                "and retry the original tool."
            )
        return (
            "start_google_auth is disabled when OAuth 2.1 is enabled. "
            "Authenticate through your MCP client's OAuth 2.1 flow and retry the "
            "original tool."
        )

    if not user_google_email:
        raise ValueError("user_google_email must be provided.")

    error_message = check_client_secrets()
    if error_message:
        return f"**Authentication Error:** {error_message}"

    try:
        auth_message = await start_auth_flow(
            user_google_email=user_google_email,
            service_name=service_name,
            redirect_uri=get_oauth_redirect_uri_for_current_mode(),
        )
        return auth_message
    except Exception as e:
        logger.error(f"Failed to start Google authentication flow: {e}", exc_info=True)
        return f"**Error:** An unexpected error occurred: {e}"

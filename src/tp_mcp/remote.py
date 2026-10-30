"""Remote deployment: scoped coaching tools over Streamable HTTP at /mcp, behind OAuth.

The stdio server (tp_mcp.server.run_server) is unchanged. This module builds a
second MCP server object over the same tool definitions and handlers, exposing
a read-only default plus optional non-destructive coaching writes; all other tools are refused.

Configuration comes from the environment (Render secrets):
    TP_AUTH_COOKIE        optional TrainingPeaks cookie (legacy method)
    TP_USERNAME           TrainingPeaks login username (optional alternative)
    TP_PASSWORD           TrainingPeaks login password (optional alternative)
    MCP_OWNER_PASSWORD    password the owner types to approve a ChatGPT connection (16+ chars)
    MCP_SIGNING_KEY       secret that signs OAuth clients and tokens (32+ chars)
    MCP_PUBLIC_URL        public https base URL; on Render RENDER_EXTERNAL_URL is used when unset
    TP_MCP_ENABLE_WRITES  set to true to expose non-destructive coaching writes (default false)
    PORT                  listen port (Render sets it)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

from mcp.server import Server, ServerRequestContext
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from tp_mcp import __version__, apps
from tp_mcp.auth import AuthStatus, validate_auth
from tp_mcp.auth.password_login import TrainingPeaksLoginError, login_with_password
from tp_mcp.auth.storage import ENV_VAR_NAME
from tp_mcp.remote_auth import READ_SCOPE, WRITE_SCOPE, OwnerOAuthProvider
from tp_mcp.server import TOOLS, _on_list_resources, _on_read_resource, call_tool

logger = logging.getLogger("tp-mcp")

MCP_PATH = "/mcp"

# The read-only surface remains the default for ChatGPT Pro. On clients/plans
# that support full MCP writes, TP_MCP_ENABLE_WRITES=true enables only the
# explicitly selected non-destructive coaching actions below.
REMOTE_READ_TOOL_NAMES = frozenset(
    {
        "tp_get_workouts",
        "tp_get_workout",
        "tp_get_strength_workouts",
        "tp_get_strength_workout",
        "tp_get_strength_summary",
        "tp_analyze_workout",
        "tp_get_workout_prs",
        "tp_get_peaks",
        "tp_get_workout_comments",
        "tp_get_workout_note",
        "tp_list_notes",
        "tp_get_note",
        "tp_get_note_comments",
        "tp_get_fitness",
        "tp_get_weekly_summary",
        "tp_get_atp",
        "tp_get_athlete_settings",
        "tp_get_pool_length_settings",
        "tp_get_workout_types",
        "tp_validate_structure",
        "tp_get_libraries",
        "tp_get_library_items",
        "tp_get_library_item",
        "tp_get_metrics",
        "tp_get_nutrition",
        "tp_get_events",
        "tp_get_next_event",
        "tp_get_focus_event",
        "tp_search_exercises",
    }
)

REMOTE_WRITE_TOOL_NAMES = frozenset(
    {
        # Plan and adjust sessions; never delete completed data.
        "tp_create_workout",
        "tp_update_workout",
        "tp_copy_workout",
        "tp_reorder_workouts",
        "tp_create_strength_workout",
        "tp_update_strength_workout",
        # Training feedback and calendar notes.
        "tp_add_workout_comment",
        "tp_set_workout_note",
        "tp_create_note",
        "tp_update_note",
        "tp_add_note_comment",
        # Race and template planning.
        "tp_create_event",
        "tp_update_event",
        "tp_create_library",
        "tp_create_library_item",
        "tp_update_library_item",
        "tp_schedule_library_workout",
    }
)

# Public union for test assertions and documentation; not all are exposed by
# default. No deletion, pairing, credential, bulk-plan-apply, or zone updates.
REMOTE_TOOL_NAMES = REMOTE_READ_TOOL_NAMES | REMOTE_WRITE_TOOL_NAMES


class RemoteConfigError(RuntimeError):
    pass


def _remote_tools(enable_writes: bool = False) -> list:
    by_name = {t.name: t for t in TOOLS}
    if REMOTE_READ_TOOL_NAMES & REMOTE_WRITE_TOOL_NAMES:
        raise RemoteConfigError("Remote read and write allowlists overlap")
    missing = sorted(REMOTE_TOOL_NAMES - by_name.keys())
    if missing:
        raise RemoteConfigError(f"Remote tool allowlist names tools that do not exist: {', '.join(missing)}")
    wrongly_marked_read = sorted(
        n for n in REMOTE_READ_TOOL_NAMES
        if not by_name[n].annotations.read_only_hint or by_name[n].annotations.destructive_hint
    )
    wrongly_marked_write = sorted(
        n for n in REMOTE_WRITE_TOOL_NAMES
        if by_name[n].annotations.read_only_hint or by_name[n].annotations.destructive_hint
    )
    if wrongly_marked_read or wrongly_marked_write:
        raise RemoteConfigError(
            f"Remote tool annotation mismatch: read={wrongly_marked_read}, write={wrongly_marked_write}"
        )
    enabled = REMOTE_READ_TOOL_NAMES | (REMOTE_WRITE_TOOL_NAMES if enable_writes else frozenset())
    result = []
    for tool in TOOLS:
        if tool.name not in enabled:
            continue
        # Never advertise cross-athlete targeting on this single-owner remote server.
        # Copy the tool so the local stdio implementation is unchanged.
        safe_tool = tool.model_copy(deep=True)
        safe_tool.input_schema.get("properties", {}).pop("athlete", None)
        if safe_tool.name == "tp_schedule_library_workout":
            safe_tool.input_schema.get("properties", {}).pop("athletes", None)
        result.append(safe_tool)
    return result


@dataclass(frozen=True)
class RemoteConfig:
    public_url: str
    owner_password: str
    signing_key: str
    enable_writes: bool = False

    @classmethod
    def from_env(cls) -> RemoteConfig:
        env = os.environ
        problems = []
        has_username = bool(env.get("TP_USERNAME"))
        has_password = bool(env.get("TP_PASSWORD"))
        if not env.get(ENV_VAR_NAME) and not (has_username and has_password):
            problems.append(
                "Set either TP_AUTH_COOKIE or both TP_USERNAME and TP_PASSWORD "
                "in Render's private Environment tab"
            )
        if has_username != has_password:
            problems.append("TP_USERNAME and TP_PASSWORD must be set together")
        password = env.get("MCP_OWNER_PASSWORD", "")
        if len(password) < 16:
            problems.append("MCP_OWNER_PASSWORD must be set to at least 16 characters")
        key = env.get("MCP_SIGNING_KEY", "")
        if len(key) < 32:
            problems.append("MCP_SIGNING_KEY must be set to at least 32 characters")
        public_url = (env.get("MCP_PUBLIC_URL") or env.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
        if not public_url:
            problems.append("MCP_PUBLIC_URL is not set and RENDER_EXTERNAL_URL is not available")
        write_env = env.get("TP_MCP_ENABLE_WRITES", "false").strip().lower()
        if write_env not in {"", "0", "1", "false", "true", "no", "yes"}:
            problems.append("TP_MCP_ENABLE_WRITES must be true or false")
        if problems:
            raise RemoteConfigError("Remote server configuration incomplete: " + "; ".join(problems))
        return cls(
            public_url=public_url,
            owner_password=password,
            signing_key=key,
            enable_writes=write_env in {"1", "true", "yes"},
        )


def build_server(enable_writes: bool = False) -> Server:
    tools = _remote_tools(enable_writes)
    allowed_names = {t.name for t in tools}

    async def on_list_tools(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=tools)

    def refuse(code: str, message: str) -> CallToolResult:
        result = {"isError": True, "error_code": code, "message": message}
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, indent=2))],
            is_error=True,
        )

    async def on_call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        if params.name not in allowed_names:
            logger.warning("Refused remote tool outside allowlist: %s", params.name)
            return refuse("TOOL_NOT_AVAILABLE", f"{params.name} is not available on this remote server")
        args = params.arguments or {}
        # The original stdio server supports coach accounts selecting an athlete.
        # This remote server is intentionally restricted to the authenticated owner.
        if args.get("athlete") not in (None, ""):
            return refuse("ATHLETE_TARGETING_NOT_ALLOWED", "Remote tools operate on the owner account only")
        if params.name == "tp_schedule_library_workout" and args.get("athletes"):
            return refuse("ATHLETE_TARGETING_NOT_ALLOWED", "Bulk or cross-athlete scheduling is not available")
        return CallToolResult(content=list(await call_tool(params.name, args)))

    srv = Server(
        "trainingpeaks-mcp",
        version=__version__,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=_on_list_resources,
        on_read_resource=_on_read_resource,
    )
    srv.extensions[apps.EXTENSION_ID] = {}
    return srv


async def _healthz(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def build_app(config: RemoteConfig) -> Starlette:
    resource_url = f"{config.public_url}{MCP_PATH}"
    scope = WRITE_SCOPE if config.enable_writes else READ_SCOPE
    provider = OwnerOAuthProvider(
        signing_key=config.signing_key,
        owner_password=config.owner_password,
        issuer_url=config.public_url,
        resource_url=resource_url,
        scope=scope,
        enable_writes=config.enable_writes,
    )
    auth = AuthSettings(
        issuer_url=config.public_url,
        resource_server_url=resource_url,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[scope], default_scopes=[scope]
        ),
        revocation_options=RevocationOptions(enabled=False),
        required_scopes=[scope],
    )
    return build_server(config.enable_writes).streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
        auth=auth,
        token_verifier=ProviderTokenVerifier(provider),
        auth_server_provider=provider,
        custom_starlette_routes=[*provider.routes(), Route("/healthz", _healthz, methods=["GET"])],
    )


def run_remote_server() -> int:
    """Validate configuration and the TrainingPeaks credential, then serve /mcp."""
    import uvicorn

    try:
        config = RemoteConfig.from_env()
    except RemoteConfigError as e:
        logger.error("%s", e)
        return 1
    port_text = os.environ.get("PORT", "")
    if not port_text.isdigit():
        logger.error("PORT must be set to the listen port (Render sets it automatically); got %r", port_text)
        return 1

    cookie = os.environ.get(ENV_VAR_NAME, "")
    validation = asyncio.run(validate_auth(cookie)) if cookie else None
    if validation is None or validation.status is not AuthStatus.VALID:
        # Alternative to browser cookie extraction: perform the normal
        # TrainingPeaks password login on the server, then reuse the resulting
        # cookie for the existing internal API client.
        username = os.environ.get("TP_USERNAME", "")
        password = os.environ.get("TP_PASSWORD", "")
        if not username or not password:
            status = validation.status.value if validation else "no_credential"
            logger.error("TrainingPeaks credential unavailable or invalid (%s)", status)
            return 1
        try:
            cookie = asyncio.run(login_with_password(username, password))
        except TrainingPeaksLoginError as exc:
            logger.error("TrainingPeaks server-side login failed: %s", exc)
            return 1
        validation = asyncio.run(validate_auth(cookie))
        if validation.status is not AuthStatus.VALID:
            logger.error("TrainingPeaks login was rejected by its API (%s)", validation.status.value)
            return 1
        # Retained in this Render process only; never committed or logged.
        os.environ[ENV_VAR_NAME] = cookie
    logger.info("TrainingPeaks credential valid (athlete_id: %s)", validation.athlete_id)
    logger.info(
        "Serving MCP at %s%s (mode=%s, tools=%d)",
        config.public_url,
        MCP_PATH,
        "coaching-write" if config.enable_writes else "read-only",
        len(_remote_tools(config.enable_writes)),
    )

    uvicorn.run(
        build_app(config),
        host="0.0.0.0",
        port=int(port_text),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0

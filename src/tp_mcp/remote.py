"""Remote deployment: read-only tools over Streamable HTTP at /mcp, behind OAuth.

The stdio server (tp_mcp.server.run_server) is unchanged. This module builds a
second MCP server object over the same tool definitions and handlers, exposing
only REMOTE_TOOL_NAMES and refusing any other tool name at call time.

Configuration comes from the environment (Render secrets):
    TP_AUTH_COOKIE        TrainingPeaks Production_tpAuth cookie value
    MCP_OWNER_PASSWORD    password the owner types to approve a ChatGPT connection (16+ chars)
    MCP_SIGNING_KEY       secret that signs OAuth clients and tokens (32+ chars)
    MCP_PUBLIC_URL        public https base URL; on Render RENDER_EXTERNAL_URL is used when unset
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
from tp_mcp.auth.storage import ENV_VAR_NAME
from tp_mcp.remote_auth import SCOPE, OwnerOAuthProvider
from tp_mcp.server import TOOLS, _on_list_resources, _on_read_resource, call_tool

logger = logging.getLogger("tp-mcp")

MCP_PATH = "/mcp"

# The only tools the remote endpoint offers. Every one must be a read-only tool in
# tp_mcp.server.TOOLS; _remote_tools() refuses to build otherwise.
REMOTE_TOOL_NAMES = frozenset(
    {
        # workouts
        "tp_get_workouts",
        "tp_get_workout",
        "tp_get_strength_workouts",
        "tp_get_strength_workout",
        "tp_get_strength_summary",
        # workout detail and analysis
        "tp_analyze_workout",
        "tp_get_workout_prs",
        "tp_get_peaks",
        # comments and notes
        "tp_get_workout_comments",
        "tp_get_workout_note",
        "tp_list_notes",
        "tp_get_note",
        "tp_get_note_comments",
        # historical fitness
        "tp_get_fitness",
        "tp_get_weekly_summary",
        # athlete settings
        "tp_get_athlete_settings",
        "tp_get_pool_length_settings",
        # metrics
        "tp_get_metrics",
        "tp_get_nutrition",
        # events
        "tp_get_events",
        "tp_get_next_event",
        "tp_get_focus_event",
    }
)


class RemoteConfigError(RuntimeError):
    pass


def _remote_tools() -> list:
    by_name = {t.name: t for t in TOOLS}
    missing = sorted(REMOTE_TOOL_NAMES - by_name.keys())
    if missing:
        raise RemoteConfigError(f"Remote tool allowlist names tools that do not exist: {', '.join(missing)}")
    writable = sorted(
        n
        for n in REMOTE_TOOL_NAMES
        if not by_name[n].annotations.read_only_hint or by_name[n].annotations.destructive_hint
    )
    if writable:
        raise RemoteConfigError(f"Remote tool allowlist includes non-read-only tools: {', '.join(writable)}")
    return [t for t in TOOLS if t.name in REMOTE_TOOL_NAMES]


@dataclass(frozen=True)
class RemoteConfig:
    public_url: str
    owner_password: str
    signing_key: str

    @classmethod
    def from_env(cls) -> RemoteConfig:
        env = os.environ
        problems = []
        if not env.get(ENV_VAR_NAME):
            problems.append(f"{ENV_VAR_NAME} is not set (the TrainingPeaks Production_tpAuth cookie value)")
        password = env.get("MCP_OWNER_PASSWORD", "")
        if len(password) < 16:
            problems.append("MCP_OWNER_PASSWORD must be set to at least 16 characters")
        key = env.get("MCP_SIGNING_KEY", "")
        if len(key) < 32:
            problems.append("MCP_SIGNING_KEY must be set to at least 32 characters")
        public_url = (env.get("MCP_PUBLIC_URL") or env.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
        if not public_url:
            problems.append("MCP_PUBLIC_URL is not set and RENDER_EXTERNAL_URL is not available")
        if problems:
            raise RemoteConfigError("Remote server configuration incomplete: " + "; ".join(problems))
        return cls(public_url=public_url, owner_password=password, signing_key=key)


def build_server() -> Server:
    tools = _remote_tools()

    async def on_list_tools(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=tools)

    async def on_call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        if params.name not in REMOTE_TOOL_NAMES:
            logger.warning("Refused tool outside the remote read-only set: %s", params.name)
            refusal = {
                "isError": True,
                "error_code": "TOOL_NOT_AVAILABLE",
                "message": f"{params.name} is not available on the remote read-only server",
            }
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(refusal, indent=2))], is_error=True)
        return CallToolResult(content=list(await call_tool(params.name, params.arguments)))

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
    provider = OwnerOAuthProvider(
        signing_key=config.signing_key,
        owner_password=config.owner_password,
        issuer_url=config.public_url,
        resource_url=resource_url,
    )
    auth = AuthSettings(
        issuer_url=config.public_url,
        resource_server_url=resource_url,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=False),
        required_scopes=[SCOPE],
    )
    return build_server().streamable_http_app(
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

    result = asyncio.run(validate_auth(os.environ[ENV_VAR_NAME]))
    if result.status is not AuthStatus.VALID:
        logger.error(
            "TrainingPeaks rejected %s at startup (%s: %s). Update the secret in Render and redeploy.",
            ENV_VAR_NAME,
            result.status.value,
            result.message,
        )
        return 1
    logger.info("TrainingPeaks credential valid (athlete_id: %s)", result.athlete_id)
    logger.info("Serving read-only MCP at %s%s (%d tools)", config.public_url, MCP_PATH, len(REMOTE_TOOL_NAMES))

    uvicorn.run(
        build_app(config),
        host="0.0.0.0",
        port=int(port_text),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0

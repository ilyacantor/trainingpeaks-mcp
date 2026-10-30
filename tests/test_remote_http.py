"""Remote Streamable HTTP server: OAuth enforcement and the read-only tool surface.

Drives the real Starlette app (SDK auth routes, owner login, /mcp transport)
in-process over ASGI. No TrainingPeaks call is made: initialize, tools/list and
the refusal of a non-allowlisted tool never reach the TrainingPeaks API.
"""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tp_mcp.remote import (
    REMOTE_READ_TOOL_NAMES,
    REMOTE_TOOL_NAMES,
    REMOTE_WRITE_TOOL_NAMES,
    RemoteConfig,
    RemoteConfigError,
    _remote_tools,
    build_app,
)
from tp_mcp.server import TOOLS

BASE = "http://localhost"
RESOURCE = f"{BASE}/mcp"
REDIRECT = "https://chatgpt.com/connector/oauth/abc123"
PASSWORD = "correct-horse-battery-staple"
KEY = "k" * 48
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def _no_live_credential(monkeypatch):
    """Pin a worthless cookie so no test can reach a real TrainingPeaks account.

    TP_AUTH_COOKIE takes precedence over the keyring, so a developer's stored
    credential is never used here, even if an enforcement check regresses.
    """
    monkeypatch.setenv("TP_AUTH_COOKIE", "not-a-real-cookie")


def _config(key: str = KEY, public_url: str = BASE) -> RemoteConfig:
    return RemoteConfig(public_url=public_url, owner_password=PASSWORD, signing_key=key)


class _Running:
    def __init__(self, config: RemoteConfig):
        self.app = build_app(config)

    async def __aenter__(self) -> httpx.AsyncClient:
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)
        return self.client

    async def __aexit__(self, *exc):
        await self.client.aclose()
        await self._lifespan.__aexit__(*exc)


async def _register(client: httpx.AsyncClient, redirect: str = REDIRECT) -> httpx.Response:
    return await client.post(
        "/register",
        json={
            "redirect_uris": [redirect],
            "client_name": "ChatGPT",
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )


async def _connect(client: httpx.AsyncClient) -> tuple[dict, dict]:
    """Run the ChatGPT flow: register, authorize, owner login, token exchange."""
    reg = await _register(client)
    assert reg.status_code == 201, reg.text
    creds = reg.json()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    auth = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": creds["client_id"],
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st-1",
            "resource": RESOURCE,
        },
    )
    assert auth.status_code == 302, auth.text
    login_url = urlparse(auth.headers["location"])
    assert login_url.path == "/oauth/login"
    req = parse_qs(login_url.query)["req"][0]

    page = await client.get("/oauth/login", params={"req": req})
    assert page.status_code == 200 and "Owner password" in page.text

    wrong = await client.post("/oauth/login", data={"req": req, "password": "not-the-password"})
    assert wrong.status_code == 401 and "Wrong password" in wrong.text

    ok = await client.post("/oauth/login", data={"req": req, "password": PASSWORD})
    assert ok.status_code == 302
    back = urlparse(ok.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == REDIRECT
    q = parse_qs(back.query)
    assert q["state"] == ["st-1"]

    token = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "resource": RESOURCE,
        },
    )
    assert token.status_code == 200, token.text

    replay = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
    )
    assert replay.status_code == 400, "an authorization code must be single-use"
    return creds, token.json()


async def _rpc(client: httpx.AsyncClient, token: str | None, method: str, params: dict | None = None) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return await client.post("/mcp", json=body, headers=headers)


async def _initialize(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await _rpc(
        client,
        token,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
    )


def test_allowlist_is_read_only_and_excludes_mutation_and_auth_tools():
    tools = _remote_tools()
    assert {t.name for t in tools} == REMOTE_READ_TOOL_NAMES
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)
    for banned in ("tp_refresh_auth", "tp_auth_status", "tp_create_workout", "tp_update_workout", "tp_delete_workout"):
        assert banned not in REMOTE_READ_TOOL_NAMES
    writes = {t.name for t in TOOLS if not t.annotations.read_only_hint}
    assert writes and not (writes & REMOTE_READ_TOOL_NAMES)


def test_config_refuses_missing_or_weak_secrets(monkeypatch):
    for var in ("TP_AUTH_COOKIE", "MCP_OWNER_PASSWORD", "MCP_SIGNING_KEY", "MCP_PUBLIC_URL", "RENDER_EXTERNAL_URL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RemoteConfigError) as e:
        RemoteConfig.from_env()
    msg = str(e.value)
    for var in ("TP_AUTH_COOKIE", "MCP_OWNER_PASSWORD", "MCP_SIGNING_KEY", "MCP_PUBLIC_URL"):
        assert var in msg

    monkeypatch.setenv("TP_AUTH_COOKIE", "x")
    monkeypatch.setenv("MCP_OWNER_PASSWORD", "short")
    monkeypatch.setenv("MCP_SIGNING_KEY", KEY)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://tp.onrender.com/")
    with pytest.raises(RemoteConfigError, match="MCP_OWNER_PASSWORD"):
        RemoteConfig.from_env()

    monkeypatch.setenv("MCP_OWNER_PASSWORD", PASSWORD)
    assert RemoteConfig.from_env().public_url == "https://tp.onrender.com"


async def test_mcp_requires_a_valid_token():
    async with _Running(_config()) as client:
        none = await _rpc(client, None, "tools/list")
        assert none.status_code == 401
        challenge = none.headers["www-authenticate"]
        assert "resource_metadata=" in challenge and "/.well-known/oauth-protected-resource/mcp" in challenge

        forged = await _rpc(client, "eyJ0eXAiOiJhY2Nlc3MifQ.forged", "tools/list")
        assert forged.status_code == 401

        prm = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert prm["resource"] == RESOURCE and prm["authorization_servers"] == [BASE]
        meta = (await client.get("/.well-known/oauth-authorization-server")).json()
        assert "S256" in meta["code_challenge_methods_supported"]
        assert meta["registration_endpoint"] == f"{BASE}/register"

        assert (await client.get("/healthz")).text == "ok"


async def test_registration_accepts_only_chatgpt_redirects():
    async with _Running(_config()) as client:
        bad = await _register(client, redirect="https://attacker.example/callback")
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_redirect_uri"
        stable = await _register(client, redirect="https://chatgpt.com/connector_platform_oauth_redirect")
        assert stable.status_code == 201


async def test_oauth_flow_then_initialize_list_and_refuse_writes():
    async with _Running(_config()) as client:
        creds, tokens = await _connect(client)
        access = tokens["access_token"]

        init = await _initialize(client, access)
        assert init.status_code == 200, init.text
        assert init.json()["result"]["serverInfo"]["name"] == "trainingpeaks-mcp"

        listed = await _rpc(client, access, "tools/list")
        assert listed.status_code == 200, listed.text
        names = {t["name"] for t in listed.json()["result"]["tools"]}
        assert names == REMOTE_READ_TOOL_NAMES

        refused = await _rpc(
            client, access, "tools/call", {"name": "tp_delete_workout", "arguments": {"workout_id": "1"}}
        )
        result = refused.json()["result"]
        assert result["isError"] is True and "TOOL_NOT_AVAILABLE" in result["content"][0]["text"]

        refreshed = await client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        assert (await _rpc(client, refreshed.json()["access_token"], "tools/list")).status_code == 200
        reused = await client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
            },
        )
        assert reused.status_code == 400, "a refresh token must be single-use"


async def test_tokens_survive_restart_and_die_with_key_rotation_or_other_audience():
    async with _Running(_config()) as client:
        _, tokens = await _connect(client)
    access = tokens["access_token"]

    async with _Running(_config()) as restarted:
        assert (await _rpc(restarted, access, "tools/list")).status_code == 200

    async with _Running(_config(key="r" * 48)) as rotated:
        assert (await _rpc(rotated, access, "tools/list")).status_code == 401

    async with _Running(_config(public_url="http://127.0.0.1")) as other:
        other.base_url = httpx.URL("http://127.0.0.1")
        assert (await _rpc(other, access, "tools/list")).status_code == 401


async def test_login_locks_after_repeated_failures():
    async with _Running(_config()) as client:
        reg = (await _register(client)).json()
        auth = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": reg["client_id"],
                "redirect_uri": REDIRECT,
                "code_challenge": "x" * 43,
                "code_challenge_method": "S256",
                "state": "s",
            },
        )
        req = parse_qs(urlparse(auth.headers["location"]).query)["req"][0]
        for _ in range(10):
            assert (await client.post("/oauth/login", data={"req": req, "password": "nope"})).status_code == 401
        locked = await client.post("/oauth/login", data={"req": req, "password": PASSWORD})
        assert locked.status_code == 429 and "location" not in locked.headers


def test_opt_in_coaching_writes_are_nondestructive_and_owner_only():
    tools = _remote_tools(enable_writes=True)
    names = {tool.name for tool in tools}
    assert names == REMOTE_TOOL_NAMES
    assert REMOTE_WRITE_TOOL_NAMES <= names
    assert all(not tool.annotations.destructive_hint for tool in tools)
    assert "tp_delete_workout" not in names
    assert "tp_update_ftp" not in names
    assert "tp_apply_training_plan" not in names
    assert "tp_add_workout_comment" in names
    assert "tp_create_workout" in names
    assert all("athlete" not in tool.input_schema.get("properties", {}) for tool in tools)
    scheduling = next(tool for tool in tools if tool.name == "tp_schedule_library_workout")
    assert "athletes" not in scheduling.input_schema.get("properties", {})


async def test_write_mode_advertises_nondestructive_tools_and_refuses_cross_athlete_calls():
    cfg = RemoteConfig(public_url=BASE, owner_password=PASSWORD, signing_key=KEY, enable_writes=True)
    async with _Running(cfg) as client:
        creds, token = await _connect(client)
        access = token["access_token"]
        listed = await _rpc(client, access, "tools/list")
        assert listed.status_code == 200
        names = {t["name"] for t in listed.json()["result"]["tools"]}
        assert names == REMOTE_TOOL_NAMES
        no_delete = await _rpc(
            client, access, "tools/call",
            {"name": "tp_delete_workout", "arguments": {"workout_id": "1"}},
        )
        assert no_delete.json()["result"]["isError"] is True
        blocked = await _rpc(
            client, access, "tools/call",
            {
                "name": "tp_add_workout_comment",
                "arguments": {"athlete": "other", "workout_id": "1", "comment": "hi"},
            },
        )
        assert blocked.json()["result"]["isError"] is True
        assert "ATHLETE_TARGETING_NOT_ALLOWED" in blocked.json()["result"]["content"][0]["text"]

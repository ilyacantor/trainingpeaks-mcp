"""Credential-free tests of the optional TrainingPeaks server-side login.

All HTTP requests use MockTransport and fake usernames/passwords; tests never
attempt to sign in to a real TrainingPeaks account.
"""

import os
from urllib.parse import parse_qs

import httpx
import pytest

from tp_mcp.auth.password_login import TrainingPeaksLoginError, login_with_password
from tp_mcp.remote import RemoteConfig, RemoteConfigError


async def test_password_login_extracts_cookie_from_trainingpeaks_redirect():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.method == "GET" and request.url.path == "/login":
            return httpx.Response(
                200,
                text='<form><input type="hidden" name="__RequestVerificationToken" value="csrf123"></form>',
                headers={"set-cookie": "csrf=123; Path=/"},
            )
        if request.method == "POST" and request.url.path == "/login":
            params = parse_qs(request.content.decode())
            assert params["Username"] == ["test-athlete"]
            assert params["Password"] == ["fake-password"]
            assert params["__RequestVerificationToken"] == ["csrf123"]
            assert "csrf=123" in request.headers.get("cookie", "")
            return httpx.Response(
                302,
                headers={
                    "location": "/calendar",
                    "set-cookie": "Production_tpAuth=FAKE_COOKIE; Path=/; HttpOnly",
                },
            )
        return httpx.Response(200, text="calendar")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://home.trainingpeaks.com",
    ) as client:
        cookie = await login_with_password("test-athlete", "fake-password", client=client)
    assert cookie == "FAKE_COOKIE"
    assert len(requests) == 3


async def test_password_login_fails_closed_when_form_changes():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="no login form")),
    ) as client:
        with pytest.raises(TrainingPeaksLoginError, match="verification token missing"):
            await login_with_password("test-athlete", "fake-password", client=client)


async def test_password_login_failure_never_discloses_password():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                text='<input type="hidden" name="__RequestVerificationToken" value="csrf123">',
            )
        return httpx.Response(200, text="Invalid username or password")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TrainingPeaksLoginError) as exc:
            await login_with_password("test-athlete", "fake-password", client=client)
        assert "fake-password" not in str(exc.value)
        assert "test-athlete" not in str(exc.value)


def test_render_config_accepts_password_instead_of_cookie(monkeypatch):
    monkeypatch.delenv("TP_AUTH_COOKIE", raising=False)
    monkeypatch.setenv("TP_USERNAME", "test-athlete")
    monkeypatch.setenv("TP_PASSWORD", "fake-password")
    monkeypatch.setenv("MCP_OWNER_PASSWORD", "owner-pass-very-long-secret")
    monkeypatch.setenv("MCP_SIGNING_KEY", "key-of-at-least-32-characters-for-signing")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://example.onrender.com")
    config = RemoteConfig.from_env()
    assert config.public_url == "https://example.onrender.com"
    monkeypatch.delenv("TP_PASSWORD")
    with pytest.raises(RemoteConfigError, match="TP_USERNAME and TP_PASSWORD must be set together"):
        RemoteConfig.from_env()


async def test_password_login_refreshes_expired_tp_cookie_without_real_network(monkeypatch):
    from tp_mcp.client import http as tp_http
    from tp_mcp.client.http import TPClient

    monkeypatch.setenv("TP_AUTH_COOKIE", "expired-cookie")
    monkeypatch.setenv("TP_USERNAME", "test-athlete")
    monkeypatch.setenv("TP_PASSWORD", "fake-password")
    monkeypatch.setattr(tp_http, "_password_relogin_retry_after", 0.0)

    calls = []

    async def fake_login(username: str, password: str) -> str:
        assert (username, password) == ("test-athlete", "fake-password")
        calls.append("login")
        return "new-cookie"

    def handler(request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("cookie", "")
        if "new-cookie" in cookie:
            return httpx.Response(
                200,
                json={"success": True, "token": {"access_token": "FAKE_ACCESS", "expires_in": 3600}},
            )
        return httpx.Response(401)

    monkeypatch.setattr(tp_http, "login_with_password", fake_login)
    async with TPClient() as client:
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        response = await client._exchange_cookie_for_token()
        assert response.success
        assert response.data["token"]["access_token"] == "FAKE_ACCESS"
        assert calls == ["login"]
        assert tp_http.os.environ["TP_AUTH_COOKIE"] == "new-cookie"


@pytest.mark.skipif(
    os.environ.get("TP_MCP_LOGIN_FORM_PROBE") != "true",
    reason="Live public login-form probe enabled only for controlled Render verification",
)
async def test_live_trainingpeaks_login_form_is_compatible_from_render():
    """GET only, no credentials. Verify the vendor's actual form is parseable."""
    from tp_mcp.auth.password_login import LOGIN_URL, _LoginInputs

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        response = await client.get(LOGIN_URL)
    assert response.status_code == 200
    parser = _LoginInputs()
    parser.feed(response.text)
    assert parser.hidden_fields.get("__RequestVerificationToken")

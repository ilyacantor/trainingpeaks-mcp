"""OAuth 2.1 authorization server for the remote (Streamable HTTP) deployment.

ChatGPT connects to remote MCP servers only through OAuth (authorization code +
PKCE S256, with dynamic client registration); it cannot present a static API
key. This module makes the MCP server its own single-owner authorization
server:

- Dynamic client registration accepts only ChatGPT connector redirect URIs.
- The authorize step shows a password page; only the owner, who holds
  MCP_OWNER_PASSWORD, can approve a connection.
- Client ids, authorization codes, access tokens and refresh tokens are
  HMAC-signed with MCP_SIGNING_KEY and carry their own claims. Nothing lives in
  server memory that a restart would lose, so a Render restart or redeploy does
  not disconnect ChatGPT. Rotating MCP_SIGNING_KEY revokes every client and
  token at once.

Authorization codes and refresh tokens are single-use within a process
lifetime (an in-memory used-set); they also expire (5 minutes / 30 days).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from collections import deque
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    IdentityAssertionParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

logger = logging.getLogger("tp-mcp")

READ_SCOPE = "trainingpeaks.read"
WRITE_SCOPE = "trainingpeaks.write"
SCOPE = READ_SCOPE
LOGIN_PATH = "/oauth/login"

ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600
AUTH_CODE_TTL = 300
LOGIN_REQUEST_TTL = 600

# ChatGPT's connector OAuth callbacks (developers.openai.com/apps-sdk/build/auth):
# the stable URI, and the callback-specific form https://chatgpt.com/connector/oauth/{callback_id}.
CHATGPT_REDIRECT_EXACT = "https://chatgpt.com/connector_platform_oauth_redirect"
CHATGPT_REDIRECT_PREFIX = "https://chatgpt.com/connector/oauth/"

# Failed owner-password attempts allowed per window before the login page locks.
LOGIN_FAILURE_LIMIT = 10
LOGIN_FAILURE_WINDOW = 15 * 60


def is_chatgpt_redirect(uri: str) -> bool:
    return uri == CHATGPT_REDIRECT_EXACT or (
        uri.startswith(CHATGPT_REDIRECT_PREFIX) and len(uri) > len(CHATGPT_REDIRECT_PREFIX)
    )


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Sealer:
    """Signs and opens self-contained tokens: base64url(json claims).base64url(hmac)."""

    def __init__(self, key: str):
        self._key = key.encode()

    def _mac(self, typ: str, body: str) -> str:
        return _b64e(hmac.new(self._key, f"{typ}.{body}".encode(), hashlib.sha256).digest())

    def seal(self, typ: str, claims: dict[str, Any], ttl: int | None) -> str:
        payload = {**claims, "typ": typ, "jti": secrets.token_urlsafe(16)}
        if ttl is not None:
            payload["exp"] = int(time.time()) + ttl
        body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        return f"{body}.{self._mac(typ, body)}"

    def open(self, typ: str, token: str) -> dict[str, Any] | None:
        """Return the claims, or None if the token is malformed, forged, of another type, or expired."""
        body, sep, mac = token.partition(".")
        if not sep or not hmac.compare_digest(mac.encode(), self._mac(typ, body).encode()):
            return None
        try:
            claims = json.loads(_b64d(body))
        except ValueError:
            return None
        if not isinstance(claims, dict) or claims.get("typ") != typ:
            return None
        exp = claims.get("exp")
        if exp is not None and exp < time.time():
            return None
        return claims

    def derive(self, label: str, value: str) -> str:
        return hmac.new(self._key, f"{label}:{value}".encode(), hashlib.sha256).hexdigest()


class _UsedSet:
    """jti values already spent in this process, kept until their token would have expired."""

    def __init__(self) -> None:
        self._used: dict[str, float] = {}

    def spend(self, jti: str, exp: float) -> bool:
        """Mark jti spent. Returns False if it was already spent."""
        now = time.time()
        for k in [k for k, v in self._used.items() if v < now]:
            del self._used[k]
        if jti in self._used:
            return False
        self._used[jti] = exp
        return True

    def is_spent(self, jti: str) -> bool:
        return jti in self._used


class OwnerOAuthProvider:
    """OAuthAuthorizationServerProvider for a single owner, stateless across restarts."""

    def __init__(
        self, *,
        signing_key: str,
        owner_password: str,
        issuer_url: str,
        resource_url: str,
        scope: str = READ_SCOPE,
        enable_writes: bool = False,
    ):
        self._sealer = Sealer(signing_key)
        self._password_digest = hashlib.sha256(owner_password.encode()).digest()
        self.issuer_url = issuer_url.rstrip("/")
        self.resource_url = resource_url.rstrip("/")
        self.scope = scope
        self.enable_writes = enable_writes
        self._used_codes = _UsedSet()
        self._used_refresh = _UsedSet()
        self._login_failures: deque[float] = deque()

    # --- clients -----------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        claims = self._sealer.open("client", client_id)
        if claims is None:
            return None
        meta = claims["meta"]
        secret = None
        if meta.get("token_endpoint_auth_method") != "none":
            secret = self._sealer.derive("client_secret", client_id)
        return OAuthClientInformationFull.model_validate(
            {
                **meta,
                "client_id": client_id,
                "client_id_issued_at": claims["iat"],
                "client_secret": secret,
                "client_secret_expires_at": 0 if secret else None,
            }
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        bad = [str(u) for u in (client_info.redirect_uris or []) if not is_chatgpt_redirect(str(u))]
        if not client_info.redirect_uris or bad:
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description=(
                    "This server accepts only ChatGPT connector redirect URIs "
                    f"({CHATGPT_REDIRECT_EXACT} or {CHATGPT_REDIRECT_PREFIX}<id>); "
                    f"rejected: {', '.join(bad) or 'none given'}"
                ),
            )
        meta = client_info.model_dump(
            mode="json",
            exclude={"client_id", "client_secret", "client_id_issued_at", "client_secret_expires_at"},
            exclude_none=True,
        )
        issued_at = client_info.client_id_issued_at or int(time.time())
        client_id = self._sealer.seal("client", {"meta": meta, "iat": issued_at}, ttl=None)
        # The SDK's registration handler returns this same object to the client, so the
        # signed id (and the secret derived from it) replace the random ones it minted.
        client_info.client_id = client_id
        if client_info.client_secret is not None:
            client_info.client_secret = self._sealer.derive("client_secret", client_id)
        logger.info("OAuth client registered: %s", client_info.client_name or "(unnamed)")

    # --- authorization -----------------------------------------------------

    def _check_resource(self, resource: str | None) -> None:
        if resource is not None and resource.rstrip("/") != self.resource_url:
            raise AuthorizeError(
                error="invalid_request",
                error_description=f"resource must be {self.resource_url}, got {resource}",
            )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._check_resource(params.resource)
        scopes = params.scopes or [self.scope]
        if set(scopes) - {self.scope}:
            raise AuthorizeError(error="invalid_scope", error_description=f"Only '{self.scope}' is available")
        pending = self._sealer.seal(
            "login",
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "explicit": params.redirect_uri_provided_explicitly,
                "state": params.state,
                "scopes": scopes,
                "code_challenge": params.code_challenge,
                "resource": params.resource,
            },
            ttl=LOGIN_REQUEST_TTL,
        )
        return f"{self.issuer_url}{LOGIN_PATH}?req={pending}"

    def _locked_out(self) -> bool:
        cutoff = time.time() - LOGIN_FAILURE_WINDOW
        while self._login_failures and self._login_failures[0] < cutoff:
            self._login_failures.popleft()
        return len(self._login_failures) >= LOGIN_FAILURE_LIMIT

    def _password_ok(self, candidate: str) -> bool:
        return hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(), self._password_digest)

    async def login_page(self, request: Request) -> Response:
        req = request.query_params.get("req", "")
        if self._sealer.open("login", req) is None:
            return _page("This sign-in link is invalid or has expired. Start the connection again from ChatGPT.", 400)
        return _login_form(req, error=None)

    async def login_submit(self, request: Request) -> Response:
        form = await request.form()
        req = str(form.get("req", ""))
        password = str(form.get("password", ""))
        pending = self._sealer.open("login", req)
        if pending is None:
            return _page("This sign-in link is invalid or has expired. Start the connection again from ChatGPT.", 400)
        if self._locked_out():
            logger.warning(
                "Owner login locked: %d failed attempts in %d minutes", LOGIN_FAILURE_LIMIT, LOGIN_FAILURE_WINDOW // 60
            )
            return _page("Too many failed attempts. Try again in 15 minutes.", 429)
        if not self._password_ok(password):
            self._login_failures.append(time.time())
            logger.warning("Owner login failed (wrong password)")
            return _login_form(req, error="Wrong password.", status=401)

        code = self._sealer.seal(
            "code",
            {k: pending[k] for k in ("client_id", "redirect_uri", "explicit", "scopes", "code_challenge", "resource")},
            ttl=AUTH_CODE_TTL,
        )
        logger.info("Owner approved an OAuth connection")
        return RedirectResponse(
            construct_redirect_uri(pending["redirect_uri"], code=code, state=pending["state"]),
            status_code=302,
            headers=_SECURITY_HEADERS,
        )

    def routes(self) -> list[Route]:
        return [
            Route(LOGIN_PATH, self.login_page, methods=["GET"]),
            Route(LOGIN_PATH, self.login_submit, methods=["POST"]),
        ]

    # --- codes and tokens --------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        claims = self._sealer.open("code", authorization_code)
        if claims is None or claims["client_id"] != client.client_id or self._used_codes.is_spent(claims["jti"]):
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=claims["scopes"],
            expires_at=claims["exp"],
            client_id=claims["client_id"],
            code_challenge=claims["code_challenge"],
            redirect_uri=AnyUrl(claims["redirect_uri"]),
            redirect_uri_provided_explicitly=claims["explicit"],
            resource=claims["resource"],
            subject="owner",
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        claims = self._sealer.open("code", authorization_code.code)
        if claims is None or not self._used_codes.spend(claims["jti"], claims["exp"]):
            raise TokenError(error="invalid_grant", error_description="authorization code already used or expired")
        return self._issue(client.client_id, authorization_code.scopes)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        claims = self._sealer.open("refresh", refresh_token)
        if claims is None or claims["client_id"] != client.client_id or self._used_refresh.is_spent(claims["jti"]):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=claims["client_id"],
            scopes=claims["scopes"],
            expires_at=claims["exp"],
            subject="owner",
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        claims = self._sealer.open("refresh", refresh_token.token)
        if claims is None or not self._used_refresh.spend(claims["jti"], claims["exp"]):
            raise TokenError(error="invalid_grant", error_description="refresh token already used or expired")
        return self._issue(client.client_id, scopes or refresh_token.scopes)

    def _issue(self, client_id: str | None, scopes: list[str]) -> OAuthToken:
        base = {"client_id": client_id, "scopes": scopes}
        access = self._sealer.seal("access", {**base, "aud": self.resource_url}, ttl=ACCESS_TOKEN_TTL)
        refresh = self._sealer.seal("refresh", base, ttl=REFRESH_TOKEN_TTL)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        claims = self._sealer.open("access", token)
        if claims is None or claims.get("aud") != self.resource_url:
            return None
        return AccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=claims["scopes"],
            expires_at=claims["exp"],
            resource=claims["aud"],
            subject="owner",
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        raise NotImplementedError("Revocation is not offered; rotate MCP_SIGNING_KEY to revoke every token")

    async def exchange_identity_assertion(
        self, client: OAuthClientInformationFull, params: IdentityAssertionParams
    ) -> OAuthToken:
        raise NotImplementedError("The identity-assertion grant is not offered by this server")


_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://chatgpt.com; frame-ancestors 'none'"
    ),
}

_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:26rem;margin:4rem auto;padding:0 1rem;color:#222}"
    "input{width:100%;padding:.6rem;margin:.4rem 0 1rem;box-sizing:border-box;font-size:1rem}"
    "button{padding:.6rem 1.2rem;font-size:1rem}.err{color:#b00020}"
)


def _page(message: str, status: int) -> HTMLResponse:
    body = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>TrainingPeaks MCP</title>"
        f"<style>{_STYLE}</style></head><body><h2>TrainingPeaks MCP</h2><p>{html.escape(message)}</p></body></html>"
    )
    return HTMLResponse(body, status_code=status, headers=_SECURITY_HEADERS)


def _login_form(req: str, error: str | None, status: int = 200) -> HTMLResponse:
    err = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Connect TrainingPeaks</title><style>{_STYLE}</style></head><body>"
        "<h2>Connect ChatGPT to your TrainingPeaks</h2>"
        "<p>ChatGPT is asking for read-only access to your TrainingPeaks data. "
        "Enter the owner password (MCP_OWNER_PASSWORD in Render) to approve.</p>"
        f"{err}<form method='post' action='{LOGIN_PATH}'>"
        f"<input type='hidden' name='req' value='{html.escape(req, quote=True)}'>"
        "<label>Owner password<input type='password' name='password' "
        "autocomplete='current-password' required autofocus></label>"
        "<button type='submit'>Approve</button></form></body></html>"
    )
    return HTMLResponse(body, status_code=status, headers=_SECURITY_HEADERS)

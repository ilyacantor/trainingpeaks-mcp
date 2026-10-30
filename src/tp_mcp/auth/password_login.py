"""Optional TrainingPeaks username/password login for headless Render deployments.

Uses the existing TrainingPeaks web login form to obtain Production_tpAuth,
without extracting cookies from the user's browser. Credentials must be supplied
as Render environment secrets. No username, password or cookie is logged or
written to disk here. This uses an unofficial interface and may require updates
if the TrainingPeaks sign-in page changes.
"""

from __future__ import annotations

from html.parser import HTMLParser

import httpx

LOGIN_URL = "https://home.trainingpeaks.com/login"
LOGIN_ORIGIN = "https://home.trainingpeaks.com"
AUTH_COOKIE_NAME = "Production_tpAuth"


class TrainingPeaksLoginError(Exception):
    """A safe-to-display, secret-free sign-in failure."""


class _LoginInputs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden_fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "input":
            return
        values = dict(attrs)
        name = values.get("name")
        if name and (values.get("type") or "").lower() == "hidden":
            self.hidden_fields[name] = values.get("value") or ""


def _find_auth_cookie(client: httpx.AsyncClient, response: httpx.Response) -> str | None:
    # The HTTP client's jar tracks cookies from redirects as well as the final
    # response. Check every Set-Cookie step too in case the session is cleared
    # after being passed to a different TrainingPeaks host.
    for item in client.cookies.jar:
        if item.name == AUTH_COOKIE_NAME and item.value:
            return item.value
    for response_step in [*response.history, response]:
        for cookie in response_step.cookies.jar:
            if cookie.name == AUTH_COOKIE_NAME and cookie.value:
                return cookie.value
    return None


async def login_with_password(
    username: str, password: str, *, client: httpx.AsyncClient | None = None,
) -> str:
    """Return a fresh auth cookie after logging in through TrainingPeaks.

    If client is supplied, the caller owns its lifecycle. This also allows
    httpx.MockTransport tests without any real account calls.
    """
    if not username.strip() or not password:
        raise TrainingPeaksLoginError("TrainingPeaks username and password are required")

    async def perform(http: httpx.AsyncClient) -> str:
        try:
            page = await http.get(LOGIN_URL, follow_redirects=True)
            if page.status_code != 200:
                raise TrainingPeaksLoginError("TrainingPeaks login page is unavailable")
            parser = _LoginInputs()
            parser.feed(page.text)
            if not parser.hidden_fields.get("__RequestVerificationToken"):
                raise TrainingPeaksLoginError(
                    "TrainingPeaks login form has changed (verification token missing)"
                )
            fields = {
                **parser.hidden_fields,
                "Username": username.strip(),
                "Password": password,
            }
            response = await http.post(
                LOGIN_URL,
                data=fields,
                headers={
                    "Origin": LOGIN_ORIGIN,
                    "Referer": LOGIN_URL,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                follow_redirects=True,
            )
            cookie = _find_auth_cookie(http, response)
            if not cookie:
                raise TrainingPeaksLoginError(
                    "TrainingPeaks did not complete password login; check username, "
                    "password, SSO or additional verification requirements"
                )
            return cookie
        except httpx.RequestError:
            raise TrainingPeaksLoginError("Could not reach the TrainingPeaks login service") from None

    if client is not None:
        return await perform(client)
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
        return await perform(http)

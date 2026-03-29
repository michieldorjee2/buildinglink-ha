"""BuildingLink API client for Home Assistant.

Ported from the buildinglink-mcp TypeScript client.
Handles the OIDC authentication flow and API calls to BuildingLink.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse, parse_qs, urlencode as _urlencode, unquote

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.buildinglink.com"
API_BASE_URL = "https://api.buildinglink.com"
TENANT_PATH = "V2/Tenant"
HOME_PATH = "Home/DefaultNew.aspx"

# Azure subscription key — works for Properties/ContentCreator products,
# but NOT for EventLog (different APIM product, key is rejected).
# https://frontend-assets.buildinglink.com/js-shared-config-micro/1.0.28/js/index.js
SUBSCRIPTION_KEY = "d56c27729c5845ba94f51efd93155a71"

# BuildingLink proxies EventLog calls through www.buildinglink.com/api/.
# The proxy injects the correct subscription key server-side, so only the
# session cookie is needed.
API_PROXY_PATH = "/api"

# Mimic a real browser so BuildingLink doesn't reject headless requests
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class _FormParser(HTMLParser):
    """Minimal HTML parser to extract form action and input fields."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.inputs: dict[str, str] = {}
        self._in_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        if tag == "form":
            self._in_form = True
            self.action = attr_dict.get("action")
        elif tag == "input" and self._in_form:
            name = attr_dict.get("name")
            value = attr_dict.get("value", "")
            if name:
                self.inputs[name] = value or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False


class BuildingLinkApiError(Exception):
    """Raised when the BuildingLink API returns an error."""


class BuildingLinkAuthError(BuildingLinkApiError):
    """Raised when authentication fails."""


class BuildingLinkApi:
    """Async client for BuildingLink, handling OIDC auth and API calls."""

    def __init__(
        self,
        username: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.username = username
        self.password = password
        self._session = session
        self._owns_session = session is None
        self._cookies: dict[str, str] = {}
        self._token: dict[str, str] | None = None
        self._history: list[str] = []

    @property
    def is_authenticated(self) -> bool:
        """Check if the client has an active session."""
        return "bl.auth.cookie.oidc" in self._cookies

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # Use the default CookieJar so aiohttp handles per-domain cookie routing
            # correctly during the multi-domain OIDC flow. We still track cookies
            # manually in self._cookies so we control what gets sent where.
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": _USER_AGENT},
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        """Close the HTTP session if we own it."""
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    # ── Low-level fetch with redirect/auth handling ──────────────────

    async def _fetch(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: str | None = None,
    ) -> tuple[int, dict[str, str], str]:
        """Fetch a URL, following redirects and handling auth forms.

        Returns (status, response_headers, body_text).
        """
        session = await self._ensure_session()
        url = urljoin(BASE_URL, url)

        # Circular redirect protection
        if self.is_authenticated:
            self._history = []
        else:
            entry = f"[{method}] {url}"
            if entry in self._history:
                raise BuildingLinkApiError(f"Circular redirect detected: {entry}")
            self._history.append(entry)

        req_headers = dict(headers or {})

        # Add cookies (not for API domains)
        parsed = urlparse(url)
        if parsed.hostname not in ("api.buildinglink.com", "users.us1.buildinglink.com"):
            if self._cookies:
                req_headers["Cookie"] = "; ".join(
                    f"{k}={v}" for k, v in self._cookies.items()
                )

        _LOGGER.debug("BuildingLink %s %s", method, url)

        async with session.request(
            method,
            url,
            headers=req_headers,
            data=data,
            allow_redirects=False,
            ssl=True,
        ) as resp:
            # Update cookies from response — URL-decode values to match TypeScript client
            for cookie_header in resp.headers.getall("Set-Cookie", []):
                parts = cookie_header.split(";")[0].split("=", 1)
                if len(parts) == 2:
                    self._cookies[parts[0].strip()] = unquote(parts[1].strip())

            status = resp.status
            resp_headers = {k: v for k, v in resp.headers.items()}
            body = await resp.text()

        # Handle HTTP redirects
        if status in (301, 302, 307):
            location = resp_headers.get("Location", "")
            if location.startswith("/"):
                location = urljoin(url, location)

            # Inject internal_resident_app_apis into the OAuth scope wherever it appears.
            # This matches the TypeScript client and ensures the returned access_token
            # has the scope required by the BuildingLink API.
            parsed_loc = urlparse(location)
            qs = parse_qs(parsed_loc.query)
            if "scope" in qs:
                scope = qs["scope"][0]
                if "internal_resident_app_apis" not in scope:
                    scope += " internal_resident_app_apis"
                    qs["scope"] = [scope]
                    new_query = _urlencode(qs, doseq=True)
                    location = parsed_loc._replace(query=new_query).geturl()

            return await self._fetch(location)

        # Handle script redirects
        match = re.search(r'window\.top\.location\.href\s?="(https?://[^"]+)', body)
        if match:
            return await self._fetch(match.group(1))

        # Handle auth form if not authenticated
        if not self.is_authenticated and "<form" in body.lower():
            return await self._handle_auth_form(body, url)

        return status, resp_headers, body

    async def _handle_auth_form(
        self, html: str, current_url: str
    ) -> tuple[int, dict[str, str], str]:
        """Parse and submit authentication forms."""
        parser = _FormParser()
        parser.feed(html)

        if not parser.inputs:
            return 200, {}, html

        action = parser.action or current_url
        if not action.startswith("http"):
            action = urljoin(current_url, action)

        form_data = parser.inputs.copy()

        # Inject credentials
        if "Username" in form_data:
            form_data["Username"] = self.username
        if "Password" in form_data:
            form_data["Password"] = self.password

        # Store token if submitting to OIDC endpoint
        if "oidc" in action.lower():
            self._token = form_data.copy()

        encoded = urlencode(form_data)
        status, headers, body = await self._fetch(
            action,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=encoded,
        )

        if status != 200:
            error_match = re.search(
                r'<div class="validation-summary-errors">(.*?)</div>', body, re.DOTALL
            )
            msg = error_match.group(1).strip() if error_match else f"HTTP {status}"
            raise BuildingLinkAuthError(f"Failed to login: {msg}")

        return status, headers, body

    # ── High-level helpers ───────────────────────────────────────────

    def _cookie_header(self) -> str:
        """Build a Cookie header string from the tracked cookies."""
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items())

    async def _api(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Call the BuildingLink API using Bearer + subscription key.

        Works for Properties and ContentCreator endpoints.
        For EventLog endpoints, use :meth:`_api_proxy` instead.
        """
        if not self.is_authenticated:
            raise BuildingLinkApiError("Not authenticated — call login() first")

        url = f"{API_BASE_URL}/{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{qs}"

        session = await self._ensure_session()
        headers: dict[str, str] = {
            "ocp-apim-subscription-key": SUBSCRIPTION_KEY,
        }
        access_token = (self._token or {}).get("access_token", "")
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        _LOGGER.debug("BuildingLink API GET %s", url)

        async with session.get(url, headers=headers, ssl=True) as resp:
            if not resp.ok:
                text = await resp.text()
                raise BuildingLinkApiError(
                    f"API error {resp.status} for {path}: {text}"
                )
            return await resp.json()

    async def _api_proxy(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Call the BuildingLink API via the cookie-authenticated proxy.

        ``www.buildinglink.com/api/*`` proxies to the Azure APIM backend
        and injects the correct subscription key server-side. Only the
        session cookie is needed.  Required for EventLog endpoints whose
        APIM product rejects the public JS subscription key.
        """
        if not self.is_authenticated:
            raise BuildingLinkApiError("Not authenticated — call login() first")

        url = f"{BASE_URL}{API_PROXY_PATH}/{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{qs}"

        session = await self._ensure_session()

        _LOGGER.debug("BuildingLink API proxy GET %s", url)

        async with session.get(
            url, headers={"Cookie": self._cookie_header()}, ssl=True
        ) as resp:
            if not resp.ok:
                text = await resp.text()
                raise BuildingLinkApiError(
                    f"API error {resp.status} for {path}: {text}"
                )
            return await resp.json()

    async def login(self) -> None:
        """Authenticate with BuildingLink.

        Navigates to the tenant home page, which triggers the OIDC flow.
        After this call, ``is_authenticated`` should be ``True`` and
        ``_token`` should contain the access token.
        """
        if self.is_authenticated and self._token:
            return

        status, _, body = await self._fetch(f"{TENANT_PATH}/{HOME_PATH}")

        if not self.is_authenticated:
            raise BuildingLinkAuthError(
                "Authentication failed — no session cookie received"
            )
        _LOGGER.debug(
            "BuildingLink token fields: %s",
            list(self._token.keys()) if self._token else "none",
        )

        _LOGGER.info("BuildingLink authentication successful")

    async def get_deliveries(self) -> list[dict[str, Any]]:
        """Fetch all open deliveries via the cookie-authenticated API proxy."""
        params = {
            "$expand": "Location,Type,Authorizations",
            "$filter": "IsOpen eq true and Type/IsShownOnTenantHomePage eq true",
            "$skip": "0",
        }

        first_path = "EventLog/Resident/v1/Events"
        url: str | None = first_path
        deliveries: list[dict[str, Any]] = []

        while url:
            if url.startswith("http"):
                # Absolute @odata.nextLink — rewrite to go through the proxy
                parsed = urlparse(url)
                path = parsed.path.lstrip("/")
                data = await self._api_proxy(
                    path + (f"?{parsed.query}" if parsed.query else "")
                )
            else:
                data = await self._api_proxy(
                    url, params if url == first_path else None
                )

            if "value" in data:
                deliveries.extend(data["value"])

            url = data.get("@odata.nextLink")

        return deliveries

    async def get_occupant(self) -> dict[str, Any]:
        """Get the current occupant info."""
        return await self._api(
            "Properties/AuthenticatedUser/v1/property/occupant/get"
        )

    async def get_buildings(self) -> list[dict[str, Any]]:
        """Get authorized properties."""
        data = await self._api(
            "Properties/AuthenticatedUser/v1/property/authorized-properties"
        )
        return data.get("authorizedProperties", {}).get("data", [])

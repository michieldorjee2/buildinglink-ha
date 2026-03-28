"""BuildingLink API client for Home Assistant.

Ported from the buildinglink-mcp TypeScript client.
Handles the OIDC authentication flow and API calls to BuildingLink.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse, parse_qs, urlencode as _urlencode

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.buildinglink.com"
API_BASE_URL = "https://api.buildinglink.com"
TENANT_PATH = "V2/Tenant"
HOME_PATH = "Home/DefaultNew.aspx"

# Azure subscription key from BuildingLink frontend assets
# https://frontend-assets.buildinglink.com/js-shared-config-micro/1.0.24/js/index.js
SUBSCRIPTION_KEY = "d56c27729c5845ba94f51efd93155a71"


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
            self._session = aiohttp.ClientSession()
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
            # Update cookies from response
            for cookie_header in resp.headers.getall("Set-Cookie", []):
                parts = cookie_header.split(";")[0].split("=", 1)
                if len(parts) == 2:
                    self._cookies[parts[0].strip()] = parts[1].strip()

            status = resp.status
            resp_headers = {k: v for k, v in resp.headers.items()}
            body = await resp.text()

        # Handle HTTP redirects
        if status in (301, 302, 307):
            location = resp_headers.get("Location", "")
            if location.startswith("/"):
                location = urljoin(url, location)
                # Add internal_resident_app_apis scope if present
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

    async def _api(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Make an authenticated API call and return parsed JSON."""
        if not self._token:
            raise BuildingLinkApiError("Not authenticated — call login() first")

        url = f"{API_BASE_URL}/{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{qs}"

        session = await self._ensure_session()

        req_headers = {
            "Authorization": f"Bearer {self._token.get('access_token', '')}",
            "ocp-apim-subscription-key": SUBSCRIPTION_KEY,
        }

        _LOGGER.debug("BuildingLink API GET %s", url)

        async with session.get(url, headers=req_headers, ssl=True) as resp:
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
        if not self._token or "access_token" not in self._token:
            raise BuildingLinkAuthError(
                "Authentication failed — no access token received"
            )

        _LOGGER.info("BuildingLink authentication successful")

    async def get_deliveries(self) -> list[dict[str, Any]]:
        """Fetch all open deliveries via the OData API."""
        params = {
            "$expand": "Location,Type,Authorizations",
            "$filter": "IsOpen eq true and Type/IsShownOnTenantHomePage eq true",
            "$skip": "0",
        }

        url: str | None = "EventLog/Resident/v1/Events"
        deliveries: list[dict[str, Any]] = []

        while url:
            # For paginated calls, url may be absolute
            if url.startswith("http"):
                # Direct API call for pagination
                session = await self._ensure_session()
                req_headers = {
                    "Authorization": f"Bearer {self._token.get('access_token', '')}",
                    "ocp-apim-subscription-key": SUBSCRIPTION_KEY,
                }
                async with session.get(url, headers=req_headers, ssl=True) as resp:
                    if not resp.ok:
                        text = await resp.text()
                        raise BuildingLinkApiError(
                            f"API error {resp.status}: {text}"
                        )
                    data = await resp.json()
            else:
                data = await self._api(url, params if url == "EventLog/Resident/v1/Events" else None)

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

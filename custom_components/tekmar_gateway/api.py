"""Async client for the Tekmar Gateway 486 cloud API.

Two layers:
  1. Azure B2C custom-policy sign-in. Runs synchronously in an executor
     via the ``requests`` library (already bundled with HA core). We tried
     pure aiohttp first; Cloudflare in front of B2C rejects aiohttp's
     requests with a generic 400 even with matching headers, likely a
     TLS-fingerprint or header-ordering signal. Sign-in happens once every
     ~15 minutes so the executor overhead is negligible.
  2. Gateway REST API at gateway.tekmarcontrols.com, authenticated with
     the resulting token as the ``jwt`` cookie. This path uses async
     aiohttp normally - Cloudflare doesn't have the same hostility there.

The B2C flow is implicit (no refresh token), so we re-sign-in whenever the
cached token is within ``TOKEN_EXPIRY_BUFFER`` seconds of expiry.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs

import aiohttp
import requests

from .const import (
    B2C_AUTHORITY,
    B2C_CLIENT_ID,
    B2C_POLICY,
    B2C_REDIRECT,
    B2C_SCOPE,
    B2C_TENANT_PATH,
    GATEWAY_BASE,
    TOKEN_EXPIRY_BUFFER,
)

_LOGGER = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) "
    "Gecko/20100101 Firefox/151.0"
)


class TekmarAuthError(Exception):
    """Raised when sign-in fails (bad credentials, policy block, etc.)."""


class TekmarApiError(Exception):
    """Raised on non-auth API failures (gateway down, 5xx, etc.)."""


def _sync_b2c_signin(username: str, password: str) -> tuple[str, int]:
    """Synchronous Azure B2C SelfAsserted sign-in.

    Returns (access_token, expiry_unix_ts).

    Runs in an executor from the async wrapper. Uses ``requests`` because
    its TLS fingerprint and default header set pass Cloudflare's checks
    where aiohttp's do not (B2C otherwise returns a generic 400 Bad
    Request before our flow even hits B2C's policy engine).
    """
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})

    # --- Step 1: GET /authorize ---
    authorize_url = (
        f"{B2C_AUTHORITY}{B2C_TENANT_PATH}/oauth2/v2.0/authorize"
    )
    auth_params = {
        "p": B2C_POLICY,
        "client_id": B2C_CLIENT_ID,
        "redirect_uri": B2C_REDIRECT,
        "scope": B2C_SCOPE,
        "response_type": "id_token token",
        "response_mode": "fragment",
        "state": secrets.token_hex(16),
        "nonce": secrets.token_hex(16),
    }
    try:
        r = s.get(
            authorize_url,
            params=auth_params,
            headers={"Referer": f"{GATEWAY_BASE}/"},
            timeout=30,
        )
        r.raise_for_status()
    except requests.RequestException as err:
        raise TekmarAuthError(f"GET /authorize failed: {err}") from err

    m = re.search(
        r"(?:window\.)?SETTINGS\s*=\s*({.+?});", r.text, re.DOTALL
    )
    if not m:
        _LOGGER.debug(
            "no SETTINGS in /authorize; first 300 chars: %r", r.text[:300]
        )
        raise TekmarAuthError(
            "could not find SETTINGS in /authorize response; "
            "B2C policy may have changed"
        )
    try:
        settings = json.loads(m.group(1))
        csrf = settings["csrf"]
        tx = settings["transId"]
    except (json.JSONDecodeError, KeyError) as err:
        raise TekmarAuthError(
            f"could not parse csrf/transId from SETTINGS: {err}"
        ) from err

    # --- Step 2: POST credentials ---
    self_asserted_url = (
        f"{B2C_AUTHORITY}{B2C_TENANT_PATH}/{B2C_POLICY}/SelfAsserted"
    )
    sa_referer = (
        f"{B2C_AUTHORITY}{B2C_TENANT_PATH}/{B2C_POLICY}/"
        f"api/CombinedSigninAndSignup/unified"
    )
    try:
        r = s.post(
            self_asserted_url,
            params={"tx": tx, "p": B2C_POLICY},
            data={
                "request_type": "RESPONSE",
                "signInName": username,
                "password": password,
            },
            headers={
                "X-CSRF-TOKEN": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Origin": B2C_AUTHORITY,
                "Referer": sa_referer,
            },
            timeout=30,
        )
    except requests.RequestException as err:
        raise TekmarAuthError(f"POST /SelfAsserted failed: {err}") from err

    try:
        body = r.json()
    except ValueError as err:
        raise TekmarAuthError(
            f"SelfAsserted returned non-JSON (status {r.status_code}): "
            f"{r.text[:200]!r}"
        ) from err

    if str(body.get("status")) != "200":
        # Common errorCodes here: AADB2C90054 (bad creds),
        # AADB2C90158 (locked out), etc.
        raise TekmarAuthError(f"sign-in failed: {body}")

    # --- Step 3: GET /confirmed (don't follow the redirect) ---
    confirmed_url = (
        f"{B2C_AUTHORITY}{B2C_TENANT_PATH}/{B2C_POLICY}"
        f"/api/CombinedSigninAndSignup/confirmed"
    )
    try:
        r = s.get(
            confirmed_url,
            params={
                "rememberMe": "false",
                "csrf_token": csrf,
                "tx": tx,
                "p": B2C_POLICY,
            },
            allow_redirects=False,
            timeout=30,
        )
    except requests.RequestException as err:
        raise TekmarAuthError(f"GET /confirmed failed: {err}") from err

    if r.status_code != 302:
        raise TekmarAuthError(
            f"expected 302 from /confirmed, got {r.status_code}: "
            f"{r.text[:200]!r}"
        )
    loc = r.headers.get("Location", "")
    if "#" not in loc:
        raise TekmarAuthError("no fragment in /confirmed redirect")
    frag = parse_qs(loc.split("#", 1)[1])
    token = frag.get("access_token", [None])[0]
    if not token:
        err_desc = frag.get("error_description", [""])[0]
        raise TekmarAuthError(
            f"no access_token in /confirmed redirect: {err_desc or loc}"
        )

    exp = _jwt_exp(token) or (int(time.time()) + 900)
    return token, exp


def _jwt_exp(token: str) -> int:
    """Return the ``exp`` claim from a JWT, or 0 if unparseable."""
    parts = token.split(".")
    if len(parts) < 2:
        return 0
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload.get("exp", 0))
    except (ValueError, json.JSONDecodeError):
        return 0


class TekmarApiClient:
    """Async client for the Tekmar Gateway 486 cloud."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._access_token: str | None = None
        self._token_exp: int = 0
        self._auth_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Token lifecycle
    # ------------------------------------------------------------------

    def _token_is_fresh(self) -> bool:
        if not self._access_token:
            return False
        return time.time() < self._token_exp - TOKEN_EXPIRY_BUFFER

    async def _ensure_token(self) -> str:
        if self._token_is_fresh():
            return self._access_token  # type: ignore[return-value]
        async with self._auth_lock:
            if self._token_is_fresh():
                return self._access_token  # type: ignore[return-value]
            await self._sign_in()
            return self._access_token  # type: ignore[return-value]

    async def _sign_in(self) -> None:
        """Sign in via the sync requests-based flow in an executor."""
        loop = asyncio.get_running_loop()
        try:
            token, exp = await loop.run_in_executor(
                None,
                _sync_b2c_signin,
                self._username,
                self._password,
            )
        except TekmarAuthError:
            raise
        except Exception as err:
            raise TekmarAuthError(
                f"unexpected sign-in error: {err}"
            ) from err
        self._access_token = token
        self._token_exp = exp
        _LOGGER.debug(
            "signed in; token valid for %d s",
            self._token_exp - int(time.time()),
        )

    # ------------------------------------------------------------------
    # Gateway REST API (async, uses shared aiohttp session)
    # ------------------------------------------------------------------

    async def _gw_request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        body: str | None = None,
    ) -> Any:
        for attempt in (1, 2):
            token = await self._ensure_token()
            headers = {
                "User-Agent": _UA,
                "Accept": "application/json",
            }
            kwargs: dict[str, Any] = {
                "headers": headers,
                "cookies": {"jwt": token},
                "timeout": aiohttp.ClientTimeout(total=30),
            }
            if params is not None:
                kwargs["params"] = params
            if body is not None:
                kwargs["data"] = body
                headers["Content-Type"] = "text/plain"
            try:
                async with self._session.request(
                    method, f"{GATEWAY_BASE}{path}", **kwargs
                ) as resp:
                    if resp.status == 401 and attempt == 1:
                        # Token may have been revoked early -- drop it and
                        # retry once after a fresh sign-in.
                        self._access_token = None
                        self._token_exp = 0
                        continue
                    if resp.status == 401:
                        # A freshly minted token was rejected too: the
                        # account has lost access (removed from the
                        # building, disabled, ...). Surface as an auth
                        # problem so HA raises a repair instead of
                        # silently retrying forever.
                        raise TekmarAuthError(
                            f"{method} {path} rejected a fresh token (401)"
                        )
                    if resp.status >= 400:
                        text = await resp.text()
                        raise TekmarApiError(
                            f"{method} {path} returned {resp.status}: "
                            f"{text[:200]}"
                        )
                    text = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise TekmarApiError(
                    f"{method} {path} failed: {err}"
                ) from err
            if not text:
                return None
            if text.strip() == "OK":
                return "OK"
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        raise TekmarApiError("authentication retry exhausted")  # unreachable

    # --- public read methods ---

    async def list_buildings(self) -> list[dict[str, Any]]:
        return await self._gw_request(
            "GET",
            "/buildings",
            params=[
                ("field", f)
                for f in (
                    "uuid", "name", "addr", "city", "state",
                    "country", "status", "level",
                )
            ],
        )

    async def get_system(self, building_id: str) -> dict[str, Any]:
        return await self._gw_request(
            "GET", f"/buildings/{building_id}/system"
        )

    async def list_devices(self, building_id: str) -> list[dict[str, Any]]:
        return await self._gw_request(
            "GET", f"/buildings/{building_id}/devices"
        )

    async def get_device(
        self, building_id: str, addr: str
    ) -> dict[str, Any]:
        return await self._gw_request(
            "GET",
            f"/buildings/{building_id}/devices/{addr}",
            params={"filter": "curr"},
        )

    # --- public write methods ---

    async def set_heat(
        self, building_id: str, addr: str, value: int
    ) -> None:
        await self._gw_request(
            "PUT",
            f"/buildings/{building_id}/devices/{addr}/settings/heat",
            body=str(value),
        )

    async def set_cool(
        self, building_id: str, addr: str, value: int
    ) -> None:
        await self._gw_request(
            "PUT",
            f"/buildings/{building_id}/devices/{addr}/settings/cool",
            body=str(value),
        )

    async def set_mode(
        self, building_id: str, addr: str, mode: str
    ) -> None:
        valid = {"off", "heat", "cool", "auto", "emergency"}
        if mode not in valid:
            raise ValueError(
                f"mode must be one of {sorted(valid)}, got {mode!r}"
            )
        await self._gw_request(
            "PUT",
            f"/buildings/{building_id}/devices/{addr}/settings/mode",
            body=mode,
        )

"""
Auth Manager — reads credentials from environment variables, never from API requests.

Credentials are stored ONLY in .env on the server. The LLM / API callers
never see or provide credentials. Tokens are fetched and refreshed automatically.

Env var naming — use SPEC_ID uppercased (hyphens → underscores) as prefix:

  For spec_id "fresenius":
    FRESENIUS_AUTH_TYPE        = xsuaa | oauth2 | basic | apikey | bearer_static | none
    FRESENIUS_AUTH_URL         = https://<subaccount>.authentication.eu10.hana.ondemand.com
    FRESENIUS_CLIENT_ID        = clientid from XSUAA / OAuth2 service binding
    FRESENIUS_CLIENT_SECRET    = clientsecret from XSUAA / OAuth2 service binding
    FRESENIUS_GRANT_TYPE       = client_credentials (default) | password | ...
    FRESENIUS_TOKEN_URL        = override token endpoint (default: AUTH_URL/oauth/token)
    FRESENIUS_API_KEY          = API key value (for apikey type)
    FRESENIUS_API_KEY_NAME     = header / param name (default: X-API-Key)
    FRESENIUS_API_KEY_IN       = header | query | cookie (default: header)
    FRESENIUS_USERNAME         = username for basic auth
    FRESENIUS_PASSWORD         = password for basic auth
    FRESENIUS_BEARER_TOKEN     = static bearer token (for bearer_static type)
"""

import asyncio
import base64
import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class AuthConfig:
    auth_type: str       # xsuaa | oauth2 | basic | apikey | bearer_static | none
    auth_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    grant_type: str = "client_credentials"
    token_url_override: str = ""   # if set, used instead of auth_url + /oauth/token
    api_key: str = ""
    api_key_name: str = "X-API-Key"
    api_key_in: str = "header"    # header | query | cookie
    username: str = ""
    password: str = ""
    bearer_token: str = ""        # static token


@dataclass
class _CachedToken:
    access_token: str
    expires_at: float             # Unix timestamp; 0 = never expires (static)


# ── Auth Manager ───────────────────────────────────────────────────────────────

class AuthManager:
    """
    Singleton that manages per-spec auth configuration and token lifecycle.
    All credentials come from environment variables — never from API callers.
    """

    def __init__(self):
        self._token_cache: dict[str, _CachedToken] = {}
        self._lock = asyncio.Lock()

    # ── Config ────────────────────────────────────────────────────────────────

    @staticmethod
    def _prefix(spec_id: str) -> str:
        """Convert spec_id to env var prefix: 'my-spec' → 'MY_SPEC'"""
        return spec_id.upper().replace("-", "_").replace(" ", "_").replace(".", "_")

    def get_config(self, spec_id: str) -> Optional[AuthConfig]:
        """Read auth config for spec_id from env vars. Returns None if not configured."""
        p = self._prefix(spec_id)
        auth_type = os.getenv(f"{p}_AUTH_TYPE", "").lower().strip()
        if not auth_type:
            return None
        return AuthConfig(
            auth_type=auth_type,
            auth_url=os.getenv(f"{p}_AUTH_URL", "").rstrip("/"),
            client_id=os.getenv(f"{p}_CLIENT_ID", ""),
            client_secret=os.getenv(f"{p}_CLIENT_SECRET", ""),
            grant_type=os.getenv(f"{p}_GRANT_TYPE", "client_credentials"),
            token_url_override=os.getenv(f"{p}_TOKEN_URL", ""),
            api_key=os.getenv(f"{p}_API_KEY", ""),
            api_key_name=os.getenv(f"{p}_API_KEY_NAME", "X-API-Key"),
            api_key_in=os.getenv(f"{p}_API_KEY_IN", "header"),
            username=os.getenv(f"{p}_USERNAME", ""),
            password=os.getenv(f"{p}_PASSWORD", ""),
            bearer_token=os.getenv(f"{p}_BEARER_TOKEN", ""),
        )

    def is_configured(self, spec_id: str) -> bool:
        return self.get_config(spec_id) is not None

    def env_var_names(self, spec_id: str) -> dict[str, str]:
        """Return the env var names (not values) for this spec_id."""
        p = self._prefix(spec_id)
        return {
            "AUTH_TYPE":     f"{p}_AUTH_TYPE",
            "AUTH_URL":      f"{p}_AUTH_URL",
            "CLIENT_ID":     f"{p}_CLIENT_ID",
            "CLIENT_SECRET": f"{p}_CLIENT_SECRET",
            "GRANT_TYPE":    f"{p}_GRANT_TYPE",
            "TOKEN_URL":     f"{p}_TOKEN_URL",
            "API_KEY":       f"{p}_API_KEY",
            "API_KEY_NAME":  f"{p}_API_KEY_NAME",
            "USERNAME":      f"{p}_USERNAME",
            "PASSWORD":      f"{p}_PASSWORD",
            "BEARER_TOKEN":  f"{p}_BEARER_TOKEN",
        }

    # ── Token resolution ──────────────────────────────────────────────────────

    async def get_auth_headers(
        self, spec_id: str
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        """
        Return (auth_headers, auth_query_params, auth_cookies) for a spec.
        Fetches or refreshes OAuth2/XSUAA tokens automatically.
        Returns empty dicts if no auth is configured for this spec_id.
        """
        config = self.get_config(spec_id)
        if not config or config.auth_type == "none":
            return {}, {}, {}

        t = config.auth_type

        if t in ("xsuaa", "oauth2"):
            token = await self._fetch_oauth_token(spec_id, config)
            return {"Authorization": f"Bearer {token}"}, {}, {}

        if t == "bearer_static":
            return {"Authorization": f"Bearer {config.bearer_token}"}, {}, {}

        if t == "basic":
            cred = base64.b64encode(
                f"{config.username}:{config.password}".encode()
            ).decode()
            return {"Authorization": f"Basic {cred}"}, {}, {}

        if t == "apikey":
            if config.api_key_in == "query":
                return {}, {config.api_key_name: config.api_key}, {}
            if config.api_key_in == "cookie":
                return {}, {}, {config.api_key_name: config.api_key}
            return {config.api_key_name: config.api_key}, {}, {}

        return {}, {}, {}

    async def _fetch_oauth_token(self, spec_id: str, config: AuthConfig) -> str:
        """Return a valid OAuth2 / XSUAA access token, refreshing if needed."""
        async with self._lock:
            cached = self._token_cache.get(spec_id)
            # Keep 60-second buffer before expiry
            if cached and cached.expires_at > 0 and time.time() < cached.expires_at - 60:
                return cached.access_token

            token_url = config.token_url_override or f"{config.auth_url}/oauth/token"

            async with httpx.AsyncClient(timeout=30, verify=False) as client:
                resp = await client.post(
                    token_url,
                    data={"grant_type": config.grant_type},
                    auth=(config.client_id, config.client_secret),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                resp.raise_for_status()
                data = resp.json()

            access_token = data.get("access_token", "")
            if not access_token:
                raise ValueError(f"Token response missing 'access_token': {data}")

            expires_in = int(data.get("expires_in", 43200))
            self._token_cache[spec_id] = _CachedToken(
                access_token=access_token,
                expires_at=time.time() + expires_in,
            )
            return access_token

    def token_status(self, spec_id: str) -> dict:
        """Return current token cache status for a spec (no secret values)."""
        cached = self._token_cache.get(spec_id)
        if not cached:
            return {"cached": False}
        remaining = max(0, cached.expires_at - time.time()) if cached.expires_at > 0 else -1
        return {
            "cached": True,
            "expires_in_seconds": int(remaining) if remaining >= 0 else "static (no expiry)",
            "expired": remaining == 0,
        }

    def invalidate(self, spec_id: str):
        """Force token refresh on next call."""
        self._token_cache.pop(spec_id, None)


# ── Singleton ──────────────────────────────────────────────────────────────────

auth_manager = AuthManager()

"""
Execute HTTP calls for OpenAPI operations.
Handles path/query/header parameters, request bodies, and all auth schemes.

Auth is applied in two layers:
  1. Caller-provided auth_headers/params/cookies (explicit, takes precedence).
  2. auth_manager — reads credentials from env vars, fetches/refreshes tokens automatically.
     Tokens are never passed through the API; they live in .env on the server only.
"""

import base64
import json
import logging
from typing import Any, Optional
from urllib.parse import quote

import httpx

from openapi_parser import OpenAPIOperation, OpenAPISpec

_logger = logging.getLogger("openapi-executor")


class OpenAPIExecutor:
    """Executes HTTP calls described by OpenAPI operations."""

    async def execute(
        self,
        operation: OpenAPIOperation,
        spec: OpenAPISpec,
        args: dict[str, Any],
        auth_headers: dict[str, str],
        auth_params: dict[str, str],
        auth_cookies: dict[str, str],
        spec_id: str,
        base_url_override: str = "",
    ) -> Any:
        # Auto-apply credentials from env vars (auth_manager) if configured.
        # Caller-provided auth overrides env-var auth (explicit > auto).
        if spec_id:
            try:
                from auth_manager import auth_manager  # lazy import avoids circular deps
                mgr_hdrs, mgr_prms, mgr_ckies = await auth_manager.get_auth_headers(spec_id)
                auth_headers = {**mgr_hdrs, **auth_headers}
                auth_params  = {**mgr_prms,  **auth_params}
                auth_cookies = {**mgr_ckies, **auth_cookies}
            except ImportError:
                pass
            except Exception as exc:
                _logger.warning("auth_manager failed for '%s': %s", spec_id, exc)

        base_url = (base_url_override or spec.base_url).rstrip("/")

        # Bucket each arg by its parameter location
        param_loc = operation.param_locations()
        body_field_names: set[str] = set()
        if operation.request_body_schema:
            body_field_names = set(operation.request_body_schema.keys())

        path_args: dict[str, Any] = {}
        query_args: dict[str, Any] = {}
        header_args: dict[str, Any] = {}
        body_args: dict[str, Any] = {}

        for key, val in args.items():
            if val is None:
                continue
            loc = param_loc.get(key)
            if loc == "path":
                path_args[key] = val
            elif loc == "query":
                query_args[key] = val
            elif loc == "header":
                header_args[key] = val
            elif loc == "cookie":
                pass  # handled below as special cookie param
            elif key == operation.body_param_name or key in body_field_names:
                body_args[key] = val
            else:
                # Unknown arg: treat as query for read methods, body for write methods
                if operation.method in ("GET", "DELETE", "HEAD"):
                    query_args[key] = val
                else:
                    body_args[key] = val

        # Build URL with path substitution
        path = operation.path
        for key, val in path_args.items():
            path = path.replace(f"{{{key}}}", _url_encode_path(val))
        url = f"{base_url}{path}"

        # Build request headers
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **auth_headers,
        }
        for key, val in header_args.items():
            headers[key] = str(val)

        # Build query params (auth params merged in, explicit args take precedence)
        query_params: dict[str, Any] = {**auth_params, **query_args}

        # Build request body
        body_json: Any = None
        if body_args:
            if operation.body_param_name and operation.body_param_name in body_args:
                raw = body_args[operation.body_param_name]
                if isinstance(raw, str):
                    try:
                        body_json = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        # Treat as plain text — switch content type
                        headers["Content-Type"] = "text/plain"
                        body_json = raw
                else:
                    body_json = raw
            else:
                body_json = body_args

        # Cookies: auth cookies + any cookie-location params
        cookies: dict[str, str] = dict(auth_cookies)
        for p in operation.parameters:
            if p.location == "cookie" and p.name in args and args[p.name] is not None:
                cookies[p.name] = str(args[p.name])

        try:
            return await self._dispatch(
                method=operation.method,
                url=url,
                headers=headers,
                query_params=query_params,
                body_json=body_json,
                cookies=cookies,
            )
        except httpx.HTTPStatusError as exc:
            # On 401, force-refresh the token and retry once
            if exc.response.status_code == 401 and spec_id:
                try:
                    from auth_manager import auth_manager
                    if auth_manager.is_configured(spec_id):
                        auth_manager.invalidate(spec_id)
                        fresh_hdrs, fresh_prms, fresh_ckies = await auth_manager.get_auth_headers(spec_id)
                        headers.update(fresh_hdrs)
                        query_params.update(fresh_prms)
                        cookies.update(fresh_ckies)
                        _logger.info("Token refreshed for '%s' — retrying after 401", spec_id)
                        return await self._dispatch(
                            method=operation.method,
                            url=url,
                            headers=headers,
                            query_params=query_params,
                            body_json=body_json,
                            cookies=cookies,
                        )
                except ImportError:
                    pass
                except Exception as refresh_exc:
                    _logger.warning("Token refresh failed for '%s': %s", spec_id, refresh_exc)
            raise

    async def _dispatch(
        self,
        method: str,
        url: str,
        headers: dict,
        query_params: dict,
        body_json: Any,
        cookies: dict,
    ) -> Any:
        async with httpx.AsyncClient(timeout=60, verify=False, follow_redirects=True) as client:
            kwargs: dict[str, Any] = {
                "headers": headers,
                "params": query_params or None,
                "cookies": cookies or None,
            }

            m = method.upper()
            if m in ("POST", "PUT", "PATCH"):
                if isinstance(body_json, str) and headers.get("Content-Type", "").startswith("text/"):
                    kwargs["content"] = body_json.encode()
                elif body_json is not None:
                    kwargs["json"] = body_json
                resp = await getattr(client, m.lower())(url, **kwargs)
            elif m == "GET":
                resp = await client.get(url, **kwargs)
            elif m == "DELETE":
                if body_json is not None:
                    kwargs["json"] = body_json
                resp = await client.delete(url, **kwargs)
            elif m == "HEAD":
                resp = await client.head(url, **kwargs)
            elif m == "OPTIONS":
                resp = await client.options(url, **kwargs)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

        resp.raise_for_status()

        if not resp.content:
            return {"status": "success", "statusCode": resp.status_code}

        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            return resp.json()
        return {
            "text": resp.text,
            "statusCode": resp.status_code,
            "contentType": content_type,
        }


def _url_encode_path(value: Any) -> str:
    """Percent-encode a path segment value."""
    return quote(str(value), safe="")


def build_openapi_auth(
    spec: OpenAPISpec,
    api_key: str = "",
    api_key_name: str = "",
    bearer_token: str = "",
    username: str = "",
    password: str = "",
    oauth2_token: str = "",
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """
    Determine (auth_headers, auth_query_params, auth_cookies) from the spec's
    security schemes and the provided credentials.

    Matches each provided credential to the appropriate scheme type and location.
    Falls back to applying credentials directly when no matching scheme is found.

    Returns:
        (auth_headers, auth_query_params, auth_cookies)
    """
    auth_headers: dict[str, str] = {}
    auth_params: dict[str, str] = {}
    auth_cookies: dict[str, str] = {}

    applied: set[str] = set()  # track which credential types were applied via scheme

    for scheme in spec.security_schemes.values():
        t = scheme.type.lower()

        if t == "apikey" and api_key and "apikey" not in applied:
            actual_name = api_key_name or scheme.param_name or "api_key"
            if scheme.in_ == "header":
                auth_headers[actual_name] = api_key
            elif scheme.in_ == "query":
                auth_params[actual_name] = api_key
            elif scheme.in_ == "cookie":
                auth_cookies[actual_name] = api_key
            applied.add("apikey")

        elif t == "http":
            http_scheme = scheme.http_scheme.lower()
            if http_scheme in ("bearer", "") and bearer_token and "bearer" not in applied:
                auth_headers["Authorization"] = f"Bearer {bearer_token}"
                applied.add("bearer")
            elif http_scheme == "basic" and username and password and "basic" not in applied:
                cred = base64.b64encode(f"{username}:{password}".encode()).decode()
                auth_headers["Authorization"] = f"Basic {cred}"
                applied.add("basic")

        elif t == "basic" and username and password and "basic" not in applied:
            cred = base64.b64encode(f"{username}:{password}".encode()).decode()
            auth_headers["Authorization"] = f"Basic {cred}"
            applied.add("basic")

        elif t == "oauth2":
            token = oauth2_token or bearer_token
            if token and "oauth2" not in applied:
                auth_headers["Authorization"] = f"Bearer {token}"
                applied.add("oauth2")

        elif t == "openidconnect":
            token = bearer_token or oauth2_token
            if token and "oidc" not in applied:
                auth_headers["Authorization"] = f"Bearer {token}"
                applied.add("oidc")

    # Fallback: apply credentials even when no matching scheme was found in spec
    if not auth_headers and not auth_params and not auth_cookies:
        if bearer_token or oauth2_token:
            auth_headers["Authorization"] = f"Bearer {bearer_token or oauth2_token}"
        elif username and password:
            cred = base64.b64encode(f"{username}:{password}".encode()).decode()
            auth_headers["Authorization"] = f"Basic {cred}"
        elif api_key:
            name = api_key_name or "Authorization"
            auth_headers[name] = api_key

    return auth_headers, auth_params, auth_cookies

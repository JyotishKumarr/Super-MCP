"""
Execute OData REST API calls for list / get / create / update / delete operations.
Handles CSRF token fetching for SAP OData v2 services.
"""

import json
import httpx
from typing import Any, Optional

from odata_parser import ODataSpec, build_key_predicate
from rbac_manager import rbac, WRITE_ODATA_OPS


class ODataExecutor:
    def __init__(self):
        # Per-spec state
        self._csrf_tokens: dict[str, str] = {}   # spec_id -> csrf_token
        self._csrf_cookies: dict[str, dict] = {}  # spec_id -> cookies

    def _build_headers(self, auth_headers: dict, extra: Optional[dict] = None) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **auth_headers,
        }
        if extra:
            headers.update(extra)
        return headers

    async def _fetch_csrf_token(self, base_url: str, auth_headers: dict, spec_id: str):
        """Fetch an X-CSRF-Token from SAP OData (needed for write operations)."""
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            resp = await client.get(
                base_url,
                headers={**auth_headers, "X-CSRF-Token": "Fetch", "Accept": "application/json"},
            )
            token = resp.headers.get("x-csrf-token") or resp.headers.get("X-CSRF-Token")
            if token:
                self._csrf_tokens[spec_id] = token
                self._csrf_cookies[spec_id] = dict(resp.cookies)

    def _build_query_params(self, args: dict) -> dict:
        """Map friendly arg names to $-prefixed OData query options."""
        mapping = {
            "filter": "$filter",
            "select": "$select",
            "orderby": "$orderby",
            "top": "$top",
            "skip": "$skip",
            "expand": "$expand",
            "count": "$count",
            "search": "$search",
        }
        params = {}
        for key, odata_key in mapping.items():
            if key in args and args[key] is not None:
                val = args[key]
                if isinstance(val, bool):
                    val = "true" if val else "false"
                params[odata_key] = val
        return params

    async def execute(
        self,
        operation: str,
        spec: ODataSpec,
        entity_set_name: str,
        args: dict,
        auth_headers: dict,
        spec_id: str,
    ) -> Any:
        if operation in WRITE_ODATA_OPS and not rbac.can_write():
            role = rbac.get_role()
            return {
                "error": (
                    f"Operation '{operation}' requires the 'admin' role. "
                    f"Current role: '{role.value}'. "
                    "Set MCP_USER_ROLE=admin or add your email to RBAC_EMAIL_ROLES."
                ),
                "required_role": "admin",
            }

        base_url = spec.service_url.rstrip("/")

        if operation == "list":
            return await self._list(base_url, entity_set_name, args, auth_headers)

        elif operation == "get":
            et = spec.resolve_entity_type(entity_set_name)
            key_pred = build_key_predicate(et, args)
            return await self._get(base_url, entity_set_name, key_pred, args, auth_headers)

        elif operation == "create":
            await self._ensure_csrf(base_url, auth_headers, spec_id)
            return await self._create(base_url, entity_set_name, args, auth_headers, spec_id)

        elif operation == "update":
            et = spec.resolve_entity_type(entity_set_name)
            key_props = et.key_properties if et else []
            key_args = {k: args[k] for k in key_props if k in args}
            body_args = {k: v for k, v in args.items() if k not in key_props}
            key_pred = build_key_predicate(et, key_args)
            await self._ensure_csrf(base_url, auth_headers, spec_id)
            return await self._update(base_url, entity_set_name, key_pred, body_args, auth_headers, spec_id)

        elif operation == "delete":
            et = spec.resolve_entity_type(entity_set_name)
            key_pred = build_key_predicate(et, args)
            await self._ensure_csrf(base_url, auth_headers, spec_id)
            return await self._delete(base_url, entity_set_name, key_pred, auth_headers, spec_id)

        else:
            raise ValueError(f"Unknown operation: {operation}")

    async def _ensure_csrf(self, base_url: str, auth_headers: dict, spec_id: str):
        if spec_id not in self._csrf_tokens:
            await self._fetch_csrf_token(base_url, auth_headers, spec_id)

    async def _list(self, base_url: str, es_name: str, args: dict, auth_headers: dict) -> dict:
        url = f"{base_url}/{es_name}"
        params = self._build_query_params(args)
        async with httpx.AsyncClient(timeout=60, verify=False) as client:
            resp = await client.get(url, headers=self._build_headers(auth_headers), params=params)
            resp.raise_for_status()
            return resp.json()

    async def _get(self, base_url: str, es_name: str, key_pred: str, args: dict, auth_headers: dict) -> dict:
        url = f"{base_url}/{es_name}{key_pred}"
        params = {}
        for opt in ("expand", "select"):
            if opt in args and args[opt]:
                params[f"${opt}"] = args[opt]
        async with httpx.AsyncClient(timeout=60, verify=False) as client:
            resp = await client.get(url, headers=self._build_headers(auth_headers), params=params)
            resp.raise_for_status()
            return resp.json()

    async def _create(self, base_url: str, es_name: str, args: dict, auth_headers: dict, spec_id: str) -> dict:
        url = f"{base_url}/{es_name}"
        extra_headers = {}
        if spec_id in self._csrf_tokens:
            extra_headers["X-CSRF-Token"] = self._csrf_tokens[spec_id]
        cookies = self._csrf_cookies.get(spec_id, {})
        async with httpx.AsyncClient(timeout=60, verify=False) as client:
            resp = await client.post(
                url,
                headers=self._build_headers(auth_headers, extra_headers),
                json=args,
                cookies=cookies,
            )
            resp.raise_for_status()
            if resp.content:
                return resp.json()
            return {"status": "created", "statusCode": resp.status_code}

    async def _update(self, base_url: str, es_name: str, key_pred: str, body: dict, auth_headers: dict, spec_id: str) -> dict:
        url = f"{base_url}/{es_name}{key_pred}"
        extra_headers = {}
        if spec_id in self._csrf_tokens:
            extra_headers["X-CSRF-Token"] = self._csrf_tokens[spec_id]
        cookies = self._csrf_cookies.get(spec_id, {})
        async with httpx.AsyncClient(timeout=60, verify=False) as client:
            resp = await client.patch(
                url,
                headers=self._build_headers(auth_headers, extra_headers),
                json=body,
                cookies=cookies,
            )
            resp.raise_for_status()
            if resp.content:
                return resp.json()
            return {"status": "updated", "statusCode": resp.status_code}

    async def _delete(self, base_url: str, es_name: str, key_pred: str, auth_headers: dict, spec_id: str) -> dict:
        url = f"{base_url}/{es_name}{key_pred}"
        extra_headers = {}
        if spec_id in self._csrf_tokens:
            extra_headers["X-CSRF-Token"] = self._csrf_tokens[spec_id]
        cookies = self._csrf_cookies.get(spec_id, {})
        async with httpx.AsyncClient(timeout=60, verify=False) as client:
            resp = await client.delete(
                url,
                headers=self._build_headers(auth_headers, extra_headers),
                cookies=cookies,
            )
            resp.raise_for_status()
            return {"status": "deleted", "statusCode": resp.status_code}

"""
Role-Based Access Control for OData MCP Server.

Two roles:
  default  – Read-only: list and get operations only.
  admin    – Full access: list, get, create, update, delete.

Configuration (environment variables — no external auth tool required):
──────────────────────────────────────────────────────────────────────
  MCP_USER_ROLE=admin|default
      Single-role mode. Entire server instance runs as this role.
      Best for stdio (Claude Desktop / Claude Code), one person per server.
      Example:  MCP_USER_ROLE=admin

  MCP_USER_EMAIL=user@company.com
      Declare who the current server instance belongs to.
      Combined with RBAC_EMAIL_ROLES to derive their role.
      Example:  MCP_USER_EMAIL=alice@acme.com

  RBAC_EMAIL_ROLES=email1:role,email2:role,...
      Map email addresses to roles. Used with MCP_USER_EMAIL.
      Example:  RBAC_EMAIL_ROLES=alice@acme.com:admin,bob@acme.com:default

  RBAC_DOMAIN_ROLES=domain:role,...
      Grant a role to everyone at a domain (catch-all within that domain).
      Evaluated after exact email match.
      Example:  RBAC_DOMAIN_ROLES=acme.com:admin,partner.com:default

  RBAC_USERS=key1:role,key2:role,...
      Map opaque API keys to roles (for HTTP/SSE multi-user transport).
      Callers pass their key in the X-API-Key request header.
      Example:  RBAC_USERS=secret_abc123:admin,readonly_xyz:default

Resolution order (first match wins):
  1. MCP_USER_ROLE          — explicit override for the whole server
  2. MCP_USER_EMAIL exact match in RBAC_EMAIL_ROLES
  3. MCP_USER_EMAIL domain  match in RBAC_DOMAIN_ROLES
  4. X-API-Key header       match in RBAC_USERS  (HTTP/SSE transport)
  5. Fallback               → 'default' role (least privilege)
"""

from __future__ import annotations

import functools
import json
import os
from enum import Enum
from typing import Optional


class Role(str, Enum):
    DEFAULT = "default"
    ADMIN = "admin"


# Operations that mutate data — require admin role
WRITE_ODATA_OPS: frozenset[str] = frozenset({"create", "update", "delete"})
WRITE_HTTP_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class PermissionDenied(Exception):
    """Raised when an operation is not permitted for the caller's role."""


class RBACManager:
    """
    Singleton role resolver and permission enforcer.

    Reads all configuration from environment variables — no external auth
    service, database, or library required.
    """

    def __init__(self) -> None:
        self._server_role: Optional[Role] = None
        self._server_email: Optional[str] = None
        self._email_roles: dict[str, Role] = {}   # exact email  → role
        self._domain_roles: dict[str, Role] = {}  # domain suffix → role
        self._api_key_roles: dict[str, Role] = {} # opaque key   → role
        self._reload()

    # ── Config loading ────────────────────────────────────────────────────────

    def _reload(self) -> None:
        """Read all RBAC config from environment variables."""
        # 1. Server-wide role override
        raw = os.environ.get("MCP_USER_ROLE", "").strip().lower()
        self._server_role = Role(raw) if raw in ("admin", "default") else None

        # 2. Server email identity
        self._server_email = os.environ.get("MCP_USER_EMAIL", "").strip().lower() or None

        # 3. Email → role exact mapping
        self._email_roles.clear()
        for entry in os.environ.get("RBAC_EMAIL_ROLES", "").split(","):
            email, role = _parse_entry(entry)
            if email and role:
                self._email_roles[email.lower()] = role

        # 4. Domain → role mapping  (e.g. "acme.com:admin")
        self._domain_roles.clear()
        for entry in os.environ.get("RBAC_DOMAIN_ROLES", "").split(","):
            domain, role = _parse_entry(entry)
            if domain and role:
                self._domain_roles[domain.lower().lstrip("@")] = role

        # 5. Opaque API key → role  (for HTTP transport)
        self._api_key_roles.clear()
        for entry in os.environ.get("RBAC_USERS", "").split(","):
            key, role = _parse_entry(entry)
            if key and role:
                self._api_key_roles[key] = role

    # ── Role resolution ───────────────────────────────────────────────────────

    def get_role(self, api_key: Optional[str] = None) -> Role:
        """
        Resolve the effective role for a caller.

        Resolution order:
          1. MCP_USER_ROLE env var (server-wide, overrides all)
          2. MCP_USER_EMAIL exact match in RBAC_EMAIL_ROLES
          3. MCP_USER_EMAIL domain  match in RBAC_DOMAIN_ROLES
          4. api_key match in RBAC_USERS (HTTP transport)
          5. Fallback → default (least privilege)
        """
        # 1. Explicit server-wide role wins
        if self._server_role is not None:
            return self._server_role

        # 2 & 3. Email-based resolution (server identity)
        if self._server_email:
            role = self._role_for_email(self._server_email)
            if role is not None:
                return role

        # 4. API key lookup (HTTP/SSE multi-user mode)
        if api_key and api_key in self._api_key_roles:
            return self._api_key_roles[api_key]

        return Role.DEFAULT  # 5. Least-privilege fallback

    def _role_for_email(self, email: str) -> Optional[Role]:
        """Return role for an email via exact match then domain match."""
        email = email.lower()
        if email in self._email_roles:
            return self._email_roles[email]
        # Domain match: "alice@acme.com" → domain "acme.com"
        if "@" in email:
            domain = email.split("@", 1)[1]
            if domain in self._domain_roles:
                return self._domain_roles[domain]
        return None

    def is_admin(self, api_key: Optional[str] = None) -> bool:
        return self.get_role(api_key) == Role.ADMIN

    def can_write(self, api_key: Optional[str] = None) -> bool:
        """Return True only for admin role."""
        return self.is_admin(api_key)

    def has_role(self, role: str, api_key: Optional[str] = None) -> bool:
        """
        Return True if the current caller holds at least the given role.

        Role hierarchy (least → most privileged): default < admin.
        Passing role='default' always returns True (every user has at least default).
        Passing role='admin' returns True only for admin users.
        Unknown role strings are treated as admin-level (fail-safe).
        """
        if role == Role.DEFAULT.value:
            return True
        if role == Role.ADMIN.value:
            return self.is_admin(api_key)
        return self.is_admin(api_key)

    # ── Permission checks ─────────────────────────────────────────────────────

    def check_odata(self, operation: str, api_key: Optional[str] = None) -> None:
        """
        Guard an OData operation.
        Raises PermissionDenied for create/update/delete when not admin.
        """
        if operation in WRITE_ODATA_OPS and not self.can_write(api_key):
            role = self.get_role(api_key)
            raise PermissionDenied(
                f"Operation '{operation}' requires the 'admin' role. "
                f"Current role: '{role.value}'. "
                "To gain write access set MCP_USER_ROLE=admin, "
                "add your email to RBAC_EMAIL_ROLES, or use an admin API key."
            )

    def check_http(self, method: str, api_key: Optional[str] = None) -> None:
        """
        Guard an HTTP method.
        Raises PermissionDenied for POST/PUT/PATCH/DELETE when not admin.
        """
        if method.upper() in WRITE_HTTP_METHODS and not self.can_write(api_key):
            role = self.get_role(api_key)
            raise PermissionDenied(
                f"HTTP {method.upper()} requires the 'admin' role. "
                f"Current role: '{role.value}'. "
                "To gain write access set MCP_USER_ROLE=admin, "
                "add your email to RBAC_EMAIL_ROLES, or use an admin API key."
            )

    # ── Introspection ─────────────────────────────────────────────────────────

    def status(self) -> dict:
        """
        Return a safe status snapshot — never exposes API keys or secret values.
        """
        role = self.get_role()
        return {
            "mode": _detect_mode(self),
            "effective_role": role.value,
            "server_role_override": self._server_role.value if self._server_role else None,
            "server_email": self._server_email,
            "email_rules_count": len(self._email_roles),
            "domain_rules_count": len(self._domain_roles),
            "api_key_users_count": len(self._api_key_roles),
            "write_odata_ops": sorted(WRITE_ODATA_OPS),
            "write_http_methods": sorted(WRITE_HTTP_METHODS),
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_entry(entry: str) -> tuple[str, Optional[Role]]:
    """Parse 'key:role' entry; returns (key, Role) or ('', None) on bad input."""
    entry = entry.strip()
    if ":" not in entry:
        return "", None
    key, role_str = entry.rsplit(":", 1)
    key = key.strip()
    role_str = role_str.strip().lower()
    if not key or role_str not in ("admin", "default"):
        return "", None
    return key, Role(role_str)


def _detect_mode(mgr: RBACManager) -> str:
    if mgr._server_role:
        return "single-role (MCP_USER_ROLE)"
    if mgr._server_email:
        return "email-based (MCP_USER_EMAIL + RBAC_EMAIL_ROLES/RBAC_DOMAIN_ROLES)"
    if mgr._api_key_roles:
        return "api-key (RBAC_USERS)"
    return "default (no config — read-only)"


# ── Module-level singleton ────────────────────────────────────────────────────
rbac = RBACManager()


# ── Middleware decorator ──────────────────────────────────────────────────────

def requires_role(role: str):
    """
    Decorator that gates an MCP tool to callers holding the specified role.

    Usage:
        @mcp.tool()
        @requires_role("admin")
        async def my_tool(...) -> str:
            ...

    The decorated function gains a ``_required_role`` attribute that tools
    like ``whoami`` and ``list_generated_tools`` can surface for introspection.

    Supported roles (same strings as MCP_USER_ROLE env var):
        "default"  — any authenticated caller (read-only baseline)
        "admin"    — full write access

    The wrapper always returns a JSON-encoded error dict on denial so that
    the caller receives a structured response rather than an exception.
    """
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            if not rbac.has_role(role):
                current = rbac.get_role()
                return json.dumps({
                    "error": (
                        f"This operation requires the '{role}' role. "
                        f"Current role: '{current.value}'. "
                        "Set MCP_USER_ROLE=admin or add your email to RBAC_EMAIL_ROLES."
                    ),
                    "required_role": role,
                })
            return await fn(*args, **kwargs)

        wrapper._required_role = role
        return wrapper
    return decorator

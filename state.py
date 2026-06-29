"""
Shared in-memory state for both the MCP server and the REST API.
Holds loaded OData and OpenAPI specs, generated tool definitions, and per-spec auth.
"""

from dataclasses import dataclass
from odata_parser import ODataSpec


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict
    spec_id: str
    entity_set: str
    operation: str      # list | get | create | update | delete | action_invoke
    key_props: list[str]
    http_method: str = "GET"


class AppState:
    """Singleton state shared across the application."""

    def __init__(self):
        # OData
        self.specs: dict[str, ODataSpec] = {}
        self.xml_cache: dict[str, str] = {}

        # OpenAPI
        self.openapi_specs: dict = {}     # spec_id -> OpenAPISpec
        self.openapi_cache: dict[str, str] = {}  # spec_id -> raw spec content

        # Shared tool registry (used by REST API; MCP server uses _tool_manager directly)
        self.tools: dict[str, ToolDefinition] = {}

        # Auth — per spec_id, keyed the same whether OData or OpenAPI
        self.auth_headers: dict[str, dict] = {}   # HTTP headers (Authorization, X-API-Key, ...)
        self.auth_params: dict[str, dict] = {}    # Query-param API keys
        self.auth_cookies: dict[str, dict] = {}   # Cookie-based auth

    def clear_spec(self, spec_id: str):
        """Remove an OData spec and all associated state."""
        self.specs.pop(spec_id, None)
        self.xml_cache.pop(spec_id, None)
        self.auth_headers.pop(spec_id, None)
        self.auth_params.pop(spec_id, None)
        self.auth_cookies.pop(spec_id, None)
        to_remove = [k for k, v in self.tools.items() if v.spec_id == spec_id]
        for k in to_remove:
            del self.tools[k]

    def clear_openapi_spec(self, spec_id: str):
        """Remove an OpenAPI spec and all associated state."""
        self.openapi_specs.pop(spec_id, None)
        self.openapi_cache.pop(spec_id, None)
        self.auth_headers.pop(spec_id, None)
        self.auth_params.pop(spec_id, None)
        self.auth_cookies.pop(spec_id, None)
        to_remove = [k for k, v in self.tools.items() if v.spec_id == spec_id]
        for k in to_remove:
            del self.tools[k]

    def tools_for_spec(self, spec_id: str) -> dict[str, ToolDefinition]:
        return {k: v for k, v in self.tools.items() if v.spec_id == spec_id}


# Global singleton
app_state = AppState()

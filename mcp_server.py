"""
OData MCP Server — built with FastMCP.

Exposes 5 static tools plus dynamically registered CRUD tools per EntitySet
after a spec is loaded.  Uses SAP AI Core (GPT-4o) for intelligent analysis.

Transports:
    python3 mcp_server.py                  # stdio (Claude Desktop / Claude Code)
    python3 mcp_server.py --sse [PORT]     # SSE  http://localhost:8000/sse
    python3 mcp_server.py --http [PORT]    # Streamable-HTTP  http://localhost:8000/mcp
"""

import asyncio
import base64
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

# Load .env from the same directory as this file (must happen before any config imports)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file, override=False)  # override=False keeps real env vars intact

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools import Tool
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.server.transport_security import TransportSecuritySettings
from datetime import datetime
from pydantic import create_model

from config import SAP_AI_CONFIG
from odata_executor import ODataExecutor
from odata_parser import ODataSpec, _sanitize_name, parse_odata_metadata
from openapi_executor import OpenAPIExecutor, build_openapi_auth
from openapi_parser import OpenAPIOperation, OpenAPISpec, parse_openapi_spec
from sap_ai_client import SAPAIClient
from state import app_state
import tool_generator
from rbac_manager import rbac, PermissionDenied, WRITE_HTTP_METHODS, WRITE_ODATA_OPS, requires_role

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

# ── Core services ──────────────────────────────────────────────────────────────

mcp = FastMCP(
    "OData MCP Server",
    instructions=(
        "This server dynamically generates tools from OData APIs. "
        "Call load_odata_spec first to register entity tools, "
        "then use the generated tools to query the service."
    ),
)

ai_client = SAPAIClient(SAP_AI_CONFIG)
executor = ODataExecutor()
openapi_executor = OpenAPIExecutor()

# ── JSON Schema → Pydantic arg model ──────────────────────────────────────────

_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def _schema_to_arg_model(schema: dict) -> type:
    """Build a dynamic Pydantic ArgModelBase subclass from a JSON Schema object."""
    props = schema.get("properties", {})
    required_fields = set(schema.get("required", []))
    field_defs: dict[str, Any] = {}

    for field_name, prop in props.items():
        py_type = _JSON_TYPE_MAP.get(prop.get("type", "string"), str)
        if field_name in required_fields:
            field_defs[field_name] = (py_type, ...)
        else:
            field_defs[field_name] = (Optional[py_type], None)

    model_name = f"Args_{'_'.join(props.keys())[:40]}"
    return create_model(model_name, **field_defs, __base__=ArgModelBase)


# ── Dynamic tool injection ─────────────────────────────────────────────────────

def _inject_tool(name: str, description: str, schema: dict, handler) -> None:
    """
    Directly construct a FastMCP Tool with a custom JSON Schema and inject it
    into the server's tool manager — bypassing signature introspection.
    """
    arg_model = _schema_to_arg_model(schema)
    func_meta = FuncMetadata(arg_model=arg_model)

    tool = Tool(
        fn=handler,
        name=name,
        description=description,
        parameters=schema,
        fn_metadata=func_meta,
        is_async=asyncio.iscoroutinefunction(handler),
        title=None,
        context_kwarg=None,
        annotations=None,
    )
    mcp._tool_manager._tools[name] = tool


def _make_odata_handler(spec_id: str, es_name: str, operation: str):
    """Return an async handler closure for a specific OData operation."""

    required = "admin" if operation in WRITE_ODATA_OPS else "default"

    async def handler(**kwargs: Any) -> str:
        spec = app_state.specs.get(spec_id)
        if not spec:
            return json.dumps({"error": f"Spec '{spec_id}' not loaded."})
        auth_headers = app_state.auth_headers.get(spec_id, {})
        try:
            result = await executor.execute(
                operation=operation,
                spec=spec,
                entity_set_name=es_name,
                args=kwargs,
                auth_headers=auth_headers,
                spec_id=spec_id,
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    handler.__name__ = f"{_sanitize_name(spec_id)}__{_sanitize_name(es_name)}__{operation}"
    return requires_role(required)(handler)


def _register_entity_tools(spec_id: str, es_name: str, spec: ODataSpec, ai_desc: str = "") -> None:
    et = spec.resolve_entity_type(es_name)
    prefix = f"{_sanitize_name(spec_id)}__{_sanitize_name(es_name)}"

    key_schema = et.key_property_schema() if et else {"key": {"type": "string", "description": "Entity key"}}
    all_props = et.all_property_schema() if et else {}
    non_key = et.all_property_schema(exclude_keys=True) if et else {}
    key_list = list(key_schema.keys())
    entity_label = ai_desc or f"OData entity '{es_name}'"

    ops = [
        (
            f"{prefix}__list",
            f"List {es_name} records. {entity_label}",
            {
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "description": "OData $filter (e.g. \"Name eq 'SAP'\")"},
                    "select": {"type": "string", "description": "Comma-separated fields to return"},
                    "orderby": {"type": "string", "description": "Sort expression (e.g. 'CreatedAt desc')"},
                    "top": {"type": "integer", "description": "Max records to return", "minimum": 1},
                    "skip": {"type": "integer", "description": "Records to skip (pagination)", "minimum": 0},
                    "expand": {"type": "string", "description": "Navigation properties to expand"},
                    "count": {"type": "boolean", "description": "Include total count"},
                    "search": {"type": "string", "description": "Full-text search ($search)"},
                },
            },
            "list",
        ),
        (
            f"{prefix}__get",
            f"Get a single {es_name} record by key. {entity_label}",
            {
                "type": "object",
                "properties": {
                    **key_schema,
                    "expand": {"type": "string", "description": "Navigation properties to expand"},
                    "select": {"type": "string", "description": "Fields to return"},
                },
                "required": key_list,
            },
            "get",
        ),
        # Write operations — visible to all, but execution blocked for non-admin at runtime
        (
            f"{prefix}__create",
            f"[ADMIN] Create a new {es_name} record. {entity_label}",
            {"type": "object", "properties": all_props},
            "create",
        ),
        (
            f"{prefix}__update",
            f"[ADMIN] Update an existing {es_name} record (PATCH). {entity_label}",
            {
                "type": "object",
                "properties": {**key_schema, **non_key},
                "required": key_list,
            },
            "update",
        ),
        (
            f"{prefix}__delete",
            f"[ADMIN] Delete a {es_name} record by key. {entity_label}",
            {"type": "object", "properties": key_schema, "required": key_list},
            "delete",
        ),
    ]

    for tool_name, desc, schema, op in ops:
        handler = _make_odata_handler(spec_id, es_name, op)
        _inject_tool(tool_name, desc, schema, handler)


# ── OpenAPI helpers ────────────────────────────────────────────────────────────

def _openapi_op_schema(op: OpenAPIOperation) -> dict:
    """Build the JSON Schema for an OpenAPI operation's tool inputs."""
    properties: dict = {}
    required: list[str] = []

    for param in op.parameters:
        if param.location == "header":
            continue  # auth headers injected automatically
        prop = dict(param.json_schema)
        prop["description"] = param.description or f"{param.name} ({param.location} parameter)"
        # Avoid name collision with body fields by prefixing (rare but possible)
        name = param.name
        if name in properties:
            name = f"param_{name}"
        properties[name] = prop
        if param.required:
            required.append(name)

    if op.request_body_schema:
        for field_name, field_schema in op.request_body_schema.items():
            prop = dict(field_schema)
            if "description" not in prop:
                prop["description"] = f"{field_name} (request body field)"
            # Suffix to avoid collision with path/query params
            name = field_name if field_name not in properties else f"body_{field_name}"
            properties[name] = prop
    elif op.body_param_name:
        req_note = " (required)" if op.request_body_required else ""
        properties[op.body_param_name] = {
            "type": "string",
            "description": f"Request body as JSON string{req_note}",
        }
        if op.request_body_required:
            required.append(op.body_param_name)

    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _make_openapi_handler(spec_id: str, op_id: str, http_method: str):
    """Return an async handler closure for a specific OpenAPI operation."""

    required = "admin" if http_method.upper() in WRITE_HTTP_METHODS else "default"

    async def handler(**kwargs: Any) -> str:
        oapi_spec = app_state.openapi_specs.get(spec_id)
        if not oapi_spec:
            return json.dumps({"error": f"OpenAPI spec '{spec_id}' not loaded."})
        op = oapi_spec.operations.get(op_id)
        if not op:
            return json.dumps({"error": f"Operation '{op_id}' not found in spec '{spec_id}'."})
        try:
            result = await openapi_executor.execute(
                operation=op,
                spec=oapi_spec,
                args=kwargs,
                auth_headers=app_state.auth_headers.get(spec_id, {}),
                auth_params=app_state.auth_params.get(spec_id, {}),
                auth_cookies=app_state.auth_cookies.get(spec_id, {}),
                spec_id=spec_id,
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    handler.__name__ = f"{_sanitize_name(spec_id)}__{_sanitize_name(op_id)}"
    return requires_role(required)(handler)


def _register_openapi_tools(spec_id: str, spec: OpenAPISpec, annotations: dict = {}) -> None:
    """Inject one FastMCP tool per OpenAPI operation. All tools are registered;
    write operations are blocked at execution time for non-admin callers."""
    for op_id, op in spec.operations.items():
        tool_name = f"{_sanitize_name(spec_id)}__{op.operation_id}"
        ann = annotations.get(op_id, {})
        desc = ann.get("description") or op.summary or op.description or f"{op.method} {op.path}"
        if op.tags:
            desc = f"[{', '.join(op.tags)}] {desc}"
        # Mark write operations so users know they need admin role
        if op.method.upper() in WRITE_HTTP_METHODS:
            desc = f"[ADMIN] {desc}"
        schema = _openapi_op_schema(op)
        handler = _make_openapi_handler(spec_id, op_id, op.method)
        _inject_tool(tool_name, desc, schema, handler)


# ── Static tools ───────────────────────────────────────────────────────────────

@mcp.tool()
async def load_odata_spec(
    source: str,
    spec_id: str = "default",
    base_url: str = "",
    username: str = "",
    password: str = "",
    bearer_token: str = "",
    save_to_file: bool = False,
    use_ai_descriptions: bool = False,
) -> str:
    """
    Load an OData $metadata spec and auto-generate CRUD tools for every EntitySet.

    Args:
        source: URL to $metadata endpoint or raw XML string
        spec_id: Short ID to namespace the generated tools (e.g. 'bp', 'sales')
        base_url: Base service URL for API calls (inferred from source if omitted)
        username: HTTP Basic auth username (optional)
        password: HTTP Basic auth password (optional)
        bearer_token: Bearer token (optional)
        save_to_file: Write a persistent tools/{spec_id}.py file (survives restarts)
        use_ai_descriptions: Use GPT-4o to generate enriched tool annotations (slower)
    """
    sid = _sanitize_name(spec_id)

    # If already loaded, return existing state — call smart_query to use it
    if sid in app_state.specs:
        spec = app_state.specs[sid]
        tool_names = [k for k in mcp._tool_manager._tools if k.startswith(f"{sid}__")]
        return json.dumps({
            "status": "already_loaded",
            "spec_id": sid,
            "odata_version": spec.version,
            "namespace": spec.namespace,
            "base_url": spec.service_url,
            "entity_sets": list(spec.entity_sets.keys()),
            "tools_registered": len(tool_names),
            "note": "Spec already loaded. Use smart_query to query it.",
        }, indent=2)

    auth_headers: dict = {}
    if bearer_token:
        auth_headers["Authorization"] = f"Bearer {bearer_token}"
    elif username and password:
        cred = base64.b64encode(f"{username}:{password}".encode()).decode()
        auth_headers["Authorization"] = f"Basic {cred}"

    src = source.strip()
    if src.startswith("http"):
        async with httpx.AsyncClient(timeout=60, verify=False) as client:
            resp = await client.get(src, headers={**auth_headers, "Accept": "application/xml,text/xml"})
            resp.raise_for_status()
            xml_content = resp.text
        effective_base = base_url or re.sub(r"/?\$metadata.*$", "", src)
    else:
        xml_content = src
        effective_base = base_url

    spec = parse_odata_metadata(xml_content, service_url=effective_base)

    # Remove old tools for this spec
    app_state.clear_spec(sid)
    for tool_name in list(mcp._tool_manager._tools.keys()):
        if tool_name.startswith(f"{sid}__"):
            del mcp._tool_manager._tools[tool_name]

    app_state.specs[sid] = spec
    app_state.auth_headers[sid] = auth_headers
    app_state.xml_cache[sid] = xml_content

    # Generate AI annotations if requested
    annotations = {}
    if use_ai_descriptions:
        try:
            annotations = await tool_generator.generate_annotations(spec, ai_client)
        except Exception:
            pass

    # Register tools in memory
    for es_name in spec.entity_sets:
        ann = annotations.get(es_name, {})
        ai_desc = ann.get("entity", "")
        _register_entity_tools(sid, es_name, spec, ai_desc)

    tool_names = [k for k in mcp._tool_manager._tools if k.startswith(f"{sid}__")]

    # Optionally write persistent tool file
    file_path = None
    if save_to_file:
        content = tool_generator.generate_file_content(sid, spec, xml_content, effective_base, annotations)
        file_path = tool_generator.save_tool_file(sid, content)

    return json.dumps({
        "status": "ok",
        "spec_id": sid,
        "odata_version": spec.version,
        "namespace": spec.namespace,
        "base_url": effective_base,
        "entity_sets": list(spec.entity_sets.keys()),
        "tools_generated": len(tool_names),
        "file_saved": file_path,
        "note": "Refresh tool list to see the newly registered tools.",
    }, indent=2)


@mcp.tool()
async def load_openapi_spec(
    source: str,
    spec_id: str = "default",
    base_url: str = "",
    api_key: str = "",
    api_key_name: str = "",
    bearer_token: str = "",
    username: str = "",
    password: str = "",
    oauth2_token: str = "",
    save_to_file: bool = False,
    use_ai_descriptions: bool = False,
) -> str:
    """
    Load an OpenAPI / Swagger spec and auto-generate one tool per operation.

    Supports OpenAPI 3.x and Swagger 2.0. Input can be a URL (JSON or YAML)
    or raw spec content. Auth is auto-detected from the spec's securitySchemes
    and applied to every tool call.

    Args:
        source: URL to spec file (e.g. https://…/openapi.json) or raw JSON/YAML content
        spec_id: Short ID to namespace tools (e.g. 'petstore', 'github', 'jira')
        base_url: Override the base URL for API calls (optional)
        api_key: API key value — applied to the correct header/query param per spec
        api_key_name: Override the API key header/param name (auto-detected if omitted)
        bearer_token: Bearer token for Authorization header (http bearer / oauth2)
        username: HTTP Basic auth username
        password: HTTP Basic auth password
        oauth2_token: OAuth2 access token (used as Bearer if bearer_token is empty)
        save_to_file: Write tools/{spec_id}.py for persistence across server restarts
        use_ai_descriptions: Use GPT-4o to generate enriched tool descriptions (slower)
    """
    sid = _sanitize_name(spec_id)

    # If already loaded, return existing state — call smart_query to use it
    if sid in app_state.openapi_specs:
        oapi_spec = app_state.openapi_specs[sid]
        tool_names = [k for k in mcp._tool_manager._tools if k.startswith(f"{sid}__")]
        return json.dumps({
            "status": "already_loaded",
            "spec_id": sid,
            "title": oapi_spec.title,
            "openapi_version": oapi_spec.openapi_version,
            "base_url": oapi_spec.base_url,
            "operations": len(oapi_spec.operations),
            "tools_registered": len(tool_names),
            "note": "Spec already loaded. Use smart_query to query it.",
        }, indent=2)

    src = source.strip()

    # Fetch spec from URL or use raw content
    if src.startswith("http"):
        async with httpx.AsyncClient(timeout=60, verify=False, follow_redirects=True) as client:
            resp = await client.get(src, headers={"Accept": "application/json,application/yaml,text/yaml,text/plain"})
            resp.raise_for_status()
            spec_content = resp.text
        # Infer base URL from spec URL: strip the filename part
        inferred_base = base_url or re.sub(r"/[^/]*\.(json|yaml|yml)([?#].*)?$", "", str(resp.url)).rstrip("/")
    else:
        spec_content = src
        inferred_base = base_url

    oapi_spec = parse_openapi_spec(spec_content, base_url_override=inferred_base)

    # Explicit base_url always wins
    if base_url:
        oapi_spec.servers = [base_url.rstrip("/")]

    # Build auth from spec security schemes + provided credentials
    auth_hdrs, auth_prms, auth_ckies = build_openapi_auth(
        oapi_spec,
        api_key=api_key,
        api_key_name=api_key_name,
        bearer_token=bearer_token,
        username=username,
        password=password,
        oauth2_token=oauth2_token,
    )

    # Clear any old tools for this spec_id
    app_state.clear_openapi_spec(sid)
    for tool_name in list(mcp._tool_manager._tools.keys()):
        if tool_name.startswith(f"{sid}__"):
            del mcp._tool_manager._tools[tool_name]

    # Persist state
    app_state.openapi_specs[sid] = oapi_spec
    app_state.openapi_cache[sid] = spec_content
    app_state.auth_headers[sid] = auth_hdrs
    app_state.auth_params[sid] = auth_prms
    app_state.auth_cookies[sid] = auth_ckies

    # Optional AI annotations
    annotations: dict = {}
    if use_ai_descriptions:
        try:
            annotations = await tool_generator.generate_openapi_annotations(oapi_spec, ai_client)
        except Exception:
            pass

    # Register tools
    _register_openapi_tools(sid, oapi_spec, annotations)

    tool_names = [k for k in mcp._tool_manager._tools if k.startswith(f"{sid}__")]

    # Persist to file if requested
    file_path = None
    if save_to_file:
        content = tool_generator.generate_openapi_file_content(sid, oapi_spec, spec_content, annotations)
        file_path = tool_generator.save_tool_file(sid, content)

    return json.dumps({
        "status": "ok",
        "spec_id": sid,
        "title": oapi_spec.title,
        "openapi_version": oapi_spec.openapi_version,
        "base_url": oapi_spec.base_url,
        "operations": len(oapi_spec.operations),
        "tools_generated": len(tool_names),
        "security_schemes": list(oapi_spec.security_schemes.keys()),
        "auth_applied": {
            "headers": list(auth_hdrs.keys()),
            "query_params": list(auth_prms.keys()),
            "cookies": list(auth_ckies.keys()),
        },
        "file_saved": file_path,
        "note": "Refresh tool list to see the newly registered tools.",
    }, indent=2)


@mcp.tool()
async def list_generated_tools(spec_id: str = "") -> str:
    """
    List all dynamically generated OData tools.

    Args:
        spec_id: Filter by spec ID (optional — lists all if omitted)
    """
    prefix = f"{_sanitize_name(spec_id)}__" if spec_id else ""
    tools = {
        name: {
            "description": t.description,
            "parameters": list(t.parameters.get("properties", {}).keys()),
        }
        for name, t in mcp._tool_manager._tools.items()
        if name.startswith(prefix) and "__" in name
    }
    return json.dumps({"count": len(tools), "tools": tools}, indent=2)


@mcp.tool()
async def smart_query(query: str, spec_id: str = "") -> str:
    """
    Answer a natural language question about any loaded spec using GPT-4o.

    Auto-detects the right spec (OData or OpenAPI) based on what is loaded.
    If multiple specs are loaded, GPT-4o picks the most relevant one.

    Args:
        query: Plain English question (e.g. 'Get the top 5 most expensive products')
        spec_id: Optional — ID of a specific loaded spec. Auto-detected if omitted.
    """
    all_specs = {**{k: "odata" for k in app_state.specs}, **{k: "openapi" for k in app_state.openapi_specs}}

    if not all_specs:
        return json.dumps({"error": "No specs loaded. Call load_odata_spec or load_openapi_spec first."})

    # Resolve spec_id
    sid = _sanitize_name(spec_id) if spec_id else ""
    if sid and sid not in all_specs:
        return json.dumps({"error": f"Spec '{spec_id}' not loaded. Available: {list(all_specs.keys())}"})

    if not sid:
        if len(all_specs) == 1:
            sid = next(iter(all_specs))
        else:
            # Ask GPT-4o to pick
            summaries = []
            for k, t in all_specs.items():
                if t == "openapi":
                    s = app_state.openapi_specs[k]
                    summaries.append(f"{k} (OpenAPI): {s.title}")
                else:
                    s = app_state.specs[k]
                    summaries.append(f"{k} (OData): {s.namespace}")
            pick_raw = await ai_client.chat_completion(
                [{"role": "user", "content": f"Specs: {summaries}\nQuery: {query}\nReturn only the spec_id."}],
                max_tokens=20, temperature=0.0,
            )
            picked = pick_raw.strip().strip('"').strip("'")
            sid = picked if picked in all_specs else next(iter(all_specs))

    # Route to OpenAPI or OData
    if all_specs.get(sid) == "openapi":
        oapi_spec = app_state.openapi_specs[sid]
        summary_lines = [f"OpenAPI {oapi_spec.openapi_version} — {oapi_spec.title}", "Operations:"]
        for op_id, op in list(oapi_spec.operations.items())[:30]:
            params = [p.name for p in op.parameters if p.location != "header"]
            summary_lines.append(f"  {op_id}: {op.method} {op.path}" + (f" — {op.summary}" if op.summary else ""))

        raw = await ai_client.chat_completion(
            [
                {"role": "system", "content": "Return ONLY JSON: {\"operation_id\":\"<id>\",\"args\":{},\"explanation\":\"one sentence\"}"},
                {"role": "user", "content": f"Spec:\n{chr(10).join(summary_lines)}\n\nQuestion: {query}"},
            ],
            max_tokens=400, temperature=0.1,
        )
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", raw.strip())
        try:
            intent = json.loads(text)
        except json.JSONDecodeError:
            return json.dumps({"error": "GPT-4o returned invalid JSON", "raw": raw[:300]})

        op_id = intent.get("operation_id", "")
        op = oapi_spec.operations.get(op_id)
        if not op:
            return json.dumps({"error": f"Unknown operation '{op_id}'", "available": list(oapi_spec.operations.keys())[:10]})

        try:
            result = await openapi_executor.execute(
                operation=op, spec=oapi_spec, args=intent.get("args", {}),
                auth_headers=app_state.auth_headers.get(sid, {}),
                auth_params=app_state.auth_params.get(sid, {}),
                auth_cookies=app_state.auth_cookies.get(sid, {}),
                spec_id=sid,
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)})

        return json.dumps({
            "query": query, "spec_id": sid, "spec_type": "openapi",
            "operation_called": f"{sid}__{op_id}",
            "explanation": intent.get("explanation", ""),
            "result": result,
        }, ensure_ascii=False, indent=2)

    # OData path
    spec = app_state.specs.get(sid)
    if not spec:
        return json.dumps({"error": f"Spec '{sid}' not found."})

    # Build a compact spec summary for the prompt
    summary_lines = [f"OData {spec.version} service at {spec.service_url}"]
    for es_name in list(spec.entity_sets.keys())[:30]:
        et = spec.resolve_entity_type(es_name)
        if et:
            keys = ", ".join(et.key_properties)
            props = ", ".join(p.name for p in et.properties[:8])
            extra = f"+{len(et.properties)-8}more" if len(et.properties) > 8 else ""
            summary_lines.append(f"  {es_name}: keys=[{keys}] fields=[{props}{extra}]")
        else:
            summary_lines.append(f"  {es_name}")
    spec_summary = "\n".join(summary_lines)

    system_prompt = (
        "You are an OData query assistant. Given an API spec summary and a user question, "
        "determine the correct OData call and return ONLY a JSON object — no markdown, no explanation outside JSON.\n\n"
        "Return this exact shape:\n"
        '{"entity_set":"EntitySetName","operation":"list|get|create|update|delete",'
        '"args":{"filter":"...","top":5,"orderby":"...","ProductID":1},'
        '"explanation":"one sentence what you understood"}\n\n'
        "Rules:\n"
        "- operation 'list' uses args: filter, select, orderby, top, skip, expand, count\n"
        "- operation 'get' uses the key field(s) from the spec (e.g. OrderID, ProductID)\n"
        "- operation 'delete' uses only key field(s)\n"
        "- Only include args that are needed — omit null/empty values\n"
        "- For 'top N' requests set top=N in args\n"
        "- For 'sorted by X' set orderby='X desc' or 'X asc'\n"
        "- For filtering use OData $filter syntax (e.g. \"Country eq 'Germany'\")\n"
    )

    user_prompt = f"Spec:\n{spec_summary}\n\nUser question: {query}"

    raw = await ai_client.chat_completion(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=400,
        temperature=0.1,
    )

    # Parse GPT-4o intent
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        intent = json.loads(text.strip())
    except json.JSONDecodeError:
        return json.dumps({"error": "GPT-4o returned invalid JSON", "raw": raw[:300]})

    es_name = intent.get("entity_set", "")
    operation = intent.get("operation", "list")
    args = intent.get("args", {})
    explanation = intent.get("explanation", "")

    if es_name not in spec.entity_sets:
        return json.dumps({"error": f"GPT-4o chose unknown entity '{es_name}'", "intent": intent})

    # RBAC: block write operations for non-admin role
    try:
        rbac.check_odata(operation)
    except PermissionDenied as exc:
        return json.dumps({"error": str(exc), "required_role": "admin", "intent": intent})

    # Execute the tool call
    auth_headers = app_state.auth_headers.get(sid, {})
    try:
        result = await executor.execute(
            operation=operation,
            spec=spec,
            entity_set_name=es_name,
            args=args,
            auth_headers=auth_headers,
            spec_id=sid,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc), "intent": intent})

    return json.dumps({
        "query": query,
        "explanation": explanation,
        "tool_called": f"{sid}__{_sanitize_name(es_name)}__{operation}",
        "args_used": args,
        "result": result,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def generate_tool_file(
    spec_id: str,
    use_ai_descriptions: bool = True,
) -> str:
    """
    Generate a persistent Python tool file for a loaded spec.

    Creates tools/{spec_id}.py with @mcp.tool() decorated functions, proper
    Python type annotations, and (optionally) AI-generated docstrings.
    The file is auto-loaded on server restart — tools survive without re-calling
    load_odata_spec.

    Args:
        spec_id: ID of an already-loaded spec (must call load_odata_spec first)
        use_ai_descriptions: Use GPT-4o to write enriched docstrings (recommended)
    """
    sid = _sanitize_name(spec_id)
    spec = app_state.specs.get(sid)
    if not spec:
        return json.dumps({"error": f"Spec '{spec_id}' not loaded. Call load_odata_spec first."})

    annotations = {}
    if use_ai_descriptions:
        try:
            annotations = await tool_generator.generate_annotations(spec, ai_client)
        except Exception as exc:
            annotations = {}
            print(f"[generate_tool_file] AI annotation failed: {exc}", file=sys.stderr)

    xml_content = app_state.xml_cache.get(sid, "")
    base_url = spec.service_url

    content = tool_generator.generate_file_content(sid, spec, xml_content, base_url, annotations)
    file_path = tool_generator.save_tool_file(sid, content)

    es_count = len(spec.entity_sets)
    tool_count = es_count * 5  # list/get/create/update/delete per entity

    return json.dumps({
        "status": "ok",
        "spec_id": sid,
        "file": file_path,
        "entity_sets": es_count,
        "tools_written": tool_count,
        "ai_annotations": bool(annotations),
        "note": "File will be auto-loaded on next server startup.",
    }, indent=2)


@mcp.tool()
async def list_tool_files() -> str:
    """
    List all generated tool files in the tools/ directory.
    Shows spec ID, file size, and modification date.
    """
    files = []
    tools_dir = tool_generator.TOOLS_DIR
    if os.path.isdir(tools_dir):
        for fname in sorted(os.listdir(tools_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            fpath = os.path.join(tools_dir, fname)
            stat = os.stat(fpath)
            files.append({
                "spec_id": fname[:-3],
                "file": fpath,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            })
    return json.dumps({"count": len(files), "files": files}, indent=2)


@mcp.resource("specs://loaded")
async def resource_loaded_specs() -> str:
    """All currently loaded API specs with their entity sets or operations."""
    result = {}
    for sid, spec in app_state.specs.items():
        result[sid] = {
            "type": "odata",
            "version": spec.version,
            "namespace": spec.namespace,
            "base_url": spec.service_url,
            "entity_sets": list(spec.entity_sets.keys()),
        }
    for sid, spec in app_state.openapi_specs.items():
        result[sid] = {
            "type": "openapi",
            "title": spec.title,
            "version": spec.openapi_version,
            "base_url": spec.base_url,
            "operations": list(spec.operations.keys()),
        }
    return json.dumps(result, indent=2)


@mcp.resource("tools://list")
async def resource_tools_list() -> str:
    """All dynamically generated tools currently registered on this server."""
    tools = {
        name: {
            "description": t.description,
            "parameters": list(t.parameters.get("properties", {}).keys()),
        }
        for name, t in mcp._tool_manager._tools.items()
        if "__" in name
    }
    return json.dumps({"count": len(tools), "tools": tools}, indent=2)


@mcp.tool()
async def get_ai_insights(question: str, spec_id: str = "") -> str:
    """
    Ask GPT-4o a question about a loaded OData spec.

    Args:
        question: Natural language question (e.g. 'What fields does BusinessPartner have?')
        spec_id: Optional — auto-detected when only one OData spec is loaded
    """
    all_odata = app_state.specs
    if not all_odata:
        return json.dumps({"error": "No OData specs loaded. Call load_odata_spec first."})

    sid = _sanitize_name(spec_id) if spec_id else ""
    if sid and sid not in all_odata:
        return json.dumps({"error": f"Spec '{spec_id}' not loaded. Available: {list(all_odata.keys())}"})
    if not sid:
        if len(all_odata) == 1:
            sid = next(iter(all_odata))
        else:
            return json.dumps({"error": f"Multiple specs loaded — specify spec_id. Available: {list(all_odata.keys())}"})

    spec = all_odata[sid]
    summary = _spec_summary(spec, sid)
    answer = await ai_client.analyze_spec(summary, question)
    return json.dumps({"spec_id": sid, "question": question, "answer": answer}, indent=2)


@mcp.tool()
async def generate_odata_query(request: str, spec_id: str = "") -> str:
    """
    Convert a natural language request into an OData REST API call using GPT-4o.

    Args:
        request: What you want to do (e.g. 'Get open orders for customer 1234')
        spec_id: Optional — auto-detected when only one OData spec is loaded
    """
    all_odata = app_state.specs
    if not all_odata:
        return json.dumps({"error": "No OData specs loaded. Call load_odata_spec first."})

    sid = _sanitize_name(spec_id) if spec_id else ""
    if sid and sid not in all_odata:
        return json.dumps({"error": f"Spec '{spec_id}' not loaded. Available: {list(all_odata.keys())}"})
    if not sid:
        if len(all_odata) == 1:
            sid = next(iter(all_odata))
        else:
            return json.dumps({"error": f"Multiple specs loaded — specify spec_id. Available: {list(all_odata.keys())}"})

    spec = all_odata[sid]
    summary = _spec_summary(spec, sid)
    result = await ai_client.generate_query(summary, request)
    return json.dumps({"spec_id": sid, "request": request, "query": result}, indent=2)


@mcp.tool()
async def test_ai_connection() -> str:
    """
    Test the SAP AI Core connection by pinging GPT-4o.
    Returns the model response and confirms the connection is healthy.
    """
    url = await ai_client._inference_url()
    response = await ai_client.chat_completion(
        [{"role": "user", "content": "Reply with exactly: OK"}],
        max_tokens=10,
    )
    return json.dumps({
        "status": "connected",
        "model": ai_client.model_name,
        "inference_url": url,
        "response": response.strip(),
    }, indent=2)


@mcp.tool()
async def whoami() -> str:
    """
    Show the current user's role and what operations are permitted.

    Useful for checking access level before attempting write operations.
    Returns role, permitted operations, and how to configure RBAC.
    """
    status = rbac.status()
    role = rbac.get_role()
    permitted_odata = ["list", "get"] + (["create", "update", "delete"] if rbac.can_write() else [])
    permitted_http = ["GET", "HEAD", "OPTIONS"] + (["POST", "PUT", "PATCH", "DELETE"] if rbac.can_write() else [])
    return json.dumps({
        "role": role.value,
        "permitted_odata_operations": permitted_odata,
        "permitted_http_methods": permitted_http,
        "rbac_config": status,
    }, indent=2)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _spec_summary(spec: ODataSpec, spec_id: str) -> str:
    lines = [
        f"OData {spec.version} — id='{spec_id}', base: {spec.service_url}",
        f"Namespace: {spec.namespace}",
        "Entity Sets:",
    ]
    for es_name in spec.entity_sets:
        et = spec.resolve_entity_type(es_name)
        if et:
            keys = ", ".join(et.key_properties) or "(none)"
            props = ", ".join(p.name for p in et.properties[:8])
            extra = f" +{len(et.properties)-8} more" if len(et.properties) > 8 else ""
            lines.append(f"  {es_name}  keys=[{keys}]  props=[{props}{extra}]")
        else:
            lines.append(f"  {es_name}")
    if spec.actions:
        lines.append("Actions: " + ", ".join(a.name for a in spec.actions))
    if spec.functions:
        lines.append("Functions: " + ", ".join(f.name for f in spec.functions))
    return "\n".join(lines)


# ── Auto-load persisted tool files ────────────────────────────────────────────
# Runs once at import time — picks up any tools/{spec_id}.py files from prior runs.
_autoloaded = tool_generator.load_all_tool_files(mcp, executor, app_state, openapi_executor=openapi_executor)
if _autoloaded:
    import sys as _sys
    print(f"[mcp_server] Auto-loaded {_autoloaded} tools from tools/ directory", file=_sys.stderr)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    argv = sys.argv[1:]

    if "--sse" in argv:
        idx = argv.index("--sse")
        port = int(argv[idx + 1]) if idx + 1 < len(argv) and argv[idx + 1].isdigit() else 8000
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = port
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
        print(f"OData MCP Server (FastMCP SSE) → http://localhost:{port}/sse", file=sys.stderr)
        mcp.run(transport="sse")

    elif "--http" in argv:
        idx = argv.index("--http")
        port = int(argv[idx + 1]) if idx + 1 < len(argv) and argv[idx + 1].isdigit() else 8000
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = port
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
        print(f"OData MCP Server (FastMCP HTTP) → http://localhost:{port}/mcp", file=sys.stderr)
        mcp.run(transport="streamable-http")

    else:
        print("OData MCP Server (FastMCP stdio)", file=sys.stderr)
        mcp.run(transport="stdio")

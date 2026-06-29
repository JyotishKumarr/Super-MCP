"""
OData MCP — REST API

Two primary endpoints:
    POST /api/v1/generate   Fetch any OData $metadata URL → parse → save tools/{id}.py → ready to query
    POST /api/v1/query      Natural-language query against a loaded spec (GPT-4o picks the right call)

Supporting:
    GET  /api/v1/files               List generated tool files
    DELETE /api/v1/files/{spec_id}   Delete a tool file
    POST /api/v1/call/{tool_name}    Call a specific tool directly
    GET  /health                     Health check

Run:
    python3 api.py
    python3 api.py --port 9000
"""

import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Path, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from auth_manager import auth_manager
from config import SAP_AI_CONFIG
from odata_executor import ODataExecutor
from odata_parser import _sanitize_name, parse_odata_metadata
from openapi_executor import OpenAPIExecutor, build_openapi_auth
from openapi_parser import parse_openapi_spec
from sap_ai_client import SAPAIClient
from state import ToolDefinition, app_state
import tool_generator

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("odata-api")

# ── Services ───────────────────────────────────────────────────────────────────

ai_client = SAPAIClient(SAP_AI_CONFIG)
executor = ODataExecutor()
openapi_executor = OpenAPIExecutor()
_start_time = time.time()


# ── App ────────────────────────────────────────────────────────────────────────

def _autoload_tool_files() -> int:
    """
    On startup, scan tools/*.py and restore every saved spec into app_state.
    Each file embeds its spec as base64 — no network call needed.
    After this runs the server is fully query-ready without calling /generate again.
    """
    import importlib.util

    total = 0
    d = tool_generator.TOOLS_DIR
    if not os.path.isdir(d):
        return 0

    for fname in sorted(os.listdir(d)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        path = os.path.join(d, fname)
        try:
            mod_name = f"autoload_{fname[:-3]}"
            file_spec = importlib.util.spec_from_file_location(mod_name, path)
            module = importlib.util.module_from_spec(file_spec)
            file_spec.loader.exec_module(module)

            spec_id = getattr(module, "SPEC_ID", None)
            base_url = getattr(module, "BASE_URL", "")
            if not spec_id:
                continue

            if hasattr(module, "OPENAPI_VERSION"):
                spec_content = getattr(module, "SPEC_CONTENT", "")
                if not spec_content:
                    continue
                oapi_spec = parse_openapi_spec(spec_content, base_url_override=base_url)
                app_state.openapi_specs[spec_id] = oapi_spec
                app_state.openapi_cache[spec_id] = spec_content
                app_state.auth_headers.setdefault(spec_id, {})
                app_state.auth_params.setdefault(spec_id, {})
                app_state.auth_cookies.setdefault(spec_id, {})
                count = _register_openapi_tools_in_state(spec_id, oapi_spec)
                total += count
                logger.info("Auto-loaded OpenAPI '%s': %d tools", spec_id, count)
            else:
                spec_xml = getattr(module, "SPEC_XML", "")
                if not spec_xml:
                    continue
                odata_spec = parse_odata_metadata(spec_xml, service_url=base_url)
                app_state.specs[spec_id] = odata_spec
                app_state.xml_cache[spec_id] = spec_xml
                app_state.auth_headers.setdefault(spec_id, {})
                count = _register_tools(spec_id, odata_spec)
                total += count
                logger.info("Auto-loaded OData '%s': %d tools", spec_id, count)

        except Exception as exc:
            logger.warning("Failed to auto-load %s: %s", fname, exc)

    return total


@asynccontextmanager
async def lifespan(app: FastAPI):
    n = _autoload_tool_files()
    if n:
        logger.info("Auto-loaded %d tools from tools/ directory — server is query-ready", n)
    else:
        logger.info("No saved tool files found. Call /generate or /generate/openapi to load a spec.")
    yield

app = FastAPI(
    title="OData MCP API",
    description="Generate MCP tools from any OData $metadata URL and query them with natural language.",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Shared helpers ─────────────────────────────────────────────────────────────

async def _fetch_xml(url: str) -> tuple[str, str]:
    """Fetch $metadata XML following redirects. Returns (xml_content, effective_base_url)."""
    async with httpx.AsyncClient(timeout=60, verify=False, follow_redirects=True) as client:
        resp = await client.get(url, headers={"Accept": "application/xml,text/xml"})
        resp.raise_for_status()
        effective = str(resp.url)
        base_url = re.sub(r"/?\$metadata.*$", "", effective).rstrip("/")
        return resp.text, base_url


def _register_openapi_tools_in_state(spec_id: str, oapi_spec) -> int:
    """Register one ToolDefinition per OpenAPI operation into app_state.tools."""
    count = 0
    for op_id, op in oapi_spec.operations.items():
        name = f"{spec_id}__{op_id}"
        td = ToolDefinition(
            name=name,
            description=op.summary or op.description or f"{op.method} {op.path}",
            input_schema={"type": "object", "properties": {}},
            spec_id=spec_id,
            entity_set=op.path,       # reused as path
            operation=op_id,          # reused as operationId
            key_props=[p.name for p in op.parameters if p.required],
            http_method=op.method,
        )
        app_state.tools[name] = td
        count += 1
    return count


def _register_tools(spec_id: str, spec) -> int:
    """Register CRUD ToolDefinitions for every EntitySet."""
    count = 0
    for es_name in spec.entity_sets:
        et = spec.resolve_entity_type(es_name)
        prefix = f"{spec_id}__{_sanitize_name(es_name)}"
        key_schema = et.key_property_schema() if et else {"key": {"type": "string"}}
        all_props = et.all_property_schema() if et else {}
        non_key = et.all_property_schema(exclude_keys=True) if et else {}
        key_list = list(key_schema.keys())

        defs = [
            ToolDefinition(name=f"{prefix}__list",   description=f"List {es_name} records.",          input_schema={"type": "object", "properties": {"filter": {"type": "string"}, "select": {"type": "string"}, "orderby": {"type": "string"}, "top": {"type": "integer"}, "skip": {"type": "integer"}, "expand": {"type": "string"}, "count": {"type": "boolean"}, "search": {"type": "string"}}}, spec_id=spec_id, entity_set=es_name, operation="list",   key_props=key_list),
            ToolDefinition(name=f"{prefix}__get",    description=f"Get a {es_name} record by key.",   input_schema={"type": "object", "properties": {**key_schema, "expand": {"type": "string"}, "select": {"type": "string"}}, "required": key_list}, spec_id=spec_id, entity_set=es_name, operation="get",    key_props=key_list),
            ToolDefinition(name=f"{prefix}__create", description=f"Create a new {es_name} record.",   input_schema={"type": "object", "properties": all_props}, spec_id=spec_id, entity_set=es_name, operation="create", key_props=key_list, http_method="POST"),
            ToolDefinition(name=f"{prefix}__update", description=f"Update a {es_name} record.",       input_schema={"type": "object", "properties": {**key_schema, **non_key}, "required": key_list}, spec_id=spec_id, entity_set=es_name, operation="update", key_props=key_list, http_method="PATCH"),
            ToolDefinition(name=f"{prefix}__delete", description=f"Delete a {es_name} record.",       input_schema={"type": "object", "properties": key_schema, "required": key_list}, spec_id=spec_id, entity_set=es_name, operation="delete", key_props=key_list, http_method="DELETE"),
        ]
        for td in defs:
            app_state.tools[td.name] = td
            count += 1
    return count


def _spec_summary(spec, spec_id: str) -> str:
    lines = [f"OData {spec.version} service — id='{spec_id}', base: {spec.service_url}", "Entity Sets:"]
    for es_name in list(spec.entity_sets.keys())[:30]:
        et = spec.resolve_entity_type(es_name)
        if et:
            keys = ", ".join(et.key_properties)
            props = ", ".join(p.name for p in et.properties[:8])
            extra = f" +{len(et.properties)-8} more" if len(et.properties) > 8 else ""
            lines.append(f"  {es_name}  keys=[{keys}]  fields=[{props}{extra}]")
        else:
            lines.append(f"  {es_name}")
    return "\n".join(lines)


async def _resolve_spec_id(spec_id: Optional[str], query: str = "") -> str:
    """
    Resolve which spec to use for a query.
    - If spec_id is given, validate and return it.
    - If only one spec is loaded (OData or OpenAPI), use it automatically.
    - If multiple specs are loaded, ask GPT-4o to pick the right one based on the query.
    """
    all_specs = {**{k: "odata" for k in app_state.specs}, **{k: "openapi" for k in app_state.openapi_specs}}

    if not all_specs:
        raise HTTPException(status_code=404, detail="No specs loaded. Run /generate or /generate/openapi first.")

    if spec_id:
        sid = _sanitize_name(spec_id)
        if sid not in all_specs:
            raise HTTPException(
                status_code=404,
                detail=f"Spec '{spec_id}' not loaded. Available: {list(all_specs.keys())}",
            )
        return sid

    if len(all_specs) == 1:
        return next(iter(all_specs))

    # Multiple specs — let GPT-4o pick
    summaries = []
    for sid, stype in all_specs.items():
        if stype == "openapi":
            s = app_state.openapi_specs[sid]
            ops = list(s.operations.keys())[:5]
            summaries.append(f"{sid} (OpenAPI): {s.title} — ops: {', '.join(ops)}")
        else:
            s = app_state.specs[sid]
            entities = list(s.entity_sets.keys())[:5]
            summaries.append(f"{sid} (OData): {s.namespace} — entities: {', '.join(entities)}")

    prompt = (
        "You are choosing which API spec to use for a user query.\n"
        "Available specs:\n" + "\n".join(f"  {s}" for s in summaries) + "\n\n"
        f"User query: {query}\n\n"
        'Return ONLY the spec_id as a plain string, nothing else.'
    )
    try:
        chosen = await ai_client.chat_completion(
            [{"role": "user", "content": prompt}], max_tokens=20, temperature=0.0
        )
        chosen = chosen.strip().strip('"').strip("'")
        if chosen in all_specs:
            return chosen
    except Exception:
        pass

    # Fallback: first spec
    return next(iter(all_specs))


# ── Models ─────────────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    source: str = Field(..., description="OData $metadata URL (e.g. https://…/$metadata) or raw XML.")
    spec_id: str = Field(..., description="Short ID to namespace the tools, e.g. 'trip', 'nw', 'bp'.", pattern=r"^[a-zA-Z0-9_\-]+$")
    use_ai_descriptions: bool = Field(False, description="Use GPT-4o to write enriched docstrings (slower, ~30s).")
    save_to_file: bool = Field(True, description="Persist tools to tools/{spec_id}.py so they auto-load on restart.")


class GenerateResponse(BaseModel):
    spec_id: str
    file: str
    odata_version: str
    entity_sets: int
    tools_written: int
    size_kb: float
    ai_annotations: bool


class QueryRequest(BaseModel):
    spec_id: Optional[str] = Field(None, description="ID of a loaded spec. Auto-detected if only one is loaded.")
    query: str = Field(..., description="Natural language question, e.g. 'Get top 5 most expensive products'.")
    max_records: Optional[int] = Field(None, description="Cap the number of records returned.", ge=1, le=1000)


class QueryIntent(BaseModel):
    entity_set: str
    operation: str
    parameters: dict[str, Any]
    explanation: str


class QueryResponse(BaseModel):
    query: str
    spec_id: str
    intent: QueryIntent
    tool_called: str
    result: Any
    elapsed_ms: int


class CallResponse(BaseModel):
    tool: str
    result: Any
    elapsed_ms: int


class SpecInfo(BaseModel):
    spec_id: str
    spec_type: str      # "odata" | "openapi"
    title: str
    version: str
    base_url: str
    entity_count: int
    tool_count: int


class FileInfo(BaseModel):
    spec_id: str
    file: str
    size_kb: float
    modified: str


class HealthResponse(BaseModel):
    status: str
    specs_loaded: int
    uptime_seconds: float


class OpenAPIGenerateRequest(BaseModel):
    source: str = Field(..., description="OpenAPI spec URL (JSON/YAML) or raw spec content.")
    spec_id: str = Field(..., description="Short ID to namespace tools, e.g. 'petstore'.", pattern=r"^[a-zA-Z0-9_\-]+$")
    base_url: str = Field("", description="Override base URL for API calls.")
    api_key: str = Field("", description="API key value (auto-applied to correct location from spec).")
    api_key_name: str = Field("", description="Override API key header/param name.")
    bearer_token: str = Field("", description="Bearer token for Authorization header.")
    username: str = Field("", description="HTTP Basic auth username.")
    password: str = Field("", description="HTTP Basic auth password.")
    oauth2_token: str = Field("", description="OAuth2 access token.")
    use_ai_descriptions: bool = Field(False, description="Use GPT-4o for enriched descriptions.")
    save_to_file: bool = Field(True, description="Persist tools to tools/{spec_id}.py so they auto-load on restart.")


class OpenAPIGenerateResponse(BaseModel):
    spec_id: str
    file: str
    title: str
    openapi_version: str
    operations: int
    tools_written: int
    size_kb: float
    security_schemes: list[str]
    auth_applied: dict
    ai_annotations: bool


class OpenAPIQueryRequest(BaseModel):
    spec_id: Optional[str] = Field(None, description="ID of a loaded OpenAPI spec. Auto-detected if only one is loaded.")
    query: str = Field(..., description="Natural language query, e.g. 'Get all pets with status available'.")


class OpenAPIQueryResponse(BaseModel):
    query: str
    spec_id: str
    operation_id: str
    method: str
    path: str
    args_used: dict
    explanation: str
    result: Any
    elapsed_ms: int


class UnifiedQueryResponse(BaseModel):
    query: str
    spec_id: str
    spec_type: str   # "odata" | "openapi"
    tool_called: str
    explanation: str
    result: Any
    elapsed_ms: int


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse, tags=["System"])
async def health():
    return HealthResponse(
        status="healthy",
        specs_loaded=len(app_state.specs),
        uptime_seconds=round(time.time() - _start_time, 1),
    )


@app.post("/api/v1/generate", response_model=GenerateResponse, tags=["Generate"], summary="Load spec + generate tool file")
async def generate(req: GenerateRequest):
    """
    **Fetch any OData `$metadata` URL, parse it, and write `tools/{spec_id}.py`.**

    - Follows redirects automatically (handles TripPin session URLs).
    - Loads the spec into memory — ready for `/query` immediately after.
    - Set `use_ai_descriptions: true` for GPT-4o enriched docstrings.

    ```
    {"source": "https://services.odata.org/TripPinRESTierService/$metadata", "spec_id": "trip"}
    ```
    """
    spec_id = _sanitize_name(req.spec_id)

    # If already loaded, return existing state — /generate is for new specs only
    if spec_id in app_state.specs:
        spec = app_state.specs[spec_id]
        fpath = os.path.join(tool_generator.TOOLS_DIR, f"{spec_id}.py")
        size_kb = round(os.path.getsize(fpath) / 1024, 1) if os.path.exists(fpath) else 0.0
        return GenerateResponse(
            spec_id=spec_id,
            file=fpath if os.path.exists(fpath) else "",
            odata_version=spec.version,
            entity_sets=len(spec.entity_sets),
            tools_written=len(app_state.tools_for_spec(spec_id)),
            size_kb=size_kb,
            ai_annotations=False,
        )

    src = req.source.strip()

    try:
        if src.startswith("http"):
            xml_content, base_url = await _fetch_xml(src)
        else:
            xml_content = src
            base_url = ""
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"HTTP {exc.response.status_code} fetching metadata: {exc.request.url}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch metadata: {exc}")

    try:
        spec = parse_odata_metadata(xml_content, service_url=base_url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse OData XML: {exc}")

    # Load into memory
    app_state.clear_spec(spec_id)
    app_state.specs[spec_id] = spec
    app_state.xml_cache[spec_id] = xml_content
    app_state.auth_headers[spec_id] = {}
    _register_tools(spec_id, spec)

    # AI annotations (optional)
    annotations = {}
    if req.use_ai_descriptions:
        try:
            annotations = await tool_generator.generate_annotations(spec, ai_client)
        except Exception as exc:
            logger.warning("AI annotation failed: %s", exc)

    file_path = None
    size_kb = 0.0
    if req.save_to_file:
        content = tool_generator.generate_file_content(spec_id, spec, xml_content, base_url, annotations)
        file_path = tool_generator.save_tool_file(spec_id, content)
        size_kb = round(os.path.getsize(file_path) / 1024, 1)
        logger.info("Generated %s: %d entities, %.1f KB", file_path, len(spec.entity_sets), size_kb)

    return GenerateResponse(
        spec_id=spec_id,
        file=file_path or "",
        odata_version=spec.version,
        entity_sets=len(spec.entity_sets),
        tools_written=len(spec.entity_sets) * 5,
        size_kb=size_kb,
        ai_annotations=bool(annotations),
    )


@app.post("/api/v1/query", response_model=QueryResponse, tags=["Query"], summary="Natural-language query via GPT-4o")
async def query(req: QueryRequest):
    """
    **Ask a plain English question — GPT-4o picks the right OData call and executes it.**

    `spec_id` is optional — auto-detected when only one spec is loaded.

    ```
    {"query": "List the first 3 people"}
    {"query": "Get top 5 most expensive products", "spec_id": "nw"}
    ```
    """
    spec_id = await _resolve_spec_id(req.spec_id, req.query)
    spec = app_state.specs.get(spec_id)

    if not spec:
        raise HTTPException(
            status_code=404,
            detail=f"Spec '{spec_id}' is an OpenAPI spec, not OData. Use POST /api/v1/query/openapi.",
        )

    t0 = time.monotonic()

    # GPT-4o intent detection
    system_prompt = (
        "You are an OData query assistant. Given an API spec and a user question, "
        "determine the correct OData call and return ONLY a JSON object — no markdown, no extra text.\n\n"
        "Return this exact shape:\n"
        '{"entity_set":"<EntitySetName>","operation":"list|get|create|update|delete",'
        '"parameters":{"filter":"...","top":5,"orderby":"..."},"explanation":"one sentence"}\n\n'
        "Rules:\n"
        "- operation 'list' uses parameters: filter, select, orderby, top, skip, expand, count\n"
        "- operation 'get' uses only the key field(s) from the spec (e.g. ProductID, OrderID, UserName)\n"
        "- operation 'delete' uses only key field(s)\n"
        "- NEVER put entity field names directly in parameters — always use 'filter' for filtering\n"
        "- For name/text searches use OData $filter syntax: filter: \"ProductName eq 'Chai'\"\n"
        "- For 'top N' set top=N; for sorted results set orderby='Field desc'\n"
        "- Only include parameters that are needed — omit null/empty values"
    )
    user_prompt = f"Spec:\n{_spec_summary(spec, spec_id)}\n\nQuestion: {req.query}"

    try:
        raw = await ai_client.chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            max_tokens=400, temperature=0.0,
        )
        cleaned = re.sub(r"^```[a-z]*\n?|\n?```$", "", raw.strip())
        intent_data = json.loads(cleaned)
        intent = QueryIntent(**intent_data)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GPT-4o failed: {exc}")

    if req.max_records and intent.operation == "list":
        intent.parameters["top"] = req.max_records

    tool_name = f"{spec_id}__{_sanitize_name(intent.entity_set)}__{intent.operation}"
    if tool_name not in app_state.tools:
        raise HTTPException(status_code=422, detail=f"GPT-4o chose unknown entity '{intent.entity_set}'. Available: {list(spec.entity_sets.keys())}")

    try:
        result = await executor.execute(
            operation=intent.operation,
            spec=spec,
            entity_set_name=intent.entity_set,
            args=intent.parameters,
            auth_headers=app_state.auth_headers.get(spec_id, {}),
            spec_id=spec_id,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"OData error {exc.response.status_code}: {exc.response.text[:300]}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"OData call failed: {exc}")

    return QueryResponse(
        query=req.query,
        spec_id=spec_id,
        intent=intent,
        tool_called=tool_name,
        result=result,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )


@app.post("/api/v1/call/{tool_name}", response_model=CallResponse, tags=["Tools"], summary="Call a tool directly by name")
async def call_tool(
    tool_name: str = Path(..., description="Full tool name, e.g. petstore__getInventory or trip__People__list"),
    args: dict[str, Any] = {},
):
    """Call a specific generated tool by name with parameters as the request body."""
    td = app_state.tools.get(tool_name)
    if not td:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found. Run /generate first.")

    t0 = time.monotonic()

    # Route to OpenAPI executor if this spec_id belongs to an OpenAPI spec
    if td.spec_id in app_state.openapi_specs:
        oapi_spec = app_state.openapi_specs[td.spec_id]
        op = oapi_spec.operations.get(td.operation)
        if not op:
            raise HTTPException(status_code=404, detail=f"Operation '{td.operation}' not found in spec '{td.spec_id}'.")
        try:
            result = await openapi_executor.execute(
                operation=op,
                spec=oapi_spec,
                args=args,
                auth_headers=app_state.auth_headers.get(td.spec_id, {}),
                auth_params=app_state.auth_params.get(td.spec_id, {}),
                auth_cookies=app_state.auth_cookies.get(td.spec_id, {}),
                spec_id=td.spec_id,
            )
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"API error {exc.response.status_code}: {exc.response.text[:300]}")
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"API call failed: {exc}")
    else:
        spec = app_state.specs.get(td.spec_id)
        if not spec:
            raise HTTPException(status_code=404, detail=f"Spec '{td.spec_id}' not in memory. Run /generate again.")
        try:
            result = await executor.execute(
                operation=td.operation,
                spec=spec,
                entity_set_name=td.entity_set,
                args=args,
                auth_headers=app_state.auth_headers.get(td.spec_id, {}),
                spec_id=td.spec_id,
            )
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"OData error {exc.response.status_code}: {exc.response.text[:300]}")
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"OData call failed: {exc}")

    return CallResponse(tool=tool_name, result=result, elapsed_ms=int((time.monotonic() - t0) * 1000))


@app.post("/api/v1/generate/openapi", response_model=OpenAPIGenerateResponse, tags=["OpenAPI"], summary="Load OpenAPI spec + generate tool file")
async def generate_openapi(req: OpenAPIGenerateRequest):
    """
    **Fetch any OpenAPI / Swagger `$metadata` URL or raw spec, parse it, and write `tools/{spec_id}.py`.**

    - Supports OpenAPI 3.x and Swagger 2.0 (JSON or YAML).
    - Detects security schemes and applies provided credentials automatically.
    - Loads the spec into memory — ready for `/query/openapi` immediately after.

    ```
    {"source": "https://petstore3.swagger.io/api/v3/openapi.json", "spec_id": "petstore"}
    {"source": "https://api.github.com/...", "spec_id": "github", "bearer_token": "ghp_xxx"}
    ```
    """
    spec_id = _sanitize_name(req.spec_id)

    # If already loaded, return existing state — /generate/openapi is for new specs only
    if spec_id in app_state.openapi_specs:
        oapi_spec = app_state.openapi_specs[spec_id]
        fpath = os.path.join(tool_generator.TOOLS_DIR, f"{spec_id}.py")
        size_kb = round(os.path.getsize(fpath) / 1024, 1) if os.path.exists(fpath) else 0.0
        return OpenAPIGenerateResponse(
            spec_id=spec_id,
            file=fpath if os.path.exists(fpath) else "",
            title=oapi_spec.title,
            openapi_version=oapi_spec.openapi_version,
            operations=len(oapi_spec.operations),
            tools_written=len(app_state.tools_for_spec(spec_id)),
            size_kb=size_kb,
            security_schemes=list(oapi_spec.security_schemes.keys()),
            auth_applied={
                "headers": list(app_state.auth_headers.get(spec_id, {}).keys()),
                "query_params": list(app_state.auth_params.get(spec_id, {}).keys()),
                "cookies": list(app_state.auth_cookies.get(spec_id, {}).keys()),
            },
            ai_annotations=False,
        )

    src = req.source.strip()

    try:
        if src.startswith("http"):
            async with httpx.AsyncClient(timeout=60, verify=False, follow_redirects=True) as client:
                resp = await client.get(src, headers={"Accept": "application/json,application/yaml,text/yaml,text/plain"})
                resp.raise_for_status()
                spec_content = resp.text
            inferred_base = req.base_url or re.sub(r"/[^/]*\.(json|yaml|yml)([?#].*)?$", "", str(resp.url)).rstrip("/")
        else:
            spec_content = src
            inferred_base = req.base_url
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"HTTP {exc.response.status_code} fetching spec: {exc.request.url}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch spec: {exc}")

    try:
        oapi_spec = parse_openapi_spec(spec_content, base_url_override=inferred_base)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse OpenAPI spec: {exc}")

    if req.base_url:
        oapi_spec.servers = [req.base_url.rstrip("/")]

    # Build auth from explicitly-provided request fields
    auth_hdrs, auth_prms, auth_ckies = build_openapi_auth(
        oapi_spec,
        api_key=req.api_key,
        api_key_name=req.api_key_name,
        bearer_token=req.bearer_token,
        username=req.username,
        password=req.password,
        oauth2_token=req.oauth2_token,
    )

    # Merge env-var auth (auth_manager) — env-var credentials are the base;
    # anything explicitly passed in the request takes precedence.
    # Credentials NEVER flow through the request body when using auth_manager.
    if auth_manager.is_configured(spec_id):
        try:
            mgr_hdrs, mgr_prms, mgr_ckies = await auth_manager.get_auth_headers(spec_id)
            auth_hdrs  = {**mgr_hdrs, **auth_hdrs}   # request overrides env-var
            auth_prms  = {**mgr_prms,  **auth_prms}
            auth_ckies = {**mgr_ckies, **auth_ckies}
            logger.info("Auto-applied env-var auth for spec '%s' (type=%s)", spec_id, auth_manager.get_config(spec_id).auth_type)
        except Exception as exc:
            logger.warning("auth_manager.get_auth_headers failed for '%s': %s", spec_id, exc)

    # Load into memory
    app_state.clear_openapi_spec(spec_id)
    app_state.openapi_specs[spec_id] = oapi_spec
    app_state.openapi_cache[spec_id] = spec_content
    app_state.auth_headers[spec_id] = auth_hdrs
    app_state.auth_params[spec_id] = auth_prms
    app_state.auth_cookies[spec_id] = auth_ckies
    _register_openapi_tools_in_state(spec_id, oapi_spec)

    # AI annotations
    annotations: dict = {}
    if req.use_ai_descriptions:
        try:
            annotations = await tool_generator.generate_openapi_annotations(oapi_spec, ai_client)
        except Exception as exc:
            logger.warning("OpenAPI AI annotation failed: %s", exc)

    # Write tool file
    content = tool_generator.generate_openapi_file_content(spec_id, oapi_spec, spec_content, annotations)
    file_path = tool_generator.save_tool_file(spec_id, content)
    size_kb = round(os.path.getsize(file_path) / 1024, 1)

    logger.info("Generated OpenAPI %s: %d operations, %.1f KB", file_path, len(oapi_spec.operations), size_kb)

    return OpenAPIGenerateResponse(
        spec_id=spec_id,
        file=file_path,
        title=oapi_spec.title,
        openapi_version=oapi_spec.openapi_version,
        operations=len(oapi_spec.operations),
        tools_written=len(oapi_spec.operations),
        size_kb=size_kb,
        security_schemes=list(oapi_spec.security_schemes.keys()),
        auth_applied={
            "headers": list(auth_hdrs.keys()),
            "query_params": list(auth_prms.keys()),
            "cookies": list(auth_ckies.keys()),
        },
        ai_annotations=bool(annotations),
    )


@app.post("/api/v1/query/openapi", response_model=OpenAPIQueryResponse, tags=["OpenAPI"], summary="Natural-language query for OpenAPI spec via GPT-4o")
async def query_openapi(req: OpenAPIQueryRequest):
    """
    **Ask a plain-English question — GPT-4o picks the right OpenAPI operation and executes it.**

    `spec_id` is optional — auto-detected when only one spec is loaded.

    ```
    {"query": "Get all available pets"}
    {"query": "List issues for repo octocat/Hello-World", "spec_id": "github"}
    ```
    """
    spec_id = await _resolve_spec_id(req.spec_id, req.query)
    oapi_spec = app_state.openapi_specs.get(spec_id)
    if not oapi_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Spec '{spec_id}' is an OData spec, not OpenAPI. Use POST /api/v1/query.",
        )

    t0 = time.monotonic()

    # Build compact summary for GPT-4o
    summary_lines = [f"OpenAPI {oapi_spec.openapi_version} — title: {oapi_spec.title}", "Operations:"]
    for op_id, op in list(oapi_spec.operations.items())[:40]:
        params = [p.name for p in op.parameters if p.location != "header"]
        if op.request_body_schema:
            params += list(op.request_body_schema.keys())[:4]
        elif op.body_param_name:
            params.append(op.body_param_name)
        param_str = f" params=[{', '.join(params[:6])}]" if params else ""
        summary_lines.append(f"  {op_id}: {op.method} {op.path}{param_str}" + (f" — {op.summary}" if op.summary else ""))
    spec_summary = "\n".join(summary_lines)

    system_prompt = (
        "You are an OpenAPI query assistant. Given the spec and a user question, "
        "return ONLY a JSON object — no markdown, no extra text.\n"
        '{"operation_id":"<operationId>","args":{"param":"value"},"explanation":"one sentence"}\n'
        "Rules:\n"
        "- operation_id must exactly match one of the listed operationIds\n"
        "- args should only include needed parameters (omit null/empty)\n"
        "- For path params, use their exact names from the operation\n"
        "- For query params, use their exact names\n"
        "- For body fields, use their field names directly\n"
    )
    user_prompt = f"Spec:\n{spec_summary}\n\nQuestion: {req.query}"

    try:
        raw = await ai_client.chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            max_tokens=400, temperature=0.0,
        )
        cleaned = re.sub(r"^```[a-z]*\n?|\n?```$", "", raw.strip())
        intent = json.loads(cleaned)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GPT-4o failed: {exc}")

    op_id = intent.get("operation_id", "")
    args = intent.get("args", {})
    explanation = intent.get("explanation", "")

    op = oapi_spec.operations.get(op_id)
    if not op:
        raise HTTPException(
            status_code=422,
            detail=f"GPT-4o chose unknown operation '{op_id}'. Available: {list(oapi_spec.operations.keys())[:20]}",
        )

    try:
        result = await openapi_executor.execute(
            operation=op,
            spec=oapi_spec,
            args=args,
            auth_headers=app_state.auth_headers.get(spec_id, {}),
            auth_params=app_state.auth_params.get(spec_id, {}),
            auth_cookies=app_state.auth_cookies.get(spec_id, {}),
            spec_id=spec_id,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"API error {exc.response.status_code}: {exc.response.text[:300]}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"API call failed: {exc}")

    return OpenAPIQueryResponse(
        query=req.query,
        spec_id=spec_id,
        operation_id=op_id,
        method=op.method,
        path=op.path,
        args_used=args,
        explanation=explanation,
        result=result,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )


@app.post("/api/v1/ask", response_model=UnifiedQueryResponse, tags=["Query"], summary="Ask anything — auto-detects spec type and routes")
async def ask(body: dict[str, Any]):
    """
    **Universal query endpoint. Just provide a query — spec_id is always optional.**

    Auto-detects whether the loaded spec is OData or OpenAPI and routes accordingly.
    If multiple specs are loaded, GPT-4o picks the right one based on the query.

    ```
    {"query": "Get store inventory"}
    {"query": "List top 5 products by price", "spec_id": "nw"}
    ```
    """
    query_text = body.get("query", "")
    spec_id_hint = body.get("spec_id")

    if not query_text:
        raise HTTPException(status_code=422, detail="'query' field is required.")

    spec_id = await _resolve_spec_id(spec_id_hint, query_text)
    t0 = time.monotonic()

    # Route to OpenAPI or OData based on what's loaded for this spec_id
    if spec_id in app_state.openapi_specs:
        oapi_spec = app_state.openapi_specs[spec_id]
        summary_lines = [f"OpenAPI {oapi_spec.openapi_version} — {oapi_spec.title}", "Operations:"]
        for op_id, op in list(oapi_spec.operations.items())[:40]:
            params = [p.name for p in op.parameters if p.location != "header"]
            if op.request_body_schema:
                params += list(op.request_body_schema.keys())[:4]
            summary_lines.append(
                f"  {op_id}: {op.method} {op.path}"
                + (f" params=[{', '.join(params[:5])}]" if params else "")
                + (f" — {op.summary}" if op.summary else "")
            )

        system_prompt = (
            "You are an OpenAPI query assistant. Return ONLY a JSON object.\n"
            '{"operation_id":"<id>","args":{"param":"value"},"explanation":"one sentence"}\n'
            "operation_id must exactly match one listed. Only include needed args."
        )
        user_prompt = f"Spec:\n{chr(10).join(summary_lines)}\n\nQuestion: {query_text}"

        try:
            raw = await ai_client.chat_completion(
                [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                max_tokens=400, temperature=0.0,
            )
            intent = json.loads(re.sub(r"^```[a-z]*\n?|\n?```$", "", raw.strip()))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"GPT-4o failed: {exc}")

        op_id = intent.get("operation_id", "")
        args = intent.get("args", {})
        op = oapi_spec.operations.get(op_id)
        if not op:
            raise HTTPException(status_code=422, detail=f"Unknown operation '{op_id}'. Available: {list(oapi_spec.operations.keys())[:10]}")

        try:
            result = await openapi_executor.execute(
                operation=op, spec=oapi_spec, args=args,
                auth_headers=app_state.auth_headers.get(spec_id, {}),
                auth_params=app_state.auth_params.get(spec_id, {}),
                auth_cookies=app_state.auth_cookies.get(spec_id, {}),
                spec_id=spec_id,
            )
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"API error {exc.response.status_code}: {exc.response.text[:300]}")

        return UnifiedQueryResponse(
            query=query_text, spec_id=spec_id, spec_type="openapi",
            tool_called=f"{spec_id}__{op_id}",
            explanation=intent.get("explanation", ""),
            result=result, elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    else:
        spec = app_state.specs[spec_id]
        system_prompt = (
            "You are an OData query assistant. Return ONLY a JSON object.\n"
            '{"entity_set":"<name>","operation":"list|get|create|update|delete",'
            '"parameters":{},"explanation":"one sentence"}\n'
            "list uses filter/select/orderby/top/skip/expand. get/delete use key fields."
        )
        user_prompt = f"Spec:\n{_spec_summary(spec, spec_id)}\n\nQuestion: {query_text}"

        try:
            raw = await ai_client.chat_completion(
                [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                max_tokens=400, temperature=0.0,
            )
            intent = json.loads(re.sub(r"^```[a-z]*\n?|\n?```$", "", raw.strip()))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"GPT-4o failed: {exc}")

        es_name = intent.get("entity_set", "")
        operation = intent.get("operation", "list")
        args = intent.get("parameters", {})

        try:
            result = await executor.execute(
                operation=operation, spec=spec, entity_set_name=es_name, args=args,
                auth_headers=app_state.auth_headers.get(spec_id, {}), spec_id=spec_id,
            )
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"OData error {exc.response.status_code}: {exc.response.text[:300]}")

        return UnifiedQueryResponse(
            query=query_text, spec_id=spec_id, spec_type="odata",
            tool_called=f"{spec_id}__{_sanitize_name(es_name)}__{operation}",
            explanation=intent.get("explanation", ""),
            result=result, elapsed_ms=int((time.monotonic() - t0) * 1000),
        )


@app.post("/api/v1/detect-auth", tags=["Auth"], summary="Probe a URL to detect what auth is required")
async def detect_auth(body: dict[str, Any]):
    """
    **Probe a service URL without credentials to detect what auth is required.**

    Returns the exact env var names to set in `.env` — credentials are NEVER
    passed through this API.  After setting them, restart (or they load on startup)
    and call `/generate/openapi` — auth is applied automatically at runtime.

    ```
    {"url": "https://myapp.cfapps.eu10.hana.ondemand.com", "spec_id": "fresenius"}
    ```
    """
    url = body.get("url", "").rstrip("/")
    spec_id = body.get("spec_id", "")

    if not url:
        raise HTTPException(status_code=422, detail="'url' field is required")

    # Probe several common paths without auth; stop on first definitive response
    probe_paths = ["/", "/health", "/api/v1/health", "/status", "/ping"]
    probe_response = None
    probed_path = "/"

    for path in probe_paths:
        try:
            async with httpx.AsyncClient(timeout=10, verify=False, follow_redirects=False) as client:
                resp = await client.get(
                    f"{url}{path}",
                    headers={"Accept": "application/json"},
                )
            probe_response = resp
            probed_path = path
            if resp.status_code in (200, 401, 403):
                break
        except Exception:
            continue

    if probe_response is None:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach '{url}' — verify the URL and network access.",
        )

    status_code = probe_response.status_code
    auth_required = status_code in (401, 403)
    www_auth = probe_response.headers.get("www-authenticate", "")
    body_text = probe_response.text.lower()

    # Detect auth type
    auth_type = "none"
    is_xsuaa = False

    if auth_required:
        www_lower = www_auth.lower()
        xsuaa_markers = ["xsuaa", "sap.com", "cfapps", "hana.ondemand.com", "authentication.sap", ".sap."]
        if "bearer" in www_lower:
            auth_type = "Bearer (OAuth2/JWT)"
            if any(m in www_lower or m in body_text for m in xsuaa_markers):
                auth_type = "Bearer (SAP XSUAA)"
                is_xsuaa = True
        elif "basic" in www_lower:
            auth_type = "Basic"
        elif status_code == 401:
            auth_type = "Bearer or API Key (check WWW-Authenticate header)"
        else:
            auth_type = "Unknown (403 Forbidden — may be IP restriction or role issue)"

    # Check if credentials already configured via env vars
    configured = auth_manager.is_configured(spec_id) if spec_id else False
    token_status_info = auth_manager.token_status(spec_id) if spec_id else {}

    result: dict[str, Any] = {
        "url": url,
        "probe_path": probed_path,
        "status_without_auth": status_code,
        "auth_required": auth_required,
        "auth_type": auth_type,
        "www_authenticate": www_auth or None,
        "is_xsuaa": is_xsuaa,
        "configured": configured,
        "token_status": token_status_info,
    }

    if auth_required:
        prefix = spec_id.upper().replace("-", "_").replace(".", "_") if spec_id else "MYSPEC"
        if is_xsuaa or "bearer" in auth_type.lower() or "oauth" in auth_type.lower():
            result["env_vars_to_set"] = {
                f"{prefix}_AUTH_TYPE": "xsuaa",
                f"{prefix}_AUTH_URL": "<url field from XSUAA service credentials JSON>",
                f"{prefix}_CLIENT_ID": "<clientid field from XSUAA service credentials JSON>",
                f"{prefix}_CLIENT_SECRET": "<clientsecret field from XSUAA service credentials JSON>",
            }
            result["how_to_get_creds"] = (
                "BTP Cockpit → Space → Service Instances → XSUAA instance → "
                "View Credentials — copy the 'url', 'clientid', and 'clientsecret' fields."
            )
        elif "basic" in auth_type.lower():
            result["env_vars_to_set"] = {
                f"{prefix}_AUTH_TYPE": "basic",
                f"{prefix}_USERNAME": "<your username>",
                f"{prefix}_PASSWORD": "<your password>",
            }
        result["next_step"] = (
            "1. Add these variables to your .env file on the server.\n"
            "2. Restart api.py (it loads .env automatically via python-dotenv).\n"
            f"3. Call POST /api/v1/generate/openapi with just the spec URL and spec_id '{spec_id or 'yourspec'}' — "
            "credentials are picked up automatically; nothing secret goes in the request."
        )

    return result


@app.get("/api/v1/auth-status/{spec_id}", tags=["Auth"], summary="Check auth configuration status for a spec")
async def auth_status(spec_id: str = Path(..., description="Spec ID to check")):
    """
    Returns whether auth is configured for a spec via env vars, and the current
    token cache status.  **Never returns credential values.**
    """
    config = auth_manager.get_config(spec_id)
    env_names = auth_manager.env_var_names(spec_id)
    return {
        "spec_id": spec_id,
        "configured": config is not None,
        "auth_type": config.auth_type if config else None,
        "auth_url_set": bool(config.auth_url) if config else False,
        "client_id_set": bool(config.client_id) if config else False,
        "client_secret_set": bool(config.client_secret) if config else False,
        "token_status": auth_manager.token_status(spec_id),
        "env_var_names": env_names,
    }


@app.post("/api/v1/auth/invalidate/{spec_id}", tags=["Auth"], summary="Force token refresh on next call")
async def invalidate_token(spec_id: str = Path(..., description="Spec ID whose cached token to clear")):
    """Force the cached OAuth2/XSUAA token for a spec to be refreshed on the next API call."""
    auth_manager.invalidate(spec_id)
    return {"spec_id": spec_id, "status": "token invalidated — will refresh on next call"}


@app.get("/api/v1/specs", response_model=list[SpecInfo], tags=["Tools"], summary="List all loaded specs")
async def list_specs():
    """List every OData and OpenAPI spec currently in memory, with entity/operation counts."""
    result = []
    for sid, spec in app_state.specs.items():
        result.append(SpecInfo(
            spec_id=sid,
            spec_type="odata",
            title=spec.namespace or sid,
            version=spec.version,
            base_url=spec.service_url,
            entity_count=len(spec.entity_sets),
            tool_count=len(app_state.tools_for_spec(sid)),
        ))
    for sid, spec in app_state.openapi_specs.items():
        result.append(SpecInfo(
            spec_id=sid,
            spec_type="openapi",
            title=spec.title or sid,
            version=spec.openapi_version,
            base_url=spec.base_url,
            entity_count=len(spec.operations),
            tool_count=len(app_state.tools_for_spec(sid)),
        ))
    return result


@app.get("/api/v1/files", response_model=list[FileInfo], tags=["Files"], summary="List generated tool files")
async def list_files():
    """List all `tools/*.py` files on disk."""
    files = []
    d = tool_generator.TOOLS_DIR
    if os.path.isdir(d):
        for fname in sorted(os.listdir(d)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            fpath = os.path.join(d, fname)
            st = os.stat(fpath)
            files.append(FileInfo(
                spec_id=fname[:-3],
                file=fpath,
                size_kb=round(st.st_size / 1024, 1),
                modified=datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            ))
    return files


@app.delete("/api/v1/files/{spec_id}", status_code=204, tags=["Files"], summary="Delete a tool file")
async def delete_file(spec_id: str = Path(..., description="Spec ID of the file to delete")):
    """Delete `tools/{spec_id}.py` from disk. Does not unload the in-memory spec."""
    fpath = os.path.join(tool_generator.TOOLS_DIR, f"{spec_id}.py")
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail=f"No tool file for spec '{spec_id}'.")
    os.remove(fpath)
    logger.info("Deleted %s", fpath)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = 8080
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    print(f"OData API  →  http://localhost:{port}")
    print(f"Docs       →  http://localhost:{port}/docs")

    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False, log_level="info")

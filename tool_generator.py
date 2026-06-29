"""
Generates persistent Python tool files from OData $metadata specs.

Each generated file tools/{spec_id}.py:
  - Embeds the spec XML as base64 (survives restarts without network access)
  - Defines a register(mcp, executor, app_state) -> int function
  - Contains @mcp.tool() decorated functions with proper type annotations
  - Has AI-generated docstrings when requested

Public API:
    generate_file_content(spec_id, spec, xml, base_url, annotations) -> str
    save_tool_file(spec_id, content) -> str           # returns path
    load_all_tool_files(mcp, executor, app_state) -> int
    async generate_annotations(spec, ai_client) -> dict
"""

import base64
import importlib.util
import json
import keyword
import os
import re
import sys
from datetime import datetime

from odata_parser import EntityType, ODataSpec, _sanitize_name

TOOLS_DIR = os.path.join(os.path.dirname(__file__), "tools")
os.makedirs(TOOLS_DIR, exist_ok=True)

# ── Type mapping ───────────────────────────────────────────────────────────────

_EDM_TO_PY: dict[str, str] = {
    "Edm.Int16": "int", "Edm.Int32": "int", "Edm.Int64": "int",
    "Edm.Byte": "int", "Edm.SByte": "int",
    "Edm.Single": "float", "Edm.Double": "float", "Edm.Decimal": "float",
    "Edm.Boolean": "bool",
}


def _py_type(edm_type: str) -> str:
    return _EDM_TO_PY.get(edm_type, "str")


def _safe_name(name: str) -> str:
    """Prefix Python keywords to avoid syntax errors in generated code."""
    return f"p_{name}" if keyword.iskeyword(name) else name


# ── Per-operation code generators ─────────────────────────────────────────────

def _gen_list_fn(spec_id: str, es_name: str, ann: dict) -> list[str]:
    fn = f"{_sanitize_name(spec_id)}__{_sanitize_name(es_name)}__list"
    op_desc = ann.get("list", f"List {es_name} records.")
    entity_desc = ann.get("entity", "")

    L = []
    L.append(f"    @mcp.tool()")
    L.append(f"    async def {fn}(")
    L.append(f"        filter: Optional[str] = None,")
    L.append(f"        select: Optional[str] = None,")
    L.append(f"        orderby: Optional[str] = None,")
    L.append(f"        top: Optional[int] = None,")
    L.append(f"        skip: Optional[int] = None,")
    L.append(f"        expand: Optional[str] = None,")
    L.append(f"        count: Optional[bool] = None,")
    L.append(f"        search: Optional[str] = None,")
    L.append(f"    ) -> str:")
    L.append(f'        """{op_desc}')
    if entity_desc:
        L.append(f"")
        L.append(f"        {entity_desc}")
    L.append(f"")
    L.append(f"        Args:")
    L.append(f"            filter: OData $filter expression (e.g. \"Name eq 'value'\")")
    L.append(f"            select: Comma-separated fields to return (e.g. \"ID,Name\")")
    L.append(f"            orderby: Sort expression (e.g. \"CreatedAt desc\")")
    L.append(f"            top: Maximum number of records to return")
    L.append(f"            skip: Records to skip for pagination")
    L.append(f"            expand: Navigation properties to expand")
    L.append(f"            count: Include total record count in response")
    L.append(f"            search: Full-text search expression")
    L.append(f'        """')
    # Dict comprehension — use plain strings (no f-string) to keep { and } literal
    L.append('        args = {k: v for k, v in {')
    L.append('            "filter": filter, "select": select, "orderby": orderby,')
    L.append('            "top": top, "skip": skip, "expand": expand,')
    L.append('            "count": count, "search": search,')
    L.append('        }.items() if v is not None}')
    L.append('        return json.dumps(await executor.execute(')
    L.append(f'            operation="list", spec=_spec(), entity_set_name="{es_name}",')
    L.append('            args=args, auth_headers=app_state.auth_headers.get(SPEC_ID, {}),')
    L.append('            spec_id=SPEC_ID,')
    L.append('        ), ensure_ascii=False)')
    L.append('    _tool_count += 1')
    L.append('')
    return L


def _gen_get_fn(spec_id: str, es_name: str, et: EntityType, ann: dict) -> list[str]:
    fn = f"{_sanitize_name(spec_id)}__{_sanitize_name(es_name)}__get"
    op_desc = ann.get("get", f"Get a single {es_name} record by key.")
    entity_desc = ann.get("entity", "")

    # Key params with types
    key_params = []
    key_docs = []
    for key in et.key_properties:
        prop = et.get_property(key)
        py_t = _py_type(prop.edm_type if prop else "Edm.String")
        safe = _safe_name(key)
        key_params.append(f"        {safe}: {py_t},")
        key_docs.append(f"            {safe}: Primary key — {key}")

    L = []
    L.append(f"    @mcp.tool()")
    L.append(f"    async def {fn}(")
    L.extend(key_params)
    L.append(f"        expand: Optional[str] = None,")
    L.append(f"        select: Optional[str] = None,")
    L.append(f"    ) -> str:")
    L.append(f'        """{op_desc}')
    if entity_desc:
        L.append(f"")
        L.append(f"        {entity_desc}")
    L.append(f"")
    L.append(f"        Args:")
    L.extend(key_docs)
    L.append(f"            expand: Navigation properties to expand")
    L.append(f"            select: Fields to include in response")
    L.append(f'        """')
    # Build key dict
    key_dict_items = ", ".join(f'"{k}": {_safe_name(k)}' for k in et.key_properties)
    L.append(f"        args = {{{key_dict_items}}}")
    L.append('        if expand is not None: args["expand"] = expand')
    L.append('        if select is not None: args["select"] = select')
    L.append('        return json.dumps(await executor.execute(')
    L.append(f'            operation="get", spec=_spec(), entity_set_name="{es_name}",')
    L.append('            args=args, auth_headers=app_state.auth_headers.get(SPEC_ID, {}),')
    L.append('            spec_id=SPEC_ID,')
    L.append('        ), ensure_ascii=False)')
    L.append('    _tool_count += 1')
    L.append('')
    return L


def _gen_create_fn(spec_id: str, es_name: str, et: EntityType, ann: dict) -> list[str]:
    fn = f"{_sanitize_name(spec_id)}__{_sanitize_name(es_name)}__create"
    op_desc = ann.get("create", f"Create a new {es_name} record.")
    entity_desc = ann.get("entity", "")

    non_key_props = [p for p in et.properties if not p.is_key]

    L = []
    L.append(f"    @mcp.tool()")
    L.append(f'    @requires_role("admin")')
    L.append(f"    async def {fn}(")
    for prop in non_key_props:
        py_t = _py_type(prop.edm_type)
        safe = _safe_name(prop.name)
        L.append(f"        {safe}: Optional[{py_t}] = None,")
    L.append(f"    ) -> str:")
    L.append(f'        """{op_desc}')
    if entity_desc:
        L.append(f"")
        L.append(f"        {entity_desc}")
    if non_key_props:
        L.append(f"")
        L.append(f"        Args:")
        for prop in non_key_props:
            safe = _safe_name(prop.name)
            req = "" if prop.nullable else " [Required]"
            L.append(f"            {safe}: {prop.edm_type}{req}")
    L.append(f'        """')
    # Build args dict filtering None
    items = ", ".join(f'"{p.name}": {_safe_name(p.name)}' for p in non_key_props)
    L.append(f"        args = " + "{k: v for k, v in {" + items + "}.items() if v is not None}")
    L.append('        return json.dumps(await executor.execute(')
    L.append(f'            operation="create", spec=_spec(), entity_set_name="{es_name}",')
    L.append('            args=args, auth_headers=app_state.auth_headers.get(SPEC_ID, {}),')
    L.append('            spec_id=SPEC_ID,')
    L.append('        ), ensure_ascii=False)')
    L.append('    _tool_count += 1')
    L.append('')
    return L


def _gen_update_fn(spec_id: str, es_name: str, et: EntityType, ann: dict) -> list[str]:
    fn = f"{_sanitize_name(spec_id)}__{_sanitize_name(es_name)}__update"
    op_desc = ann.get("update", f"Update an existing {es_name} record (PATCH).")
    entity_desc = ann.get("entity", "")

    key_props = et.key_properties
    non_key_props = [p for p in et.properties if not p.is_key]

    L = []
    L.append(f"    @mcp.tool()")
    L.append(f'    @requires_role("admin")')
    L.append(f"    async def {fn}(")
    # Key params first (required)
    for key in key_props:
        prop = et.get_property(key)
        py_t = _py_type(prop.edm_type if prop else "Edm.String")
        L.append(f"        {_safe_name(key)}: {py_t},")
    # Non-key params (optional)
    for prop in non_key_props:
        py_t = _py_type(prop.edm_type)
        L.append(f"        {_safe_name(prop.name)}: Optional[{py_t}] = None,")
    L.append(f"    ) -> str:")
    L.append(f'        """{op_desc}')
    if entity_desc:
        L.append(f"")
        L.append(f"        {entity_desc}")
    L.append(f"")
    L.append(f"        Args:")
    for key in key_props:
        L.append(f"            {_safe_name(key)}: Primary key identifying the record to update")
    for prop in non_key_props:
        L.append(f"            {_safe_name(prop.name)}: {prop.edm_type} field to update")
    L.append(f'        """')
    # Build full args dict (executor separates keys from body internally)
    all_names = key_props + [p.name for p in non_key_props]
    items = ", ".join(f'"{n}": {_safe_name(n)}' for n in all_names)
    L.append(f"        args = " + "{k: v for k, v in {" + items + "}.items() if v is not None}")
    L.append('        return json.dumps(await executor.execute(')
    L.append(f'            operation="update", spec=_spec(), entity_set_name="{es_name}",')
    L.append('            args=args, auth_headers=app_state.auth_headers.get(SPEC_ID, {}),')
    L.append('            spec_id=SPEC_ID,')
    L.append('        ), ensure_ascii=False)')
    L.append('    _tool_count += 1')
    L.append('')
    return L


def _gen_delete_fn(spec_id: str, es_name: str, et: EntityType, ann: dict) -> list[str]:
    fn = f"{_sanitize_name(spec_id)}__{_sanitize_name(es_name)}__delete"
    op_desc = ann.get("delete", f"Delete a {es_name} record by key.")
    entity_desc = ann.get("entity", "")

    L = []
    L.append(f"    @mcp.tool()")
    L.append(f'    @requires_role("admin")')
    L.append(f"    async def {fn}(")
    key_docs = []
    for key in et.key_properties:
        prop = et.get_property(key)
        py_t = _py_type(prop.edm_type if prop else "Edm.String")
        safe = _safe_name(key)
        L.append(f"        {safe}: {py_t},")
        key_docs.append(f"            {safe}: Primary key of the record to delete")
    L.append(f"    ) -> str:")
    L.append(f'        """{op_desc}')
    if entity_desc:
        L.append(f"")
        L.append(f"        {entity_desc}")
    L.append(f"")
    L.append(f"        Args:")
    L.extend(key_docs)
    L.append(f'        """')
    key_dict_items = ", ".join(f'"{k}": {_safe_name(k)}' for k in et.key_properties)
    L.append(f"        args = {{{key_dict_items}}}")
    L.append('        return json.dumps(await executor.execute(')
    L.append(f'            operation="delete", spec=_spec(), entity_set_name="{es_name}",')
    L.append('            args=args, auth_headers=app_state.auth_headers.get(SPEC_ID, {}),')
    L.append('            spec_id=SPEC_ID,')
    L.append('        ), ensure_ascii=False)')
    L.append('    _tool_count += 1')
    L.append('')
    return L


def _gen_entity_section(spec_id: str, es_name: str, spec: ODataSpec, ann: dict) -> list[str]:
    et = spec.resolve_entity_type(es_name)
    bar = "─" * max(0, 56 - len(es_name))
    L = [f"", f"    # ── {es_name} {bar}"]

    if not et or not et.key_properties:
        # No key → list-only entity
        L.extend(_gen_list_fn(spec_id, es_name, ann))
        return L

    L.extend(_gen_list_fn(spec_id, es_name, ann))
    L.extend(_gen_get_fn(spec_id, es_name, et, ann))
    L.extend(_gen_create_fn(spec_id, es_name, et, ann))
    L.extend(_gen_update_fn(spec_id, es_name, et, ann))
    L.extend(_gen_delete_fn(spec_id, es_name, et, ann))
    return L


# ── File assembly ──────────────────────────────────────────────────────────────

def generate_file_content(
    spec_id: str,
    spec: ODataSpec,
    xml_content: str,
    base_url: str,
    annotations: dict,
) -> str:
    """
    Assemble the full Python source for tools/{spec_id}.py.

    annotations: {EntitySetName: {entity, list, get, create, update, delete}}
    """
    sid = _sanitize_name(spec_id)
    xml_b64 = base64.b64encode(xml_content.encode("utf-8")).decode("ascii")
    now = datetime.now().isoformat(timespec="seconds")
    es_names = list(spec.entity_sets.keys())

    L = []

    # ── Module docstring ──
    L.append('"""')
    L.append(f"OData tools for spec: {spec_id}")
    L.append(f"Namespace: {spec.namespace}")
    L.append(f"Base URL: {base_url}")
    L.append(f"OData Version: {spec.version}")
    L.append(f"Generated: {now}")
    L.append(f"Entity Sets ({len(es_names)}): {', '.join(es_names[:10])}" +
             (f" +{len(es_names)-10} more" if len(es_names) > 10 else ""))
    L.append("")
    L.append("AUTO-GENERATED — re-generate by calling generate_tool_file() in the MCP server.")
    L.append('"""')
    L.append("")

    # ── Imports ──
    L.append("import base64")
    L.append("import json")
    L.append("from typing import Optional")
    L.append("from rbac_manager import requires_role")
    L.append("from rbac_manager import rbac, PermissionDenied")
    L.append("")

    # ── Constants ──
    L.append(f"SPEC_ID = {json.dumps(sid)}")
    L.append(f"BASE_URL = {json.dumps(base_url)}")
    L.append(f"ODATA_VERSION = {json.dumps(spec.version)}")
    L.append(f"_SPEC_XML_B64 = {json.dumps(xml_b64)}")
    L.append("SPEC_XML = base64.b64decode(_SPEC_XML_B64).decode('utf-8')")
    L.append("")
    L.append("")

    # ── register() ──
    L.append("def register(mcp, executor, app_state) -> int:")
    L.append(f'    """Register all OData tools for spec {json.dumps(sid)}.')
    L.append("")
    L.append("    Auto-called at startup when this file is in the tools/ directory.")
    L.append("    Returns the number of tools registered.")
    L.append('    """')
    L.append("    from odata_parser import parse_odata_metadata")
    L.append("")
    L.append("    def _spec():")
    L.append("        if SPEC_ID not in app_state.specs:")
    L.append("            app_state.specs[SPEC_ID] = parse_odata_metadata(SPEC_XML, service_url=BASE_URL)")
    L.append("        return app_state.specs[SPEC_ID]")
    L.append("")
    L.append("    _tool_count = 0")

    # ── Tool functions for each EntitySet ──
    for es_name in es_names:
        ann = annotations.get(es_name, {})
        L.extend(_gen_entity_section(sid, es_name, spec, ann))

    L.append("")
    L.append("    return _tool_count")
    L.append("")

    return "\n".join(L)


# ── Save / load ────────────────────────────────────────────────────────────────

def save_tool_file(spec_id: str, content: str) -> str:
    """Write generated code to tools/{spec_id}.py. Returns the file path."""
    path = os.path.join(TOOLS_DIR, f"{_sanitize_name(spec_id)}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def load_all_tool_files(mcp, executor, app_state, openapi_executor=None) -> int:
    """
    Scan tools/*.py files, import each one, and call module.register().

    OData files (have ODATA_VERSION attr)  → register(mcp, executor, app_state)
    OpenAPI files (have OPENAPI_VERSION attr) → register(mcp, openapi_executor, app_state)

    Returns total number of tools registered.
    """
    total = 0
    if not os.path.isdir(TOOLS_DIR):
        return 0

    for fname in sorted(os.listdir(TOOLS_DIR)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        path = os.path.join(TOOLS_DIR, fname)
        try:
            mod_name = f"tool_file_{fname[:-3]}"
            file_spec = importlib.util.spec_from_file_location(mod_name, path)
            module = importlib.util.module_from_spec(file_spec)
            file_spec.loader.exec_module(module)
            if not hasattr(module, "register"):
                continue
            if hasattr(module, "OPENAPI_VERSION"):
                if openapi_executor is None:
                    print(f"[tool_generator] Skipping OpenAPI file {fname}: no openapi_executor provided", file=sys.stderr)
                    continue
                count = module.register(mcp, openapi_executor, app_state)
            else:
                count = module.register(mcp, executor, app_state)
            total += count or 0
        except Exception as exc:
            print(f"[tool_generator] Failed to load {fname}: {exc}", file=sys.stderr)

    return total


# ── AI annotation generation ───────────────────────────────────────────────────

async def generate_annotations(spec: ODataSpec, ai_client) -> dict:
    """
    Call GPT-4o via SAP AI Core to generate descriptions for every EntitySet.

    Returns dict: {EntitySetName: {entity, list, get, create, update, delete}}
    """
    # Build a compact spec summary for the prompt
    summary_lines = [
        f"OData {spec.version} API — namespace: {spec.namespace}",
        "EntitySets and their key/property fields:",
    ]
    for es_name in list(spec.entity_sets.keys())[:40]:  # cap at 40 to keep prompt size reasonable
        et = spec.resolve_entity_type(es_name)
        if et:
            keys = ", ".join(et.key_properties)
            sample_props = ", ".join(p.name for p in et.properties[:6])
            extra = f"+{len(et.properties)-6} more" if len(et.properties) > 6 else ""
            summary_lines.append(f"  {es_name}: keys=[{keys}] props=[{sample_props} {extra}]")
        else:
            summary_lines.append(f"  {es_name}")
    spec_summary = "\n".join(summary_lines)

    system_prompt = (
        "You are an API documentation generator. "
        "Return ONLY a valid JSON object — no markdown, no explanation, just raw JSON. "
        'Each key is an EntitySet name, each value has keys: "entity", "list", "get", "create", "update", "delete". '
        "Each value is a single concise sentence (max 15 words)."
    )

    user_prompt = (
        f"Generate descriptions for this OData API spec:\n\n{spec_summary}\n\n"
        "Return JSON with this exact shape:\n"
        '{\n  "EntitySetName": {\n'
        '    "entity": "What this entity represents.",\n'
        '    "list": "List operation description.",\n'
        '    "get": "Get by key description.",\n'
        '    "create": "Create description.",\n'
        '    "update": "Update description.",\n'
        '    "delete": "Delete description."\n'
        "  }\n}"
    )

    try:
        raw = await ai_client.chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=3000,
            temperature=0.3,
        )
        # Strip markdown code fences if present
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        return json.loads(text.strip())
    except Exception as exc:
        print(f"[tool_generator] AI annotation failed: {exc}", file=sys.stderr)
        return {}


# ── OpenAPI tool file generation ───────────────────────────────────────────────

def _json_type_to_py(json_type: str) -> str:
    return {
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "array": "list",
        "object": "dict",
    }.get(json_type, "str")


def _gen_openapi_tool_fn(spec_id: str, op, ann: dict) -> list[str]:
    """Generate lines for a single @mcp.tool() function for an OpenAPI operation."""
    fn = f"{_sanitize_name(spec_id)}__{op.operation_id}"
    desc = ann.get("description") or op.summary or op.description or f"{op.method} {op.path}"
    if op.tags:
        desc = f"[{', '.join(op.tags)}] {desc}"

    # Collect required and optional params (skip header params — handled by auth)
    required_params: list[tuple[str, str, str]] = []  # (safe_name, py_type, doc)
    optional_params: list[tuple[str, str, str]] = []

    for param in op.parameters:
        if param.location == "header":
            continue
        py_t = _json_type_to_py(param.json_schema.get("type", "string"))
        safe = _safe_name(param.name)
        doc = param.description or f"{param.name} ({param.location})"
        if param.required:
            required_params.append((safe, py_t, doc))
        else:
            optional_params.append((safe, py_t, doc))

    # Body fields (from flattened request body schema)
    if op.request_body_schema:
        existing_names = {n for n, _, _ in required_params} | {n for n, _, _ in optional_params}
        for field_name, field_schema in op.request_body_schema.items():
            py_t = _json_type_to_py(field_schema.get("type", "string"))
            safe = _safe_name(field_name)
            if safe in existing_names:
                safe = f"body_{safe}"
            existing_names.add(safe)
            doc = field_schema.get("description", field_name)
            optional_params.append((safe, py_t, doc))
    elif op.body_param_name:
        req = op.request_body_required
        doc = "Request body as JSON string" + (" (required)" if req else "")
        if req:
            required_params.append((op.body_param_name, "str", doc))
        else:
            optional_params.append((op.body_param_name, "str", doc))

    is_write = op.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}

    L: list[str] = []
    L.append(f"    @mcp.tool()")
    if is_write:
        L.append(f'    @requires_role("admin")')
    L.append(f"    async def {fn}(")
    for name, py_t, _ in required_params:
        L.append(f"        {name}: {py_t},")
    for name, py_t, _ in optional_params:
        L.append(f"        {name}: Optional[{py_t}] = None,")
    L.append(f"    ) -> str:")

    # Docstring
    L.append(f'        """{desc}')
    L.append(f"")
    L.append(f"        {op.method} {op.path}")
    if required_params or optional_params:
        L.append(f"")
        L.append(f"        Args:")
        for name, _, doc in required_params:
            L.append(f"            {name}: {doc}")
        for name, _, doc in optional_params:
            L.append(f"            {name}: {doc} (optional)")
    L.append(f'        """')

    # Body: build args dict, filtering None values
    all_names = [n for n, _, _ in required_params + optional_params]
    items_str = ", ".join(f'"{n}": {n}' for n in all_names)
    L.append(f"        args = " + "{k: v for k, v in {" + items_str + "}.items() if v is not None}")

    L.append(f"        spec = _spec()")
    L.append(f"        op = spec.operations.get({json.dumps(op.operation_id)})")
    L.append(f"        if not op:")
    L.append(f'            return json.dumps({{"error": "Operation {op.operation_id!r} not found in spec"}})')
    L.append(f"        try:")
    L.append(f"            result = await openapi_executor.execute(")
    L.append(f"                operation=op,")
    L.append(f"                spec=spec,")
    L.append(f"                args=args,")
    L.append(f"                auth_headers=app_state.auth_headers.get(SPEC_ID, {{}}),")
    L.append(f"                auth_params=app_state.auth_params.get(SPEC_ID, {{}}),")
    L.append(f"                auth_cookies=app_state.auth_cookies.get(SPEC_ID, {{}}),")
    L.append(f"                spec_id=SPEC_ID,")
    L.append(f"            )")
    L.append(f"            return json.dumps(result, ensure_ascii=False)")
    L.append(f"        except Exception as exc:")
    L.append(f'            return json.dumps({{"error": str(exc)}})')
    L.append(f"    _tool_count += 1")
    L.append(f"")
    return L


def generate_openapi_file_content(
    spec_id: str,
    spec,                # OpenAPISpec
    raw_content: str,
    annotations: dict,
) -> str:
    """
    Assemble the full Python source for tools/{spec_id}.py for an OpenAPI spec.

    annotations: {operation_id: {"description": "..."}}
    """
    sid = _sanitize_name(spec_id)
    content_b64 = base64.b64encode(raw_content.encode("utf-8")).decode("ascii")
    now = datetime.now().isoformat(timespec="seconds")
    op_ids = list(spec.operations.keys())

    L: list[str] = []

    # Module docstring
    L.append('"""')
    L.append(f"OpenAPI tools for spec: {spec_id}")
    L.append(f"Title: {spec.title}")
    L.append(f"Base URL: {spec.base_url}")
    L.append(f"OpenAPI Version: {spec.openapi_version}")
    L.append(f"Generated: {now}")
    count_str = f"Operations ({len(op_ids)}): {', '.join(op_ids[:10])}"
    if len(op_ids) > 10:
        count_str += f" +{len(op_ids)-10} more"
    L.append(count_str)
    L.append("")
    L.append("AUTO-GENERATED — re-generate by calling generate_tool_file() in the MCP server.")
    L.append('"""')
    L.append("")
    L.append("import base64")
    L.append("import json")
    L.append("from typing import Optional")
    L.append("from rbac_manager import requires_role")
    L.append("")
    L.append(f"SPEC_ID = {json.dumps(sid)}")
    L.append(f"BASE_URL = {json.dumps(spec.base_url)}")
    L.append(f"OPENAPI_VERSION = {json.dumps(spec.openapi_version)}")
    L.append(f"_SPEC_CONTENT_B64 = {json.dumps(content_b64)}")
    L.append("SPEC_CONTENT = base64.b64decode(_SPEC_CONTENT_B64).decode('utf-8')")
    L.append("")
    L.append("")
    L.append("def register(mcp, openapi_executor, app_state) -> int:")
    L.append(f'    """Register all OpenAPI tools for spec {json.dumps(sid)}.')
    L.append("")
    L.append("    Auto-called at startup when this file is in the tools/ directory.")
    L.append("    Auth headers/params must be set in app_state before calling APIs.")
    L.append('    """')
    L.append("    from openapi_parser import parse_openapi_spec")
    L.append("")
    L.append("    def _spec():")
    L.append("        if SPEC_ID not in app_state.openapi_specs:")
    L.append("            app_state.openapi_specs[SPEC_ID] = parse_openapi_spec(SPEC_CONTENT, base_url_override=BASE_URL)")
    L.append("        return app_state.openapi_specs[SPEC_ID]")
    L.append("")
    L.append("    _tool_count = 0")

    for op_id, op in spec.operations.items():
        ann = annotations.get(op_id, {})
        bar = "─" * max(0, 54 - len(op_id))
        L.append(f"")
        L.append(f"    # ── {op_id} {bar}")
        L.extend(_gen_openapi_tool_fn(sid, op, ann))

    L.append("")
    L.append("    return _tool_count")
    L.append("")

    return "\n".join(L)


async def generate_openapi_annotations(spec, ai_client) -> dict:
    """
    Call GPT-4o to generate a one-line description for each OpenAPI operation.

    Returns dict: {operation_id: {"description": "..."}}
    """
    summary_lines = [
        f"OpenAPI {spec.openapi_version} — title: {spec.title}",
        "Operations:",
    ]
    for op_id, op in list(spec.operations.items())[:50]:
        summary_lines.append(
            f"  {op_id}: {op.method} {op.path}"
            + (f" — {op.summary}" if op.summary else "")
        )
    spec_summary = "\n".join(summary_lines)

    system_prompt = (
        "You are an API documentation generator. "
        "Return ONLY a valid JSON object — no markdown, no explanation. "
        'Each key is an operationId, each value is {"description": "one concise sentence max 15 words"}.'
    )
    user_prompt = (
        f"Generate descriptions for this OpenAPI spec:\n\n{spec_summary}\n\n"
        "Return JSON shaped exactly like:\n"
        '{"operationId": {"description": "What this operation does."}}'
    )

    try:
        raw = await ai_client.chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=2000,
            temperature=0.3,
        )
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        return json.loads(text.strip())
    except Exception as exc:
        print(f"[tool_generator] OpenAPI AI annotation failed: {exc}", file=sys.stderr)
        return {}

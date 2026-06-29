"""
OpenAPI 2.0 (Swagger) and 3.x spec parser.
Extracts operations, parameters, request bodies, and security schemes.
Supports JSON and YAML input. Handles $ref resolution internally.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class OpenAPIParameter:
    name: str
    location: str    # "path" | "query" | "header" | "cookie"
    required: bool = False
    json_schema: dict = field(default_factory=lambda: {"type": "string"})
    description: str = ""


@dataclass
class OpenAPIOperation:
    operation_id: str       # sanitized, unique within spec
    raw_id: str             # original operationId or ""
    method: str             # uppercase: GET POST PUT PATCH DELETE ...
    path: str               # /pets/{id}
    summary: str = ""
    description: str = ""
    parameters: list[OpenAPIParameter] = field(default_factory=list)
    request_body_schema: Optional[dict] = None   # flattened {field: json_schema} or None
    request_body_required: bool = False
    body_param_name: Optional[str] = None        # "body" when schema can't be flattened
    security_requirements: list[dict] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def param_locations(self) -> dict[str, str]:
        """Return {param_name: location} mapping for all parameters."""
        return {p.name: p.location for p in self.parameters}

    def required_params(self) -> list[str]:
        return [p.name for p in self.parameters if p.required and p.location != "header"]

    def all_input_names(self) -> list[str]:
        names = [p.name for p in self.parameters if p.location != "header"]
        if self.request_body_schema:
            names.extend(self.request_body_schema.keys())
        elif self.body_param_name:
            names.append(self.body_param_name)
        return names


@dataclass
class SecurityScheme:
    name: str
    type: str           # "apiKey" | "http" | "basic" | "oauth2" | "openIdConnect"
    in_: str = ""       # apiKey: "header" | "query" | "cookie"
    param_name: str = ""  # apiKey: the actual header/param name, e.g. "X-API-Key"
    http_scheme: str = ""  # http: "bearer" | "basic"
    description: str = ""


@dataclass
class OpenAPISpec:
    title: str
    spec_version: str      # from info.version
    openapi_version: str   # "2.0", "3.0.x", "3.1.x"
    servers: list[str] = field(default_factory=list)
    operations: dict[str, OpenAPIOperation] = field(default_factory=dict)
    security_schemes: dict[str, SecurityScheme] = field(default_factory=dict)
    global_security: list[dict] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return self.servers[0] if self.servers else ""


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_content(content: str) -> dict:
    """Parse JSON or YAML string into a dict."""
    content = content.strip()
    # JSON detection: starts with { or [
    if content.startswith(("{", "[")):
        return json.loads(content)
    if _HAS_YAML:
        result = _yaml.safe_load(content)
        if isinstance(result, dict):
            return result
        raise ValueError("YAML content did not parse to a dict")
    # Last resort: try JSON anyway (handles cases like JSON without leading brace whitespace)
    return json.loads(content)


def _sanitize_op_id(name: str) -> str:
    """Make a valid Python / tool identifier from an operationId."""
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "op"


def _path_to_id(method: str, path: str) -> str:
    """Generate an operationId from HTTP method + path when none is provided."""
    # Replace {param} with by_param
    clean = re.sub(r"\{([^}]+)\}", lambda m: "by_" + m.group(1), path)
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", clean)
    clean = re.sub(r"_+", "_", clean).strip("_")
    return f"{method.lower()}_{clean}" if clean else method.lower()


def _resolve_ref(ref: str, root: dict, depth: int = 0) -> dict:
    """Resolve a local JSON Pointer ($ref starting with #/) within the document."""
    if depth > 8 or not isinstance(ref, str) or not ref.startswith("#/"):
        return {}
    parts = ref[2:].split("/")
    node = root
    for part in parts:
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict):
            return {}
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def _resolve_schema(schema: Any, root: dict, depth: int = 0) -> dict:
    """
    Recursively resolve $refs and flatten allOf in a schema dict.
    Returns a plain JSON Schema dict safe for tool parameter definitions.
    """
    if depth > 8:
        return {"type": "string"}
    if not isinstance(schema, dict):
        return {"type": "string"}

    # Dereference $ref first
    if "$ref" in schema:
        resolved = _resolve_ref(schema["$ref"], root, depth + 1)
        return _resolve_schema(resolved, root, depth + 1)

    result = {k: v for k, v in schema.items()}

    # Flatten allOf by merging properties
    if "allOf" in result:
        merged_props: dict = {}
        merged_required: list = []
        for sub in result.pop("allOf", []):
            sub_res = _resolve_schema(sub, root, depth + 1)
            if "properties" in sub_res:
                merged_props.update(sub_res["properties"])
            if "required" in sub_res:
                merged_required.extend(sub_res.get("required", []))
            for k, v in sub_res.items():
                if k not in ("properties", "required"):
                    result.setdefault(k, v)
        if merged_props:
            result.setdefault("properties", {}).update(merged_props)
        if merged_required:
            result.setdefault("required", [])
            result["required"] = list(set(result["required"] + merged_required))

    # Recursively resolve properties
    if "properties" in result and isinstance(result["properties"], dict):
        result["properties"] = {
            k: _resolve_schema(v, root, depth + 1)
            for k, v in result["properties"].items()
        }

    # Recursively resolve items (array)
    if "items" in result:
        result["items"] = _resolve_schema(result["items"], root, depth + 1)

    return result


def _clean_schema(schema: dict) -> dict:
    """Strip OpenAPI-only fields that aren't valid JSON Schema."""
    drop = {"example", "examples", "xml", "externalDocs", "deprecated", "readOnly", "writeOnly", "x-"}
    return {k: v for k, v in schema.items() if k not in drop and not k.startswith("x-")}


def _extract_json_body_schema(content_map: dict, root: dict) -> Optional[dict]:
    """
    Extract and resolve a JSON Schema from an OpenAPI content map.
    Prefers application/json, falls back to first available content type.
    """
    for ct, media in content_map.items():
        if "json" in ct:
            return _resolve_schema(media.get("schema", {}), root)
    for ct, media in content_map.items():
        schema = media.get("schema", {})
        if schema:
            return _resolve_schema(schema, root)
    return None


def _param_schema_from_v2(p: dict) -> dict:
    """Build a JSON Schema dict from a v2-style inline parameter."""
    schema: dict = {}
    t = p.get("type", "string")
    schema["type"] = t
    if "format" in p:
        schema["format"] = p["format"]
    if "enum" in p:
        schema["enum"] = p["enum"]
    if "minimum" in p:
        schema["minimum"] = p["minimum"]
    if "maximum" in p:
        schema["maximum"] = p["maximum"]
    if "minLength" in p:
        schema["minLength"] = p["minLength"]
    if "maxLength" in p:
        schema["maxLength"] = p["maxLength"]
    if t == "array" and "items" in p:
        schema["items"] = p["items"]
    if t == "object" and "properties" in p:
        schema["properties"] = p["properties"]
    return schema or {"type": "string"}


# ── v2 parser ──────────────────────────────────────────────────────────────────

def _parse_v2_security_definitions(data: dict) -> dict[str, SecurityScheme]:
    schemes: dict[str, SecurityScheme] = {}
    for name, sd in data.get("securityDefinitions", {}).items():
        sd_type = sd.get("type", "").lower()
        if sd_type == "apikey":
            schemes[name] = SecurityScheme(
                name=name, type="apiKey",
                in_=sd.get("in", "header"),
                param_name=sd.get("name", name),
                description=sd.get("description", ""),
            )
        elif sd_type == "basic":
            schemes[name] = SecurityScheme(
                name=name, type="basic",
                description=sd.get("description", ""),
            )
        elif sd_type == "oauth2":
            schemes[name] = SecurityScheme(
                name=name, type="oauth2",
                description=sd.get("description", ""),
            )
    return schemes


def _parse_v2(data: dict, base_url_override: str) -> OpenAPISpec:
    info = data.get("info", {})
    spec = OpenAPISpec(
        title=info.get("title", "API"),
        spec_version=info.get("version", "1.0"),
        openapi_version="2.0",
    )

    # Base URL from host + basePath
    if base_url_override:
        spec.servers = [base_url_override.rstrip("/")]
    else:
        host = data.get("host", "")
        base_path = data.get("basePath", "").rstrip("/")
        schemes = data.get("schemes", ["https"])
        scheme = schemes[0] if schemes else "https"
        if host:
            spec.servers = [f"{scheme}://{host}{base_path}"]
        else:
            spec.servers = [base_path or ""]

    spec.security_schemes = _parse_v2_security_definitions(data)
    spec.global_security = data.get("security", [])

    op_ids_seen: set[str] = set()

    for path, path_item in data.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        shared_params = path_item.get("parameters", [])

        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            op_data = path_item.get(method)
            if not op_data or not isinstance(op_data, dict):
                continue

            raw_id = op_data.get("operationId", "")
            base_id = _sanitize_op_id(raw_id) if raw_id else _path_to_id(method, path)

            op_id = base_id
            counter = 2
            while op_id in op_ids_seen:
                op_id = f"{base_id}_{counter}"
                counter += 1
            op_ids_seen.add(op_id)

            op = OpenAPIOperation(
                operation_id=op_id,
                raw_id=raw_id,
                method=method.upper(),
                path=path,
                summary=op_data.get("summary", ""),
                description=op_data.get("description", ""),
                tags=op_data.get("tags", []),
                security_requirements=op_data.get("security", []),
            )

            # Merge shared + operation params; operation params win on name collision
            merged_params: dict[str, dict] = {}
            for p in shared_params:
                if not isinstance(p, dict):
                    continue
                if "$ref" in p:
                    p = _resolve_ref(p["$ref"], data)
                if p.get("name"):
                    merged_params[p["name"]] = p

            body_param: Optional[dict] = None
            for p in op_data.get("parameters", []):
                if not isinstance(p, dict):
                    continue
                if "$ref" in p:
                    p = _resolve_ref(p["$ref"], data)
                p_in = p.get("in", "")
                if p_in == "body":
                    body_param = p
                    continue
                if p_in == "formData":
                    continue
                if p.get("name"):
                    merged_params[p["name"]] = p

            for p_name, p in merged_params.items():
                p_in = p.get("in", "query")
                if p_in not in ("path", "query", "header", "cookie"):
                    continue
                raw_schema = p.get("schema")
                if raw_schema:
                    p_schema = _clean_schema(_resolve_schema(raw_schema, data))
                else:
                    p_schema = _param_schema_from_v2(p)

                op.parameters.append(OpenAPIParameter(
                    name=p_name,
                    location=p_in,
                    required=p.get("required", p_in == "path"),
                    json_schema=p_schema,
                    description=p.get("description", ""),
                ))

            # Body parameter
            if body_param:
                body_schema = _resolve_schema(body_param.get("schema", {}), data)
                if body_schema.get("type") == "object" and "properties" in body_schema:
                    op.request_body_schema = {
                        k: _clean_schema(v)
                        for k, v in body_schema["properties"].items()
                    }
                    op.request_body_required = body_param.get("required", False)
                else:
                    op.body_param_name = "body"
                    op.request_body_required = body_param.get("required", False)

            spec.operations[op_id] = op

    return spec


# ── v3 parser ──────────────────────────────────────────────────────────────────

def _parse_v3_security_schemes(components: dict) -> dict[str, SecurityScheme]:
    schemes: dict[str, SecurityScheme] = {}
    for name, ss in components.get("securitySchemes", {}).items():
        ss_type = ss.get("type", "").lower()
        if ss_type == "apikey":
            schemes[name] = SecurityScheme(
                name=name, type="apiKey",
                in_=ss.get("in", "header"),
                param_name=ss.get("name", name),
                description=ss.get("description", ""),
            )
        elif ss_type == "http":
            schemes[name] = SecurityScheme(
                name=name, type="http",
                http_scheme=ss.get("scheme", "bearer").lower(),
                description=ss.get("description", ""),
            )
        elif ss_type == "oauth2":
            schemes[name] = SecurityScheme(
                name=name, type="oauth2",
                description=ss.get("description", ""),
            )
        elif ss_type == "openidconnect":
            schemes[name] = SecurityScheme(
                name=name, type="openIdConnect",
                description=ss.get("description", ""),
            )
    return schemes


def _parse_v3(data: dict, base_url_override: str) -> OpenAPISpec:
    info = data.get("info", {})
    spec = OpenAPISpec(
        title=info.get("title", "API"),
        spec_version=info.get("version", "1.0"),
        openapi_version=data.get("openapi", "3.0.0"),
    )

    if base_url_override:
        spec.servers = [base_url_override.rstrip("/")]
    else:
        for s in data.get("servers", []):
            url = s.get("url", "")
            if url:
                # Resolve server variables with defaults
                for var_name, var_def in s.get("variables", {}).items():
                    url = url.replace(f"{{{var_name}}}", str(var_def.get("default", "")))
                # Keep only absolute URLs as primary base
                if not url.startswith("/"):
                    spec.servers.append(url.rstrip("/"))
        if not spec.servers:
            spec.servers = [""]

    components = data.get("components", {})
    spec.security_schemes = _parse_v3_security_schemes(components)
    spec.global_security = data.get("security", [])

    op_ids_seen: set[str] = set()

    for path, path_item in data.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        shared_params = path_item.get("parameters", [])

        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            op_data = path_item.get(method)
            if not op_data or not isinstance(op_data, dict):
                continue

            raw_id = op_data.get("operationId", "")
            base_id = _sanitize_op_id(raw_id) if raw_id else _path_to_id(method, path)

            op_id = base_id
            counter = 2
            while op_id in op_ids_seen:
                op_id = f"{base_id}_{counter}"
                counter += 1
            op_ids_seen.add(op_id)

            op = OpenAPIOperation(
                operation_id=op_id,
                raw_id=raw_id,
                method=method.upper(),
                path=path,
                summary=op_data.get("summary", ""),
                description=op_data.get("description", ""),
                tags=op_data.get("tags", []),
                security_requirements=op_data.get("security", []),
            )

            # Merge shared + operation params (operation wins)
            param_map: dict[str, dict] = {}
            for p in shared_params:
                if not isinstance(p, dict):
                    continue
                if "$ref" in p:
                    p = _resolve_ref(p["$ref"], data)
                if p.get("name"):
                    param_map[p["name"]] = p

            for p in op_data.get("parameters", []):
                if not isinstance(p, dict):
                    continue
                if "$ref" in p:
                    p = _resolve_ref(p["$ref"], data)
                if p.get("name"):
                    param_map[p["name"]] = p

            for p in param_map.values():
                p_in = p.get("in", "query")
                if p_in not in ("path", "query", "header", "cookie"):
                    continue
                raw_schema = p.get("schema", {"type": "string"})
                p_schema = _clean_schema(_resolve_schema(raw_schema, data))

                op.parameters.append(OpenAPIParameter(
                    name=p.get("name", ""),
                    location=p_in,
                    required=p.get("required", p_in == "path"),
                    json_schema=p_schema,
                    description=p.get("description", ""),
                ))

            # Request body
            req_body = op_data.get("requestBody", {})
            if req_body:
                if "$ref" in req_body:
                    req_body = _resolve_ref(req_body["$ref"], data)
                op.request_body_required = req_body.get("required", False)
                body_schema = _extract_json_body_schema(req_body.get("content", {}), data)
                if body_schema:
                    if body_schema.get("type") == "object" and "properties" in body_schema:
                        op.request_body_schema = {
                            k: _clean_schema(v)
                            for k, v in body_schema["properties"].items()
                        }
                    else:
                        # Array, primitive, or non-object: use opaque body param
                        op.body_param_name = "body"
                else:
                    op.body_param_name = "body"

            spec.operations[op_id] = op

    return spec


# ── Public API ─────────────────────────────────────────────────────────────────

def parse_openapi_spec(content: str, base_url_override: str = "") -> OpenAPISpec:
    """
    Parse an OpenAPI 2.x or 3.x spec from a JSON or YAML string.

    Args:
        content: Raw JSON or YAML spec content
        base_url_override: Override the base URL extracted from the spec

    Returns:
        OpenAPISpec with all operations and security schemes parsed
    """
    data = _load_content(content)
    if not isinstance(data, dict):
        raise ValueError("OpenAPI spec must be a JSON/YAML object (got a non-dict)")

    swagger = str(data.get("swagger", ""))
    openapi = str(data.get("openapi", ""))

    if swagger.startswith("2"):
        return _parse_v2(data, base_url_override)
    elif openapi:
        return _parse_v3(data, base_url_override)
    else:
        raise ValueError(
            "Not a valid OpenAPI spec: missing 'swagger' (v2) or 'openapi' (v3) field. "
            f"Top-level keys found: {list(data.keys())[:10]}"
        )

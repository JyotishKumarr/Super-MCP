"""
OData v2/v4 $metadata XML parser.
Extracts EntityTypes, EntitySets, Actions, Functions, and their properties.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

# OData v4 namespaces
EDMX_NS_V4 = "http://docs.oasis-open.org/odata/ns/edmx"
EDM_NS_V4 = "http://docs.oasis-open.org/odata/ns/edm"

# OData v2 namespaces (Microsoft ADO)
EDMX_NS_V2_LIST = [
    "http://schemas.microsoft.com/ado/2007/06/edmx",
    "http://schemas.microsoft.com/ado/2009/11/edmx",
]
EDM_NS_V2_LIST = [
    "http://schemas.microsoft.com/ado/2007/05/edm",
    "http://schemas.microsoft.com/ado/2008/09/edm",
    "http://schemas.microsoft.com/ado/2009/11/edm",
]

# EDM to JSON Schema type mapping
EDM_TO_JSON_TYPE: dict[str, str] = {
    "Edm.String": "string",
    "Edm.Int16": "integer",
    "Edm.Int32": "integer",
    "Edm.Int64": "integer",
    "Edm.Byte": "integer",
    "Edm.SByte": "integer",
    "Edm.Single": "number",
    "Edm.Double": "number",
    "Edm.Decimal": "number",
    "Edm.Boolean": "boolean",
    "Edm.DateTime": "string",
    "Edm.DateTimeOffset": "string",
    "Edm.Date": "string",
    "Edm.Time": "string",
    "Edm.TimeOfDay": "string",
    "Edm.Duration": "string",
    "Edm.Guid": "string",
    "Edm.Binary": "string",
    "Edm.Stream": "string",
}


@dataclass
class Property:
    name: str
    edm_type: str
    nullable: bool = True
    is_key: bool = False
    max_length: Optional[int] = None

    @property
    def json_type(self) -> str:
        return EDM_TO_JSON_TYPE.get(self.edm_type, "string")


@dataclass
class NavigationProperty:
    name: str
    target_type: str
    is_collection: bool = False


@dataclass
class EntityType:
    name: str
    key_properties: list[str] = field(default_factory=list)
    properties: list[Property] = field(default_factory=list)
    navigation_properties: list[NavigationProperty] = field(default_factory=list)

    def get_property(self, name: str) -> Optional[Property]:
        return next((p for p in self.properties if p.name == name), None)

    def key_property_schema(self) -> dict:
        schema = {}
        for key in self.key_properties:
            prop = self.get_property(key)
            schema[key] = {
                "type": prop.json_type if prop else "string",
                "description": f"Key field: {key}",
            }
        return schema

    def all_property_schema(self, exclude_keys: bool = False) -> dict:
        schema = {}
        for prop in self.properties:
            if exclude_keys and prop.is_key:
                continue
            desc = f"{prop.edm_type}"
            if prop.is_key:
                desc = f"[Key] {desc}"
            if not prop.nullable:
                desc = f"[Required] {desc}"
            schema[prop.name] = {"type": prop.json_type, "description": desc}
        return schema


@dataclass
class EntitySet:
    name: str
    entity_type_name: str  # Short name (without namespace)


@dataclass
class ActionOrFunction:
    name: str
    kind: str  # "action" | "function"
    is_bound: bool = False
    binding_type: Optional[str] = None
    parameters: list[dict] = field(default_factory=list)
    return_type: Optional[str] = None
    http_method: str = "POST"  # v2 FunctionImport


@dataclass
class ODataSpec:
    service_url: str
    version: str  # "v2" | "v4"
    entity_types: dict[str, EntityType] = field(default_factory=dict)
    entity_sets: dict[str, EntitySet] = field(default_factory=dict)
    actions: list[ActionOrFunction] = field(default_factory=list)
    functions: list[ActionOrFunction] = field(default_factory=list)
    namespace: str = ""

    def resolve_entity_type(self, entity_set_name: str) -> Optional[EntityType]:
        es = self.entity_sets.get(entity_set_name)
        if not es:
            return None
        # Try direct name lookup first, then strip namespace
        et = self.entity_types.get(es.entity_type_name)
        if not et:
            short = es.entity_type_name.split(".")[-1]
            et = self.entity_types.get(short)
        return et


def _detect_namespaces(root: ET.Element) -> tuple[str, str]:
    """Detect EDMX and EDM namespaces from the root element tag."""
    tag = root.tag
    if EDMX_NS_V4 in tag:
        return EDMX_NS_V4, EDM_NS_V4
    for ns in EDMX_NS_V2_LIST:
        if ns in tag:
            # Find matching EDM namespace from child elements
            xml_str = ET.tostring(root, encoding="unicode")
            for edm_ns in EDM_NS_V2_LIST:
                if edm_ns in xml_str:
                    return ns, edm_ns
            return ns, EDM_NS_V2_LIST[0]
    # Fallback: try v4
    return EDMX_NS_V4, EDM_NS_V4


def _sanitize_name(name: str) -> str:
    """Sanitize a name to be safe for use as a tool name component."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def parse_odata_metadata(xml_content: str, service_url: str = "") -> ODataSpec:
    """Parse OData $metadata XML and return a structured ODataSpec."""
    root = ET.fromstring(xml_content)
    edmx_ns, edm_ns = _detect_namespaces(root)
    version = "v4" if edmx_ns == EDMX_NS_V4 else "v2"

    spec = ODataSpec(service_url=service_url, version=version)

    data_services = root.find(f"{{{edmx_ns}}}DataServices")
    if data_services is None:
        # Some v4 docs have Schema directly under root
        schemas = root.findall(f"{{{edm_ns}}}Schema")
    else:
        schemas = data_services.findall(f"{{{edm_ns}}}Schema")

    for schema in schemas:
        ns = schema.get("Namespace", "")
        if not spec.namespace:
            spec.namespace = ns

        # Parse EntityTypes
        for et_elem in schema.findall(f"{{{edm_ns}}}EntityType"):
            et = _parse_entity_type(et_elem, edm_ns)
            spec.entity_types[et.name] = et

        # Parse ComplexTypes (stored as entity types without keys — used for nested objects)
        for ct_elem in schema.findall(f"{{{edm_ns}}}ComplexType"):
            ct = _parse_entity_type(ct_elem, edm_ns)
            spec.entity_types[ct.name] = ct

        # Parse EntityContainer
        container = schema.find(f"{{{edm_ns}}}EntityContainer")
        if container:
            for es_elem in container.findall(f"{{{edm_ns}}}EntitySet"):
                es_name = es_elem.get("Name", "")
                et_name = es_elem.get("EntityType", "").split(".")[-1]
                spec.entity_sets[es_name] = EntitySet(name=es_name, entity_type_name=et_name)

            # v2 FunctionImports
            for fi_elem in container.findall(f"{{{edm_ns}}}FunctionImport"):
                fi = ActionOrFunction(
                    name=fi_elem.get("Name", ""),
                    kind="function",
                    http_method=fi_elem.get("{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}HttpMethod", "GET"),
                    return_type=fi_elem.get("ReturnType"),
                )
                for p_elem in fi_elem.findall(f"{{{edm_ns}}}Parameter"):
                    fi.parameters.append({
                        "name": p_elem.get("Name"),
                        "type": p_elem.get("Type", "Edm.String"),
                        "mode": p_elem.get("Mode", "In"),
                    })
                spec.functions.append(fi)

        # v4 Actions
        for action_elem in schema.findall(f"{{{edm_ns}}}Action"):
            action = _parse_action_or_function(action_elem, edm_ns, "action")
            spec.actions.append(action)

        # v4 Functions
        for func_elem in schema.findall(f"{{{edm_ns}}}Function"):
            func = _parse_action_or_function(func_elem, edm_ns, "function")
            spec.functions.append(func)

    return spec


def _parse_entity_type(elem: ET.Element, edm_ns: str) -> EntityType:
    name = elem.get("Name", "")
    et = EntityType(name=name)

    key_elem = elem.find(f"{{{edm_ns}}}Key")
    if key_elem:
        for ref in key_elem.findall(f"{{{edm_ns}}}PropertyRef"):
            et.key_properties.append(ref.get("Name", ""))

    for prop_elem in elem.findall(f"{{{edm_ns}}}Property"):
        p_name = prop_elem.get("Name", "")
        p_type = prop_elem.get("Type", "Edm.String")
        nullable_str = prop_elem.get("Nullable", "true")
        nullable = nullable_str.lower() not in ("false", "0")
        max_length_str = prop_elem.get("MaxLength")
        max_length = int(max_length_str) if max_length_str and max_length_str.isdigit() else None
        et.properties.append(Property(
            name=p_name,
            edm_type=p_type,
            nullable=nullable,
            is_key=p_name in et.key_properties,
            max_length=max_length,
        ))

    for nav_elem in elem.findall(f"{{{edm_ns}}}NavigationProperty"):
        nav_type = nav_elem.get("Type", nav_elem.get("ToRole", ""))
        is_coll = nav_type.startswith("Collection(")
        clean_type = nav_type.removeprefix("Collection(").removesuffix(")").split(".")[-1]
        et.navigation_properties.append(NavigationProperty(
            name=nav_elem.get("Name", ""),
            target_type=clean_type,
            is_collection=is_coll,
        ))

    return et


def _parse_action_or_function(elem: ET.Element, edm_ns: str, kind: str) -> ActionOrFunction:
    name = elem.get("Name", "")
    is_bound = elem.get("IsBound", "false").lower() == "true"
    aof = ActionOrFunction(name=name, kind=kind, is_bound=is_bound)

    return_elem = elem.find(f"{{{edm_ns}}}ReturnType")
    if return_elem is not None:
        aof.return_type = return_elem.get("Type")

    for p_elem in elem.findall(f"{{{edm_ns}}}Parameter"):
        aof.parameters.append({
            "name": p_elem.get("Name"),
            "type": p_elem.get("Type", "Edm.String"),
            "nullable": p_elem.get("Nullable", "true").lower() != "false",
        })
        if is_bound and not aof.binding_type and aof.parameters:
            aof.binding_type = aof.parameters[0].get("type", "")

    return aof


def build_key_predicate(entity_type: Optional[EntityType], key_args: dict) -> str:
    """Build OData key predicate string, e.g., (1) or (Key1='A',Key2=1)."""
    if not entity_type or not entity_type.key_properties:
        val = next(iter(key_args.values()), "")
        return f"({_format_key_value(val, 'Edm.String')})"

    if len(entity_type.key_properties) == 1:
        key_name = entity_type.key_properties[0]
        val = key_args.get(key_name, "")
        prop = entity_type.get_property(key_name)
        edm_type = prop.edm_type if prop else "Edm.String"
        return f"({_format_key_value(val, edm_type)})"

    parts = []
    for key_name in entity_type.key_properties:
        val = key_args.get(key_name, "")
        prop = entity_type.get_property(key_name)
        edm_type = prop.edm_type if prop else "Edm.String"
        parts.append(f"{key_name}={_format_key_value(val, edm_type)}")
    return f"({','.join(parts)})"


def _format_key_value(value, edm_type: str) -> str:
    """Format a key value for inclusion in a URL predicate."""
    str_val = str(value)
    if edm_type in ("Edm.String", "Edm.Guid"):
        escaped = str_val.replace("'", "''")
        return f"'{escaped}'"
    if edm_type in ("Edm.DateTime",):
        return f"datetime'{str_val}'"
    return str_val

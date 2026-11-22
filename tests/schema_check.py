"""A small JSON Schema checker for the subset of OpenAPI 3.1 this app generates.

`jsonschema` would need a compiled dependency, and this machine blocks some of those (Smart App Control).
"""

import re
from datetime import date, datetime
from typing import Any

TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def problems(value: Any, schema: dict[str, Any], spec: dict[str, Any], at: str = "$") -> list[str]:
    """What is wrong with `value`. Unknown properties count as wrong: they would be undocumented."""
    if "$ref" in schema:
        return problems(value, spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]], spec, at)
    if "anyOf" in schema:
        options = [problems(value, option, spec, at) for option in schema["anyOf"]]
        return [] if any(not found for found in options) else [f"{at}: fits none of anyOf ({options})"]

    if kind := schema.get("type"):
        wrong_bool = isinstance(value, bool) and kind != "boolean"
        if wrong_bool or not isinstance(value, TYPES[kind]):
            return [f"{at}: expected {kind}, got {value!r}"]

    found = []
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{at}: {value!r} is not in the enum")
    if "const" in schema and value != schema["const"]:
        found.append(f"{at}: {value!r} is not {schema['const']!r}")

    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            found.append(f"{at}: below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            found.append(f"{at}: above the maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            found.append(f"{at}: not above the minimum")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            found.append(f"{at}: too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            found.append(f"{at}: too long")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            found.append(f"{at}: doesn't match {schema['pattern']}")
        try:
            if schema.get("format") == "date":
                date.fromisoformat(value)
            elif schema.get("format") == "date-time" and datetime.fromisoformat(value).tzinfo is None:
                found.append(f"{at}: date-time without a time zone")
        except ValueError:
            found.append(f"{at}: not a {schema['format']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            found.append(f"{at}: too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            found.append(f"{at}: too many items")
        if "items" in schema:
            for index, item in enumerate(value):
                found += problems(item, schema["items"], spec, f"{at}[{index}]")

    if isinstance(value, dict):
        found += [f"{at}: {name} is missing" for name in schema.get("required", []) if name not in value]
        if "properties" in schema:
            for name, item in value.items():
                if name in schema["properties"]:
                    found += problems(item, schema["properties"][name], spec, f"{at}.{name}")
                elif not schema.get("additionalProperties"):
                    found.append(f"{at}: {name} is undocumented")
    return found

"""Framework-free tool bindings and provider-facing schema guidance."""

import json
from dataclasses import dataclass

from pydantic import BaseModel

from statistics_agent.agent.errors import AdapterError


@dataclass(frozen=True)
class ToolBinding:
    """Server-owned schema; real tools and policies arrive in plans 5 and 6."""

    name: str
    description: str
    arguments: type[BaseModel]

    def schema(self) -> dict:
        if self.arguments.model_config.get("extra") != "forbid":
            raise AdapterError("Tool argument schemas must forbid extra fields")
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": model_tool_schema(self.arguments.model_json_schema()),
            },
        }


def model_tool_schema(schema: dict) -> dict:
    """Lean provider guidance, not a replacement for strict local validation.

    Keep types, enums, required fields, UUID formats and extra-field rejection.
    String/array size bounds remain enforced by the original Pydantic model.
    Preserve actual field/definition names, even if named title or maxLength.
    """
    omitted = {"title", "default", "minLength", "maxLength", "minItems", "maxItems"}
    mappings = {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}
    alternatives = {"anyOf", "oneOf", "allOf", "prefixItems"}
    children = {"items", "additionalProperties", "contains", "not", "if", "then", "else"}
    result = {}
    for key, value in schema.items():
        if key in omitted:
            continue
        if key in mappings:
            value = {name: model_tool_schema(child) for name, child in value.items()}
        elif key in alternatives:
            value = [model_tool_schema(child) for child in value]
        elif key in children and isinstance(value, dict):
            value = model_tool_schema(value)
        result[key] = value
    # Inline leaf definitions to avoid repeated $defs/$ref wrappers on the wire.
    definitions = result.get("$defs", {})
    leaf_names = {name for name, child in definitions.items() if "$ref" not in json.dumps(child)}

    def inline(node):
        reference = node.get("$ref", "")
        if (
            isinstance(reference, str)
            and reference.startswith("#/$defs/")
            and reference[8:] in leaf_names
        ):
            node = definitions[reference[8:]] | {k: v for k, v in node.items() if k != "$ref"}
        compact = {}
        for key, value in node.items():
            if key in mappings:
                value = {name: inline(child) for name, child in value.items()}
            elif key in alternatives:
                value = [inline(child) for child in value]
            elif key in children and isinstance(value, dict):
                value = inline(value)
            compact[key] = value
        return compact

    if definitions and len(leaf_names) == len(definitions):
        return inline({key: value for key, value in result.items() if key != "$defs"})
    return result

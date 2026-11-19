"""Lean wire guidance must never weaken the authoritative runtime contracts."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from statistics_agent.agent.statistics import observation_summary
from statistics_agent.agent.tool_schema import model_tool_schema
from statistics_agent.contracts.datasets import DescribeColumnsArguments
from statistics_agent.tools.statistics import STATISTICS_BINDINGS


def test_nested_clarification_definitions_are_inlined_without_losing_fields():
    from statistics_agent.contracts.clarification import RequestClarification

    schema = model_tool_schema(RequestClarification.model_json_schema())
    assert "$defs" not in schema
    assert schema["required"] == ["question"]
    option = schema["properties"]["options"]["items"]
    assert option["required"] == ["key", "text"]
    assert option["additionalProperties"] is False
    assert option["properties"]["key"]["pattern"] == "^[A-Za-z0-9_.-]+$"


def test_recursive_guidance_keeps_resolvable_definitions():
    schema = {
        "type": "object",
        "properties": {"node": {"$ref": "#/$defs/Node"}},
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {
                    "child": {"$ref": "#/$defs/Node"},
                },
            }
        },
    }
    assert model_tool_schema(schema) == schema


def test_schema_compaction_preserves_types_enums_required_and_real_field_names():
    schema = {
        "title": "Example",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "maxLength": 5},
            "maxLength": {"$ref": "#/$defs/Choice"},
            "$defs": {"type": "string"},
        },
        "required": ["title", "maxLength"],
        "$defs": {"Choice": {"type": "string", "enum": ["a", "b"], "default": "a"}},
    }
    before = json.dumps(schema)
    compact = model_tool_schema(schema)
    assert json.dumps(schema) == before
    assert compact == {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "maxLength": {"type": "string", "enum": ["a", "b"]},
            "$defs": {"type": "string"},
        },
        "required": ["title", "maxLength"],
    }


@pytest.mark.parametrize("columns", [["x"] * 129, ["x" * 129], [], ["x", "x"]])
def test_provider_size_hints_omitted_but_runtime_column_bounds_still_enforced(columns):
    binding = next(b for b in STATISTICS_BINDINGS if b.name == "describe_columns")
    assert "maxItems" not in json.dumps(binding.schema())
    with pytest.raises(ValidationError):
        DescribeColumnsArguments.model_validate_json(
            json.dumps(
                {
                    "dataset_id": str(uuid4()),
                    "columns": columns,
                    "missing_policy": "omit",
                }
            ),
            strict=True,
        )


def test_json_observations_avoid_double_escaping_and_legacy_text_stays_text():
    assert observation_summary('{"result":{"mean":2.0}}') == {"result": {"mean": 2.0}}
    assert observation_summary("mean=2") == "mean=2"

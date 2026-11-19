"""Versioned static policy rules; no permissive execution policy or infrastructure."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from statistics_agent.agent.errors import AdapterError
from statistics_agent.agent.policy import PolicyRules
from statistics_agent.agent.prompts import CONTROL_FUNCTIONS
from statistics_agent.agent.statistics import StatisticsSession
from statistics_agent.agent.tool_schema import ToolBinding
from statistics_agent.contracts.agent import ToolDecision, ToolProposal
from statistics_agent.contracts.common import ConfigurationReference
from statistics_agent.contracts.datasets import DatasetLimits, InspectDatasetArguments
from statistics_agent.contracts.policy import (
    PolicyEvaluation,
    ToolExecutionScope,
    ToolPolicyConfiguration,
)
from support.datasets import context, owner


def configuration(**changes):
    return ToolPolicyConfiguration(
        **{
            "reference": ConfigurationReference(config_id=uuid4(), version="policy-v1"),
            **changes,
        }
    )


def proposal(name="inspect_dataset", **arguments):
    return ToolProposal(
        tool_call_id=uuid4(),
        name=name,
        raw_arguments=json.dumps({"dataset_id": str(uuid4()), **arguments}),
    )


@pytest.mark.parametrize(
    "name", ["inspect_dataset", "describe_columns", "aggregate_statistics", "correlate_columns"]
)
def test_tools_are_disabled_by_default(name):
    result = PolicyRules(configuration()).check(proposal(name))
    assert result.status == "denied" and result.decision.reason == "denied"


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("inspect_dataset", {}),
        ("describe_columns", {"columns": ["x"], "missing_policy": "omit"}),
        ("aggregate_statistics", {"column": "x", "operation": "sum", "missing_policy": "omit"}),
        (
            "correlate_columns",
            {
                "x_column": "x",
                "y_column": "y",
                "method": "pearson",
                "missing_policy": "pairwise_complete",
            },
        ),
    ],
)
def test_enabled_typed_operations_keep_call_and_policy_identity(name, arguments):
    snapshot = configuration(allowed_tools=(name,))
    call = proposal(name, **arguments)
    result = PolicyRules(snapshot).check(call)
    assert result.status == "allowed" and result.decision.allowed
    assert result.policy == snapshot.reference
    assert result.tool_call_id == result.decision.tool_call_id == call.tool_call_id


@pytest.mark.parametrize("name", ["unknown", *sorted(CONTROL_FUNCTIONS)])
def test_unknown_and_control_names_cannot_be_numerical_rules(name):
    with pytest.raises(ValidationError):
        configuration(allowed_tools=(name,))
    result = PolicyRules(configuration()).check(proposal(name))
    assert result.status == "denied" and result.decision.reason == "unknown_tool"


@pytest.mark.parametrize("name", sorted(CONTROL_FUNCTIONS))
def test_runner_rejects_control_name_shadowing_before_infrastructure_use(name):
    with pytest.raises(AdapterError, match="Invalid tool registry"):
        StatisticsSession(
            context=None,
            adapter=None,
            toolset=None,
            policy_version=None,
            bindings=(ToolBinding(name, "fixture", InspectDatasetArguments),),
            policy=None,
            executor=None,
            authorize=None,
            sink=None,
            sequence=None,
            deadline=None,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"allowed_tools": ("inspect_dataset", "inspect_dataset")},
        {"aggregate_operations": ("sum", "sum")},
        {"correlation_methods": ("pearson", "pearson")},
        {"aggregate_operations": ("eval",)},
        {"correlation_methods": ("custom",)},
        {"allowed_tools": "inspect_dataset"},
        {"allowed_tools": ["inspect_dataset"]},
        {"dataset_limits": {"max_rows": 10001}},
        {"statistics_limits": {"max_groups": 51}},
        {"statistics_limits": {"max_result_bytes": 16385}},
        {"statistics_limits": {"max_groups": True}},
        {"extra": "ignored-by-mistake"},
    ],
)
def test_invalid_configuration_cannot_be_admitted(changes):
    with pytest.raises(ValidationError):
        configuration(**changes)


def test_strict_json_configuration_is_supported_without_python_container_coercion():
    source = configuration(allowed_tools=("inspect_dataset",))
    assert (
        ToolPolicyConfiguration.model_validate_json(source.model_dump_json(), strict=True) == source
    )


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("aggregate_statistics", {"column": "x", "operation": "max", "missing_policy": "omit"}),
        (
            "correlate_columns",
            {
                "x_column": "x",
                "y_column": "y",
                "method": "spearman",
                "missing_policy": "pairwise_complete",
            },
        ),
    ],
)
def test_valid_but_disallowed_operation_or_method_is_denied(name, arguments):
    result = PolicyRules(
        configuration(
            allowed_tools=(name,), aggregate_operations=("sum",), correlation_methods=("pearson",)
        )
    ).check(proposal(name, **arguments))
    assert result.status == "denied" and result.decision.reason == "denied"


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "{}",
        '{"dataset_id":"invalid"}',
        json.dumps({"dataset_id": str(uuid4()), "extra": True}),
    ],
)
def test_invalid_arguments_fail_closed_without_echoing_input(raw):
    call = proposal().model_copy(update={"raw_arguments": raw})
    result = PolicyRules(configuration(allowed_tools=("inspect_dataset",))).check(call)
    assert result.decision.reason == "invalid_arguments" and not result.decision.allowed
    assert "raw_arguments" not in result.model_dump_json()


def test_selected_columns_respect_lowered_policy_limit():
    rules = PolicyRules(
        configuration(
            allowed_tools=("describe_columns",), dataset_limits=DatasetLimits(max_columns=1)
        )
    )
    result = rules.check(proposal("describe_columns", columns=["x", "y"], missing_policy="omit"))
    assert result.status == "denied" and result.decision.reason == "limit_exceeded"
    assert (
        rules.check(proposal("describe_columns", columns=["x"], missing_policy="omit")).status
        == "allowed"
    )


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("aggregate_statistics", {"column": "x", "operation": "eval", "missing_policy": "omit"}),
        (
            "correlate_columns",
            {
                "x_column": "x",
                "y_column": "y",
                "method": "custom",
                "missing_policy": "pairwise_complete",
            },
        ),
    ],
)
def test_unknown_operation_or_method_fails_argument_validation(name, arguments):
    result = PolicyRules(configuration(allowed_tools=(name,))).check(proposal(name, **arguments))
    assert result.status == "denied" and result.decision.reason == "invalid_arguments"


def test_tampered_nested_limits_cannot_bypass_snapshot_validation():
    original = configuration()
    tampered = original.model_copy(
        update={
            "statistics_limits": original.statistics_limits.model_copy(update={"max_groups": 51})
        }
    )
    with pytest.raises(ValidationError):
        PolicyRules(tampered)


def test_rules_pin_an_immutable_revalidated_configuration():
    snapshot = configuration(allowed_tools=("inspect_dataset",))
    rules = PolicyRules(snapshot)
    different = configuration(allowed_tools=())
    assert rules.configuration.reference == snapshot.reference != different.reference
    with pytest.raises(ValidationError):
        rules.configuration.allowed_tools = ()
    with pytest.raises(ValidationError):
        rules.configuration.dataset_limits.max_rows = 1
    with pytest.raises(AttributeError):
        rules.configuration = different
    assert rules.check(proposal()).status == "allowed"
    assert PolicyRules(different).check(proposal()).status == "denied"
    with pytest.raises(ValidationError):
        PolicyRules(snapshot.model_copy(update={"allowed_tools": ("unknown",)}))


@pytest.mark.parametrize("status", ["unavailable", "not_evaluated"])
def test_missing_policy_evidence_is_distinct_from_a_denial(status):
    call = proposal()
    evidence = PolicyEvaluation(tool_call_id=call.tool_call_id, status=status)
    assert evidence.decision is None and evidence.policy is None
    with pytest.raises(ValidationError):
        PolicyEvaluation(
            tool_call_id=call.tool_call_id,
            status=status,
            decision=ToolDecision(tool_call_id=call.tool_call_id, allowed=False, reason="denied"),
        )


@pytest.mark.parametrize(
    "fault", ["missing_version", "missing_decision", "wrong_call", "wrong_status"]
)
def test_evaluated_evidence_cannot_misrepresent_a_decision(fault):
    result = PolicyRules(configuration(allowed_tools=("inspect_dataset",))).check(proposal())
    changes = {
        "missing_version": {"policy": None},
        "missing_decision": {"decision": None},
        "wrong_call": {"tool_call_id": uuid4()},
        "wrong_status": {"status": "denied"},
    }[fault]
    with pytest.raises(ValidationError):
        PolicyEvaluation.model_validate(result.model_copy(update=changes))


@pytest.mark.parametrize("ids", [(uuid4(),) * 2, tuple(uuid4() for _ in range(17)), [uuid4()]])
def test_execution_selection_rejects_duplicates_overflow_and_mutable_containers(ids):
    with pytest.raises(ValidationError):
        ToolExecutionScope(context=context(owner()), dataset_ids=ids)


def test_owned_policy_revalidates_tampered_scope_before_using_dependencies():
    from statistics_agent.agent.authorization import OwnedToolPolicy

    scope = ToolExecutionScope(context=context(owner()))
    tampered = scope.model_copy(update={"dataset_ids": (uuid4(),) * 2})
    with pytest.raises(ValidationError):
        OwnedToolPolicy(scope=tampered, rules=None, datasets=None, authorize=None)

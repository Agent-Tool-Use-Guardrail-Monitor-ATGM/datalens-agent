"""Feature observations preserve attempts without exporting untrusted strings."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from statistics_agent.agent.telemetry import observe_proposal
from statistics_agent.contracts.run_events import ToolRequested
from statistics_agent.core.kafka_settings import KafkaSettings, load_kafka_settings
from statistics_agent.core.settings import ConfigurationError


@pytest.mark.parametrize(
    "arguments,shape",
    [
        ("{", "invalid_json"),
        ("[]", "invalid_schema"),
        ('{"private_extra":"private-cell"}', "not_checked"),
        (42, "invalid_json"),
    ],
)
def test_malformed_arguments_still_have_bounded_features(arguments, shape):
    data = observe_proposal(
        {"function": {"name": "describe_columns", "arguments": arguments}}, uuid4(), frozenset()
    )
    value = ToolRequested.model_validate(data)
    assert value.features.argument_shape == shape
    assert len(value.arguments_sha256) == 64
    assert "private-cell" not in json.dumps(data) and "private_extra" not in json.dumps(data)


@pytest.mark.parametrize("name", ["private-malicious-tool", None, {}, []])
def test_unknown_name_is_never_exported(name):
    data = observe_proposal({"function": {"name": name, "arguments": "{}"}}, uuid4(), frozenset())
    assert data["kind"] == "unknown" and "name" not in data
    assert "private-malicious" not in json.dumps(data)


@pytest.mark.parametrize("selection", ["selected", "unselected", "invalid", "absent"])
def test_features_record_intent_not_raw_dataset_or_column_names(selection):
    dataset = uuid4()
    arguments = {"columns": ["private-column"] * 200, "operation": "mean", "missing_policy": "omit"}
    if selection != "absent":
        arguments["dataset_id"] = "private-invalid-id" if selection == "invalid" else str(dataset)
    data = observe_proposal(
        {"function": {"name": "describe_columns", "arguments": json.dumps(arguments)}},
        uuid4(),
        frozenset({dataset}) if selection == "selected" else frozenset(),
    )
    assert data["features"]["dataset_selection"] == selection
    assert data["features"]["column_count"] == 200
    assert data["features"]["operation"] == "mean"
    assert str(dataset) not in json.dumps(data) and "private-column" not in json.dumps(data)


def test_control_features_do_not_contain_question_or_options():
    data = observe_proposal(
        {
            "function": {
                "name": "request_clarification",
                "arguments": json.dumps(
                    {
                        "question": "private question",
                        "options": [{"key": "a", "text": "private option"}],
                    }
                ),
            }
        },
        uuid4(),
        frozenset(),
    )
    assert data["kind"] == "control" and data["features"]["column_count"] == 0
    assert "private" not in json.dumps(data)


@pytest.mark.parametrize(
    "address",
    [
        "https://host:9092",
        "user:secret@host:9092",
        "host",
        "host:0",
        "host:65536",
        "host:9092/path",
        "host:9092,",
    ],
)
def test_kafka_rejects_urls_credentials_or_invalid_endpoints(address):
    with pytest.raises(ValidationError):
        KafkaSettings(KAFKA_BOOTSTRAP_SERVERS=address, _env_file=None)


def test_hosted_kafka_requires_tls_and_auth_and_secrets_are_excluded():
    with pytest.raises(ValidationError):
        KafkaSettings(APP_ENV="production", KAFKA_BOOTSTRAP_SERVERS="host:9092", _env_file=None)
    settings = KafkaSettings(
        APP_ENV="production",
        KAFKA_BOOTSTRAP_SERVERS="host:9092",
        KAFKA_SECURITY_PROTOCOL="SASL_SSL",
        KAFKA_SASL_USERNAME="private-user",
        KAFKA_SASL_PASSWORD="private-password",
        _env_file=None,
    )
    assert "private" not in repr(settings) and "private" not in settings.model_dump_json()
    options = settings.producer_options()
    assert options["ssl_context"].check_hostname
    assert options["enable_idempotence"] and options["acks"] == "all"


def test_invalid_kafka_configuration_has_no_private_error_context(monkeypatch):
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "private:password@host")
    with pytest.raises(ConfigurationError) as error:
        load_kafka_settings()
    assert "private" not in str(error.value) and error.value.__context__ is None

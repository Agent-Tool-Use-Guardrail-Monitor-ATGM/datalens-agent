"""Wire compatibility, evidence preservation and contract boundary checks."""

import json
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from statistics_agent.contracts import (
    ArtifactReference,
    AuthenticatedPrincipal,
    AuthSessionReference,
    ConfigurationReference,
    ErrorEnvelope,
    EventEnvelope,
    EventType,
    ValidationDetail,
    resolve_request_id,
)
from statistics_agent.contracts.events import MAX_EVENT_DATA_BYTES, MAX_EVENT_DATA_DEPTH

SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "events_v1.json").read_text())


@pytest.fixture
def event_data():
    return deepcopy(SAMPLES[0])


def test_wire_names_remain_exactly_the_seven_agreed_event_types():
    assert {item.value for item in EventType} == {
        "session.started",
        "tool_call.requested",
        "tool_call.completed",
        "tool_call.failed",
        "model_call.completed",
        "model_call.failed",
        "session.ended",
    }


@pytest.mark.parametrize("sample", SAMPLES, ids=[sample["event_type"] for sample in SAMPLES])
def test_versioned_samples_round_trip_without_new_identity_or_changed_evidence(sample):
    event = EventEnvelope.model_validate(sample)
    serialized = event.model_dump_json()
    assert json.loads(serialized) == sample
    assert EventEnvelope.model_validate_json(serialized) == event


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_id", "not-a-uuid"),
        ("run_id", "provider-run-id"),
        ("session_id", None),
        ("request_id", "provider-request-id"),
        ("sequence", 0),
        ("sequence", -1),
        ("sequence", True),
        ("sequence", "1"),
        ("schema_version", "2.0"),
        ("event_type", "tool.executed"),
        ("occurred_at", "2026-10-21T12:00:00"),
        ("occurred_at", datetime(2026, 10, 21, 12, 0)),
        ("occurred_at", 1_792_584_000),
        ("data", ["not-an-object"]),
    ],
)
def test_invalid_wire_identity_version_sequence_and_timestamps_fail(event_data, field, value):
    event_data[field] = value
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(event_data)


def test_received_event_id_is_required_instead_of_silently_regenerated(event_data):
    del event_data["event_id"]
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(event_data)


def test_offsets_are_normalized_to_utc_without_changing_the_instant(event_data):
    event_data["occurred_at"] = "2026-10-21T14:00:00+02:00"
    event = EventEnvelope.model_validate(event_data)
    assert event.occurred_at == datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    assert event.model_dump(mode="json")["occurred_at"] == "2026-10-21T12:00:00Z"


def test_unknown_proposal_and_malformed_arguments_are_recorded_verbatim():
    sample = next(item for item in SAMPLES if item["event_type"] == "tool_call.requested")
    event = EventEnvelope.model_validate(sample)
    decoded = json.loads(event.model_dump_json())
    assert decoded["data"]["proposed_name"] == "unknown_statistics_tool"
    assert decoded["data"]["arguments"] == '{"column":'
    assert decoded["data"]["provider_tool_call_id"] == "provider-call-17"
    assert decoded["data"]["call_id"] != decoded["data"]["provider_tool_call_id"]


def test_missing_provider_usage_stays_null_instead_of_becoming_zero():
    sample = next(item for item in SAMPLES if item["event_type"] == "model_call.completed")
    assert EventEnvelope.model_validate(sample).model_dump(mode="json")["data"]["usage"] is None


def test_payload_size_is_bounded_at_the_encoded_utf8_boundary(event_data):
    overhead = len(b'{"text":""}')
    event_data["data"] = {"text": "x" * (MAX_EVENT_DATA_BYTES - overhead)}
    EventEnvelope.model_validate(event_data)
    event_data["data"]["text"] += "x"
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(event_data)
    event_data["data"] = {"text": "é" * (MAX_EVENT_DATA_BYTES // 2)}
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(event_data)


def test_deeply_nested_payloads_fail_before_serialization(event_data):
    nested = {}
    for _ in range(MAX_EVENT_DATA_DEPTH + 1):
        nested = {"next": nested}
    event_data["data"] = nested
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(event_data)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), object()])
def test_payloads_require_finite_json_values(event_data, value):
    event_data["data"] = {"value": value}
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(event_data)


@pytest.mark.parametrize("mutation", ["oversize", "cycle", "non-json", "integer-key"])
def test_payload_mutation_cannot_bypass_serialization_checks(event_data, mutation):
    event = EventEnvelope.model_validate(event_data)
    if mutation == "oversize":
        event.data["raw"] = "x" * MAX_EVENT_DATA_BYTES
    elif mutation == "cycle":
        event.data["loop"] = event.data
    elif mutation == "non-json":
        event.data["raw"] = object()
    else:
        event.data[1] = "not-a-json-object-key"
    with pytest.raises(PydanticSerializationError):
        event.model_dump_json()


def test_event_identity_cannot_be_reassigned(event_data):
    event = EventEnvelope.model_validate(event_data)
    with pytest.raises(ValidationError):
        event.event_id = uuid4()


@pytest.mark.parametrize(
    "header", [None, "", "invalid", "x" * 4096, "{00000000-0000-4000-8000-000000000001}"]
)
def test_invalid_request_headers_get_fresh_server_identifiers(header):
    first = resolve_request_id(header)
    second = resolve_request_id(header)
    assert isinstance(first, UUID) and first.version == 4
    assert first != second


def test_valid_uppercase_request_header_preserves_the_same_uuid():
    original = uuid4()
    assert resolve_request_id(str(original).upper()) == original


def test_principal_preserves_the_exact_verified_identity_and_is_immutable():
    principal = AuthenticatedPrincipal(
        issuer="https://identity.example.test/realms/team/", subject="user-123"
    )
    assert principal.issuer.endswith("/team/")
    assert principal.subject == "user-123"
    with pytest.raises(ValidationError):
        principal.subject = "another-user"


@pytest.mark.parametrize(
    "issuer",
    [
        "not-an-issuer",
        "file:///secret",
        "https://user:password@example.test",
        "https://example.test?token=x",
    ],
)
def test_principal_issuer_rejects_paths_credentials_and_queries(issuer):
    with pytest.raises(ValidationError):
        AuthenticatedPrincipal(issuer=issuer, subject="user-123")


def test_identity_and_auth_session_models_do_not_accept_token_or_execution_session_fields():
    with pytest.raises(ValidationError):
        AuthSessionReference(auth_session_ref=uuid4(), access_token="not-an-allowed-field")
    with pytest.raises(ValidationError):
        AuthSessionReference(session_id=uuid4())
    with pytest.raises(ValidationError):
        AuthSessionReference(auth_session_ref="cookie-credential")
    reference = AuthSessionReference(auth_session_ref=uuid4())
    with pytest.raises(ValidationError):
        reference.auth_session_ref = uuid4()


def test_artifacts_and_configuration_are_opaque_references_with_no_secret_payloads():
    artifact = ArtifactReference(
        artifact_id=uuid4(), sha256="a" * 64, media_type="application/json"
    )
    assert ArtifactReference.model_validate_json(artifact.model_dump_json()) == artifact
    config = ConfigurationReference(config_id=uuid4(), version="azure-v1")
    with pytest.raises(ValidationError):
        config.version = "azure-v2"
    with pytest.raises(ValidationError):
        ArtifactReference(artifact_id="C:/private/data.csv", sha256="a" * 64, media_type="text/csv")
    with pytest.raises(ValidationError):
        ConfigurationReference(config_id=uuid4(), version="v1", api_key="not-allowed")


def test_error_wire_format_uses_safe_codes_and_bounded_schema_locations():
    detail = ValidationDetail(location=("body", "columns", 0), code="invalid_value")
    error = ErrorEnvelope(
        code="validation_error",
        message="Request validation failed",
        request_id=uuid4(),
        details=(detail,),
    )
    assert ErrorEnvelope.model_validate_json(error.model_dump_json()) == error
    with pytest.raises(ValidationError):
        ValidationDetail(location=("body",), code="invalid_value", input="raw-value")
    with pytest.raises(ValidationError):
        ErrorEnvelope(
            code="validation_error",
            message="Request validation failed",
            request_id=uuid4(),
            details=(detail,) * 21,
        )
    with pytest.raises(ValidationError):
        ErrorEnvelope(code="internal_error", message="x" * 257, request_id=uuid4())
    with pytest.raises(ValidationError):
        ErrorEnvelope(code="500 ERROR", message="Failure", request_id=uuid4())


def test_unknown_top_level_fields_are_rejected(event_data):
    event_data["access_token"] = "not-an-envelope-field"
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(event_data)


def test_contract_import_requires_no_configuration_or_infrastructure(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    script = """
import socket
def deny_network(*args, **kwargs):
    raise AssertionError("Contract import attempted a network connection")
socket.socket.connect = deny_network
import statistics_agent.contracts
print("contracts imported")
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=15
    )
    assert result.stdout.strip() == "contracts imported"

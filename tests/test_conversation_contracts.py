"""Conversation/control contracts are additive; no HTTP or repository behavior."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from statistics_agent.agent.prompts import CONTROL_FUNCTIONS
from statistics_agent.agent.tool_schema import ToolBinding
from statistics_agent.contracts.agent import AgentVersions, StatisticsAnswer
from statistics_agent.contracts.common import ArtifactReference, ConfigurationReference
from statistics_agent.contracts.conversations import (
    ClarificationOption,
    Conversation,
    ConversationMessage,
    ConversationRun,
    DatasetSelection,
    DatasetSnapshot,
    Page,
    PageRequest,
    RequestClarification,
    ResultProvenance,
    RunError,
    RunSubmission,
)
from statistics_agent.tools.statistics import STATISTICS_BINDINGS


def values():
    reference = ConfigurationReference(config_id=uuid4(), version="contracts-v1")
    snapshot = DatasetSnapshot(dataset_id=uuid4(), sha256="0" * 64)
    conversation, run = uuid4(), uuid4()
    result = ResultProvenance(
        conversation_id=conversation,
        run_id=run,
        tool_call_id=uuid4(),
        dataset=snapshot,
        artifact=ArtifactReference(
            artifact_id=uuid4(), sha256="1" * 64, media_type="application/json"
        ),
    )
    return dict(
        conversation_id=conversation,
        run_id=run,
        lifecycle="finished",
        outcome="completed",
        created_at=datetime.now(UTC),
        finished_at=datetime.now(UTC) + timedelta(seconds=1),
        versions=AgentVersions(
            provider=reference, prompt=reference, toolset=reference, policy=reference
        ),
        selection=(snapshot,),
        results=(result,),
    )


@pytest.mark.parametrize("outcome", ["completed", "needs_input", "unsupported", "incomplete"])
def test_terminal_outcomes_roundtrip_and_preserve_verified_results(outcome):
    data = values() | {"outcome": outcome}
    if outcome == "needs_input":
        data["clarification"] = RequestClarification(question="Which dataset?")
    result = ConversationRun(**data)
    assert ConversationRun.model_validate_json(result.model_dump_json()) == result
    assert result.results == data["results"]


@pytest.mark.parametrize("lifecycle", ["queued", "running"])
def test_active_run_has_no_terminal_outcome(lifecycle):
    data = values() | {"lifecycle": lifecycle, "outcome": None, "finished_at": None, "results": ()}
    if lifecycle == "running":
        data["started_at"] = data["created_at"]
    assert ConversationRun(**data).outcome is None


@pytest.mark.parametrize(
    "change",
    [
        {"outcome": None},
        {"finished_at": None},
        {"results": ()},
        {"lifecycle": "queued"},
        {"lifecycle": "running"},
        {"outcome": "needs_input"},
        {"clarification": RequestClarification(question="Which column?")},
        {"created_at": datetime.now()},
        {"started_at": datetime(2000, 1, 1, tzinfo=UTC)},
        {"finished_at": datetime(2000, 1, 1, tzinfo=UTC)},
        {"lifecycle": "cancelled"},
        {"owner": "caller-supplied"},
    ],
)
def test_run_rejects_impossible_states_and_extra_fields(change):
    with pytest.raises(ValidationError):
        ConversationRun(**(values() | change))


@pytest.mark.parametrize("field", ["conversation_id", "run_id", "tool_call_id", "dataset"])
def test_result_identity_and_snapshot_must_match_run(field):
    data = values()
    first = data["results"][0]
    update = {
        field: DatasetSnapshot(dataset_id=first.dataset.dataset_id, sha256="2" * 64)
        if field == "dataset"
        else uuid4()
    }
    if field == "tool_call_id":
        data["results"] = (first, first)
    else:
        data["results"] = (ResultProvenance(**(first.model_dump() | update)),)
    with pytest.raises(ValidationError):
        ConversationRun(**data)


def test_selections_and_prior_references_are_bounded_and_unique():
    ids = tuple(uuid4() for _ in range(16))
    assert len(DatasetSelection(dataset_ids=ids).dataset_ids) == 16
    for bad in (ids + (uuid4(),), (ids[0], ids[0])):
        with pytest.raises(ValidationError):
            DatasetSelection(dataset_ids=bad)
    result = values()["results"][0]
    request = RunSubmission(
        idempotency_key=uuid4(), question="Describe this dataset.", prior_results=(result,)
    )
    assert RunSubmission.model_validate_json(request.model_dump_json()) == request
    with pytest.raises(ValidationError):
        RunSubmission(
            idempotency_key=uuid4(), question="Compare results.", prior_results=(result, result)
        )


@pytest.mark.parametrize("question", ["", "   ", "x" * 8193, 12, True])
def test_question_validation_is_strict(question):
    with pytest.raises(ValidationError):
        RunSubmission(idempotency_key=uuid4(), question=question)


def test_clarification_is_control_and_does_not_change_legacy_answers():
    option = ClarificationOption(key="dataset-1", text="First dataset")
    control = RequestClarification(question="Which dataset?", options=(option,))
    assert RequestClarification.model_validate_json(control.model_dump_json()) == control
    schema = ToolBinding(
        "request_clarification", "Ask for missing input.", RequestClarification
    ).schema()
    assert schema["function"]["parameters"]["additionalProperties"] is False
    assert "request_clarification" in CONTROL_FUNCTIONS
    assert all(binding.name != "request_clarification" for binding in STATISTICS_BINDINGS)
    with pytest.raises(ValidationError):
        StatisticsAnswer(status="needs_input", explanation="Missing input")
    for bad in (
        {"question": " "},
        {"question": "x" * 513},
        {"question": "Which?", "options": (option, option)},
        {
            "question": "Which?",
            "options": tuple(
                ClarificationOption(key=f"option-{i}", text="Choice") for i in range(9)
            ),
        },
        {"question": "Which?", "code": "arbitrary"},
    ):
        with pytest.raises(ValidationError):
            RequestClarification(**bad)


@pytest.mark.parametrize("limit", [0, 101, True, "50"])
def test_pagination_limit_is_bounded_and_strict(limit):
    with pytest.raises(ValidationError):
        PageRequest(limit=limit)


def test_conversation_message_pages_and_errors_are_separate_from_auth_sessions():
    conversation = Conversation(
        conversation_id=uuid4(), title="Statistics", created_at=datetime.now(UTC)
    )
    message = ConversationMessage(
        message_id=uuid4(),
        conversation_id=conversation.conversation_id,
        run_id=uuid4(),
        sequence=1,
        role="user",
        content="Describe data.",
        created_at=datetime.now(UTC),
    )
    page = Page[ConversationMessage](items=(message,), next_cursor="opaque-server-cursor")
    assert Page[ConversationMessage].model_validate_json(page.model_dump_json()) == page
    assert "content" not in repr(message) and "Statistics" not in repr(conversation)
    with pytest.raises(ValidationError):
        ConversationMessage(**(message.model_dump() | {"auth_session_ref": uuid4()}))
    with pytest.raises(ValidationError):
        PageRequest(cursor="x" * 513)
    with pytest.raises(ValidationError):
        Page[ConversationMessage](items=(message,) * 101)
    assert RunError(code="conversation_busy", request_id=uuid4()).code == "conversation_busy"
    with pytest.raises(ValidationError):
        RunError(code="raw_database_error", request_id=uuid4())

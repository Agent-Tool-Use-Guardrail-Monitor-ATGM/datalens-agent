"""Public shared contracts; importing them loads no service configuration."""

from statistics_agent.contracts.common import (
    ArtifactReference,
    AuthenticatedPrincipal,
    AuthSessionReference,
    ConfigurationReference,
)
from statistics_agent.contracts.events import EventEnvelope, EventType
from statistics_agent.contracts.http import ErrorEnvelope, ValidationDetail, resolve_request_id

__all__ = [
    "ArtifactReference",
    "AuthenticatedPrincipal",
    "AuthSessionReference",
    "ConfigurationReference",
    "ErrorEnvelope",
    "EventEnvelope",
    "EventType",
    "ValidationDetail",
    "resolve_request_id",
]

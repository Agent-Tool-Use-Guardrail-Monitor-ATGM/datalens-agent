"""Agent-only ORM metadata for future migrations; no business tables yet."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """All future agent models share this metadata, never Keycloak's models."""

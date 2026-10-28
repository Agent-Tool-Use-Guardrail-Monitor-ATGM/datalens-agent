"""Exact issuer/subject filtering for agent-owned data, including admin callers."""

from sqlalchemy import and_

from statistics_agent.contracts.auth import AuthorizedPrincipal


def owner_filter(issuer_column, subject_column, principal: AuthorizedPrincipal):
    """Apply inside the resource query; columns come from service-owned models.

    Never accept an owner supplied by a client. Role membership does not widen
    this predicate. A filtered resource miss should become the same 404 as an
    unknown resource; do not fetch another owner's row and filter it afterward.
    """
    return and_(issuer_column == principal.issuer, subject_column == principal.subject)

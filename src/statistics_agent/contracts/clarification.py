"""Bounded clarification control values, independent of execution and storage."""

from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from statistics_agent.contracts.common import Contract, ShortReference


class ClarificationOption(Contract):
    key: ShortReference
    text: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=256, pattern=r"\S")
    ] = Field(repr=False)


class RequestClarification(Contract):
    """Arguments for request_clarification; control only, not a numerical tool."""

    question: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=512, pattern=r"\S")
    ] = Field(repr=False)
    options: tuple[ClarificationOption, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def unique_options(self):
        if "\x00" in self.question or any("\x00" in option.text for option in self.options):
            raise ValueError("Clarification cannot contain null characters")
        keys = [option.key for option in self.options]
        if len(keys) != len(set(keys)):
            raise ValueError("Clarification option keys must be unique")
        return self

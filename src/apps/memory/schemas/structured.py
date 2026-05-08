"""Pydantic schemas used for memory ingest structured outputs."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

RELATIVE_MARKDOWN_PATH = r"^[^/.\n][^\n]*\.md$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageWriteOp(StrictModel):
    path: str = Field(min_length=1, pattern=RELATIVE_MARKDOWN_PATH)
    markdown: str = Field(min_length=1)


class PageDeleteOp(StrictModel):
    path: str = Field(min_length=1, pattern=RELATIVE_MARKDOWN_PATH)


class PageMarkdownOp(StrictModel):
    markdown: str = Field(min_length=1)


class ExistingPageUpdatePayload(StrictModel):
    create_pages: list[PageMarkdownOp] = Field(max_length=0)
    update_pages: list[PageMarkdownOp] = Field(min_length=1, max_length=1)
    notes: str


class NewPageCreatePayload(StrictModel):
    create_pages: list[PageMarkdownOp] = Field(max_length=1)
    update_pages: list[PageMarkdownOp] = Field(max_length=0)
    notes: str


class FinalizePageOpsPayload(StrictModel):
    create_pages: list[PageWriteOp] = Field(default_factory=list)
    update_pages: list[PageWriteOp] = Field(default_factory=list)
    delete_pages: list[PageDeleteOp] = Field(default_factory=list)
    notes: str

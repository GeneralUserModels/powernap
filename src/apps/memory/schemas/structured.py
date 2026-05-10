"""Pydantic schemas used for memory ingest structured outputs."""

from __future__ import annotations

from typing import Literal

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


class InventoryPayload(StrictModel):
    mode: Literal["first_run", "incremental", "no_new_data"]
    sources_to_read: list[str]
    existing_pages_to_read: list[str]
    likely_pages_to_create: list[str]
    likely_pages_to_update: list[str]
    backfill_sources_to_sample: list[str]
    rationale: str


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

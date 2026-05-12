"""Pydantic schemas used for memory ingest structured outputs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RELATIVE_MARKDOWN_PATH = r"^[^/.\n][^\n]*\.md$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageWriteOp(StrictModel):
    path: str = Field(
        min_length=1,
        pattern=RELATIVE_MARKDOWN_PATH,
        description="Memory-wiki markdown path relative to the memory directory.",
    )
    markdown: str = Field(
        min_length=1,
        description="Full replacement markdown for this page.",
    )


class PageDeleteOp(StrictModel):
    path: str = Field(
        min_length=1,
        pattern=RELATIVE_MARKDOWN_PATH,
        description="Normal content-page path to delete, relative to the memory directory.",
    )


class PageMarkdownOp(StrictModel):
    markdown: str = Field(
        min_length=1,
        description="Full markdown for one normal content page, including YAML frontmatter.",
    )


class InventoryPayload(StrictModel):
    mode: Literal["first_run", "incremental", "no_new_data"] = Field(
        description="The ingest mode from the prompt."
    )
    sources_to_read: list[str] = Field(
        description="Log source paths, relative to the logs directory, that the content passes should inspect."
    )
    existing_pages_to_read: list[str] = Field(
        description="Existing memory page paths, relative to the memory directory, worth reading or updating."
    )
    likely_pages_to_create: list[str] = Field(
        description="Natural titles for grounded new memory pages to consider creating."
    )
    likely_pages_to_update: list[str] = Field(
        description="Existing memory page paths, relative to the memory directory, likely to need updates."
    )
    backfill_sources_to_sample: list[str] = Field(
        description="Older source paths to sample for coverage, enrichment, or first-run bootstrapping."
    )
    rationale: str = Field(description="Brief explanation of why this plan is grounded and bounded.")


class ExistingPageUpdatePayload(StrictModel):
    create_pages: list[PageMarkdownOp] = Field(
        max_length=0,
        description="Always empty for an existing-page update pass.",
    )
    update_pages: list[PageMarkdownOp] = Field(
        min_length=1,
        max_length=1,
        description="Exactly one full replacement markdown document for the owned target page; no path is needed.",
    )
    notes: str = Field(description="Short note about what changed or why the page was left unchanged.")


class NewPageCreatePayload(StrictModel):
    create_pages: list[PageMarkdownOp] = Field(
        max_length=1,
        description="Zero or one grounded new page markdown document; no path is needed.",
    )
    update_pages: list[PageMarkdownOp] = Field(
        max_length=0,
        description="Always empty for a new-page create pass.",
    )
    notes: str = Field(description="Short create/skip rationale.")


class FinalizePageOpsPayload(StrictModel):
    create_pages: list[PageWriteOp] = Field(
        description="Minimal grounded stub pages needed only to repair validation issues.",
    )
    update_pages: list[PageWriteOp] = Field(
        description="Full replacements for index.md, log.md, schema.md, or validation-repair pages.",
    )
    delete_pages: list[PageDeleteOp] = Field(
        description="Existing normal content pages that are safe to delete after a move or split; omit special, archived, hidden, or absent paths.",
    )
    notes: str = Field(description="Short summary of finalize repairs.")

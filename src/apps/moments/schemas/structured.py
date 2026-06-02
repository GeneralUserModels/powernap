"""Pydantic schemas used for moments structured LLM outputs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidatePayload(StrictModel):
    id: str
    slug: str
    topic: str
    title: str
    description: str
    cadence: Literal["once", "scheduled", "trigger"]
    schedule: str
    trigger: str
    confidence: float = Field(ge=0, le=1)
    usefulness: int = Field(ge=1, le=10)
    disregard: int = Field(default=5, ge=1, le=10)
    surprise: int = Field(default=5, ge=1, le=10)
    is_update: bool = False  # True iff this draft reuses an existing accepted moment's slug.
    specific_instructions: str
    desired_artifact: str
    likely_next_need: str
    why_now: str
    user_value: str

    @model_validator(mode="after")
    def validate_cadence_fields(self):
        if self.cadence == "scheduled" and not self.schedule.strip():
            raise ValueError("scheduled candidates require non-empty schedule")
        if self.cadence == "trigger" and not self.trigger.strip():
            raise ValueError("trigger candidates require non-empty trigger")
        return self


class DiscoveryPayload(StrictModel):
    tasks: list[CandidatePayload] = []


class PromotionReject(StrictModel):
    id: str
    reason: str


class PromotionRank(StrictModel):
    id: str
    score: int
    reason: str


class PromotionPayload(StrictModel):
    ranked: list[PromotionRank]
    rejected: list[PromotionReject] = []


class TriggerPayload(StrictModel):
    fired: list[str] = []

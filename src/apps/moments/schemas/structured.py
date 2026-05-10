"""Pydantic schemas used for moments structured LLM outputs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


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
    confidence: float
    usefulness: int
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


class DraftRejectOp(StrictModel):
    id: str
    reason: str


class DiscoveryPayload(StrictModel):
    tasks: list[CandidatePayload] = []


class ReconcileUpdate(StrictModel):
    candidate_id: str
    accepted_slug: str
    reason: str


class ReconcilePayload(StrictModel):
    candidates: list[CandidatePayload]
    updates: list[ReconcileUpdate] = []
    rejected: list[DraftRejectOp] = []
    notes: str = ""


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

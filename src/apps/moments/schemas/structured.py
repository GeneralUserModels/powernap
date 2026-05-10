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
    evidence: list[str]
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


class MomentIdea(StrictModel):
    likely_next_need: str
    title: str
    topic_hint: str
    artifact: str
    why_useful: str
    evidence: list[str]
    cadence_hint: Literal["once", "scheduled", "trigger"]
    relation_to_existing: Literal["new", "possible_update", "duplicate", "weak"]


class IdeaPayload(StrictModel):
    ideas: list[MomentIdea] = []
    notes: str = ""


class DraftRejectOp(StrictModel):
    id: str
    reason: str


class DraftRemoveOp(StrictModel):
    id: str
    reason: str


class DraftActionPayload(StrictModel):
    upserts: list[CandidatePayload] = []
    rejected: list[DraftRejectOp] = []
    remove: list[DraftRemoveOp] = []
    notes: str = ""


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

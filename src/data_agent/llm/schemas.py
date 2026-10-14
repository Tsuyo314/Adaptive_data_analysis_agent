from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuestionSelection(StrictModel):
    question_ids: list[str]
    rationale: str


class PlanProposal(StrictModel):
    tool_ids: list[str]
    rationale: str


class ReflectionClass(str, Enum):
    FILE_OR_ACCESS = "file_or_access"
    DATA_OR_ANALYSIS = "data_or_analysis"
    REQUIREMENTS_CHANGE = "requirements_change"
    EXECUTION = "execution"


class RestartNode(str, Enum):
    QUESTIONS = "questions"
    PLAN = "plan"
    PREPROCESS = "preprocess"
    CURRENT = "current"
    HUMAN_REVIEW = "human_review"


class Reflection(StrictModel):
    classification: ReflectionClass
    restart_node: RestartNode
    advice: str


class NarrativeItem(StrictModel):
    text: str
    evidence_ids: list[str] = Field(min_length=1)


class ReportNarrative(StrictModel):
    summary: list[NarrativeItem]
    interpretations: list[NarrativeItem]
    limitations: list[NarrativeItem]
    next_actions: list[NarrativeItem]

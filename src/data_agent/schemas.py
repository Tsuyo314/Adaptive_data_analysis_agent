from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class QuestionKind(str, Enum):
    YES_NO = "yes_no"
    NUMBERED_CHOICE = "numbered_choice"
    MULTI_CHOICE = "multi_choice"
    FREE_TEXT = "free_text"


class Question(BaseModel):
    id: str
    category: Literal["goal", "scope", "preprocessing", "statistics", "causal", "prediction"]
    text: str
    kind: QuestionKind
    required: bool = True
    choices: list[str] = Field(default_factory=list)
    depends_on: dict[str, Any] = Field(default_factory=dict)
    completion_key: str

    @model_validator(mode="after")
    def validate_choices(self) -> Question:
        if self.kind == QuestionKind.YES_NO and self.choices:
            raise ValueError("yes_no questions must not define choices")
        if self.kind == QuestionKind.NUMBERED_CHOICE and len(self.choices) < 3:
            raise ValueError("numbered questions require at least three choices")
        if self.kind == QuestionKind.MULTI_CHOICE and len(self.choices) < 2:
            raise ValueError("multi-choice questions require at least two choices")
        return self


class Answer(BaseModel):
    question_id: str
    choice: str
    value: Any
    notes: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    id: str
    title: str
    tool: str
    description: str
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    rule_id: str
    stage: str
    passed: bool
    severity: Literal["info", "warning", "error"]
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)

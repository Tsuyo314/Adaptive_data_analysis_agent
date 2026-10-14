from __future__ import annotations

from typing import Any, Literal

from typing_extensions import NotRequired, TypedDict


class AnalysisState(TypedDict):
    run_id: str
    thread_id: str
    working_directory: str
    user_request: str
    plan_version: int
    approval_status: Literal["pending", "approved", "rejected"]
    retry_counts: dict[str, int]
    status: str
    dataset_path: NotRequired[str]
    supporting_file_paths: NotRequired[list[str]]
    supporting_file_summaries: NotRequired[list[dict[str, Any]]]
    dataset_hash: NotRequired[str]
    dataset_profile: NotRequired[dict[str, Any]]
    answers: NotRequired[dict[str, dict[str, Any]]]
    analysis_options: NotRequired[list[dict[str, Any]]]
    pending_questions: NotRequired[list[dict[str, Any]]]
    plan: NotRequired[list[dict[str, Any]]]
    causal_design: NotRequired[dict[str, Any]]
    artifact_paths: NotRequired[list[str]]
    validation_results: NotRequired[list[dict[str, Any]]]
    active_nodes: NotRequired[list[str]]
    report_path: NotRequired[str]
    error: NotRequired[dict[str, Any]]
    outside_workspace_approved: NotRequired[bool]
    question_rounds: NotRequired[int]
    llm_provider: NotRequired[Literal["deterministic", "chatgpt", "openrouter"]]
    llm_model: NotRequired[str]
    llm_status: NotRequired[str]
    llm_calls: NotRequired[list[str]]
    pending_llm_operation: NotRequired[dict[str, Any]]
    llm_reselection_reason: NotRequired[str]

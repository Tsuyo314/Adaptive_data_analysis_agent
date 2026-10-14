from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from data_agent.config import Settings
from data_agent.graph import build_graph
from data_agent.llm import LLMService, ProviderReselectionRequired
from data_agent.llm.schemas import PlanProposal, QuestionSelection, Reflection, ReportNarrative
from data_agent.schemas import PlanStep
from data_agent.specialists.causal import run_causal_analysis
from data_agent.state import AnalysisState
from data_agent.storage import RunStore
from data_agent.tools.analysis import create_visualizations, describe, load_dataset, preprocess
from data_agent.tools.discovery import (
    discover_workspace,
    inspect_dataset,
    inspect_supporting_files,
    select_dataset,
)
from data_agent.tools.questions import select_questions
from data_agent.tools.reporting import build_report_data, save_report
from data_agent.validators.core import (
    has_blocking_failures,
    validate_data,
    validate_final,
    validate_results,
)


class Pipeline:
    def __init__(
        self,
        state: AnalysisState,
        settings: Settings,
        store: RunStore,
        llm_service: LLMService | None = None,
    ):
        self.state = state
        self.settings = settings
        self.store = store
        self.llm_service = llm_service
        self.graph = build_graph(
            {
                "discover": self._node("discover", self._discover),
                "plan": self._node("plan", self._plan),
                "preprocess": self._node("preprocess", self._preprocess),
                "validate_data": self._node("validate_data", self._validate_data),
                "eda": self._node("eda", self._eda),
                "validate_results": self._node("validate_results", self._validate_results),
                "specialist": self._node("causal_specialist", self._specialist),
                "validate_specialist": self._node("validate_specialist", self._validate_specialist),
                "report": self._node("report", self._report),
            }
        )

    @classmethod
    def create(
        cls,
        user_request: str,
        workspace: Path | None = None,
        settings: Settings | None = None,
        run_id: str | None = None,
        allow_outside_workspace: bool = False,
        llm_provider: str = "deterministic",
        llm_model: str = "",
        llm_service: LLMService | None = None,
    ) -> Pipeline:
        workspace = (workspace or Path.cwd()).resolve()
        settings = settings or Settings.load(workspace / "conf/config.yaml")
        run_id = run_id or f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        state: AnalysisState = {
            "run_id": run_id,
            "thread_id": str(uuid.uuid4()),
            "working_directory": str(workspace),
            "user_request": user_request,
            "plan_version": 1,
            "approval_status": "pending",
            "retry_counts": {},
            "status": "initialized",
            "answers": {},
            "artifact_paths": [],
            "validation_results": [],
            "active_nodes": [],
            "outside_workspace_approved": allow_outside_workspace,
            "question_rounds": 0,
            "llm_provider": llm_provider,
            "llm_model": llm_model,
            "llm_status": "disabled" if llm_provider == "deterministic" else "ready",
            "llm_calls": [],
        }
        store = RunStore(workspace / settings.runs_dir, run_id)
        store.initialize(state, config=asdict(settings))
        return cls(state, settings, store, llm_service)

    @classmethod
    def resume(
        cls,
        run_id: str,
        workspace: Path | None = None,
        settings: Settings | None = None,
        llm_service: LLMService | None = None,
    ) -> Pipeline:
        workspace = (workspace or Path.cwd()).resolve()
        settings = settings or Settings.load(workspace / "conf/config.yaml")
        store = RunStore(workspace / settings.runs_dir, run_id)
        return cls(store.load_state(), settings, store, llm_service)

    def select_llm_provider(
        self, provider: str, model: str = "", service: LLMService | None = None
    ) -> AnalysisState:
        if provider not in {"deterministic", "chatgpt", "openrouter"}:
            raise ValueError(f"未対応のAIプロバイダーです: {provider}")
        self.llm_service = service
        self.state["llm_provider"] = provider
        self.state["llm_model"] = model
        self.state["llm_status"] = "disabled" if provider == "deterministic" else "ready"
        pending = self.state.pop("pending_llm_operation", None)
        self.state.pop("llm_reselection_reason", None)
        self.state.pop("error", None)
        if self.state.get("status") == "awaiting_llm_provider" and pending:
            self.state["status"] = str(pending["resume_status"])
        self.store.checkpoint(self.state)
        return self.state

    def run_until_pause(self) -> AnalysisState:
        self.state = self.graph.invoke(
            self.state, config={"configurable": {"thread_id": self.state["thread_id"]}}
        )
        return self.state

    def submit_answers(self, answers: dict[str, dict[str, Any]]) -> AnalysisState:
        self.state["question_rounds"] = self.state.get("question_rounds", 0) + 1
        merged = {**self.state.get("answers", {}), **answers}
        self.state["answers"] = merged
        self.store.write_json("questions/answers.json", merged)
        if merged.get("documentation_mismatch_ack", {}).get("value") is False:
            self.state["status"] = "human_review"
            self.state["error"] = {
                "node": "questions",
                "type": "DocumentationMismatch",
                "message": "関連資料と実データの矛盾が未解決です",
                "classification": "requirements_change",
            }
            self.store.checkpoint(self.state)
            return self.state
        try:
            pending = self._select_questions(self.state, merged)
        except ProviderReselectionRequired as exc:
            if self.llm_service:
                self._record_llm_call(self.llm_service, self.state)
            self._pause_for_llm(self.state, exc, "discover", "initialized")
            return self.state
        if pending:
            if self.state["question_rounds"] >= self.settings.max_question_rounds:
                self.state["status"] = "human_review"
                self.state["error"] = {
                    "node": "questions",
                    "type": "QuestionLimitExceeded",
                    "message": "質問ラウンド上限に達しました",
                    "classification": "requirements_change",
                }
                self.store.checkpoint(self.state)
                return self.state
            self.state["pending_questions"] = [item.model_dump(mode="json") for item in pending]
            self.state["status"] = "awaiting_questions"
        else:
            self.state["pending_questions"] = []
            self.state["status"] = "questions_complete"
        self.store.checkpoint(self.state)
        return self.state

    def approve_plan(self, approved: bool, feedback: str = "") -> AnalysisState:
        self.store.approval("plan", approved, feedback)
        if approved:
            self.state["approval_status"] = "approved"
            self.state["status"] = "plan_approved"
        else:
            self.state["approval_status"] = "rejected"
            self.state.setdefault("answers", {})["plan_feedback"] = {
                "question_id": "plan_feedback",
                "choice": feedback,
                "value": feedback,
                "notes": [],
            }
            self.state["plan_version"] += 1
            self.state["status"] = "questions_complete"
        self.store.checkpoint(self.state)
        return self.state

    def approve_intermediate(self, approved: bool, feedback: str = "") -> AnalysisState:
        self.store.approval("intermediate", approved, feedback)
        if approved:
            self.state["status"] = "intermediate_approved"
        else:
            self.state.setdefault("answers", {})["intermediate_feedback"] = {
                "question_id": "intermediate_feedback",
                "choice": feedback,
                "value": feedback,
                "notes": [],
            }
            self.state["plan_version"] += 1
            self.state["approval_status"] = "pending"
            self.state["status"] = "questions_complete"
            self._invalidate_downstream()
        self.store.checkpoint(self.state)
        return self.state

    def _node(
        self, name: str, function: Callable[[AnalysisState], AnalysisState]
    ) -> Callable[[AnalysisState], AnalysisState]:
        def wrapped(state: AnalysisState) -> AnalysisState:
            state["active_nodes"] = [name]
            self.store.event(name, "start", "running", {"status": state["status"]})
            started = time.perf_counter()
            try:
                state = function(state)
                duration = (time.perf_counter() - started) * 1000
                state["active_nodes"] = []
                self.store.event(name, "end", "completed", duration_ms=duration)
                self.store.checkpoint(state)
                self.state = state
                return state
            except ProviderReselectionRequired as exc:
                duration = (time.perf_counter() - started) * 1000
                if self.llm_service:
                    self._record_llm_call(self.llm_service, state)
                state["status"] = "awaiting_llm_provider"
                state["llm_status"] = "reselection_required"
                state["llm_reselection_reason"] = exc.reason
                state["pending_llm_operation"] = {
                    "task": exc.task,
                    "node": name,
                    "resume_status": self._resume_status_for_node(name),
                }
                state["active_nodes"] = []
                self.store.event(
                    name,
                    "llm_provider_required",
                    "paused",
                    {"task": exc.task, "reason": exc.reason},
                    duration_ms=duration,
                )
                self.store.checkpoint(state)
                self.state = state
                return state
            except Exception as exc:
                duration = (time.perf_counter() - started) * 1000
                count = state["retry_counts"].get(name, 0) + 1
                state["retry_counts"][name] = count
                state["error"] = {
                    "node": name,
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "classification": self._classify_error(exc),
                }
                try:
                    reflection = self._reflect(name, state["error"], state)
                    if reflection:
                        state["error"]["reflection"] = reflection
                except ProviderReselectionRequired as llm_exc:
                    if self.llm_service:
                        self._record_llm_call(self.llm_service, state)
                    self._pause_for_llm(
                        state, llm_exc, name, self._resume_status_for_node(name), duration
                    )
                    self.state = state
                    return state
                self.store.event(
                    name,
                    "failure",
                    "failed",
                    state["error"],
                    duration_ms=duration,
                )
                self.store.error(name, state["error"])
                if count > self.settings.max_retries:
                    state["status"] = "human_review"
                    state["active_nodes"] = []
                    self.store.checkpoint(state)
                    return state
                self.store.event(name, "retry", "scheduled", {"attempt": count})
                state["active_nodes"] = []
                self.store.checkpoint(state)
                return state

        return wrapped

    @staticmethod
    def _resume_status_for_node(name: str) -> str:
        return {
            "discover": "initialized",
            "plan": "questions_complete",
            "preprocess": "plan_approved",
            "validate_data": "preprocessed",
            "eda": "data_valid",
            "validate_results": "eda_complete",
            "causal_specialist": "intermediate_approved",
            "validate_specialist": "specialist_complete",
            "report": "specialist_valid",
        }[name]

    def _reflect(
        self, node: str, error: dict[str, Any], state: AnalysisState
    ) -> dict[str, Any] | None:
        if state.get("llm_provider", "deterministic") == "deterministic":
            return None
        service = self._require_llm("reflector")
        result = service.generate(
            "reflector",
            {
                "node": node,
                "error_type": error["type"],
                "deterministic_classification": error["classification"],
            },
            Reflection,
        )
        self._record_llm_call(service, state)
        return result.model_dump(mode="json")

    def _pause_for_llm(
        self,
        state: AnalysisState,
        exc: ProviderReselectionRequired,
        node: str,
        resume_status: str,
        duration_ms: float | None = None,
    ) -> None:
        state["status"] = "awaiting_llm_provider"
        state["llm_status"] = "reselection_required"
        state["llm_reselection_reason"] = exc.reason
        state["pending_llm_operation"] = {
            "task": exc.task,
            "node": node,
            "resume_status": resume_status,
        }
        state["active_nodes"] = []
        self.store.event(
            node,
            "llm_provider_required",
            "paused",
            {"task": exc.task, "reason": exc.reason},
            duration_ms=duration_ms,
        )
        self.store.checkpoint(state)

    @staticmethod
    def _classify_error(exc: Exception) -> str:
        if isinstance(exc, (FileNotFoundError, PermissionError, UnicodeError)):
            return "file_or_access"
        if isinstance(exc, (ValueError, TypeError, KeyError)):
            return "data_or_analysis"
        return "execution"

    def _discover(self, state: AnalysisState) -> AnalysisState:
        workspace = Path(state["working_directory"])
        manifest = discover_workspace(workspace, self.settings)
        dataset = select_dataset(
            manifest,
            state["user_request"],
            workspace,
            allow_outside_workspace=bool(state.get("outside_workspace_approved", False)),
        )
        profile = inspect_dataset(dataset, self.settings)
        support = inspect_supporting_files(manifest, self.settings, dataset)
        self.store.write_json("inputs/workspace_manifest.json", manifest)
        self.store.write_json("inputs/dataset_profile.json", profile)
        self.store.write_json("inputs/documents_summary.json", support)
        state["dataset_path"] = str(dataset)
        state["dataset_hash"] = profile["sha256"]
        state["dataset_profile"] = profile
        state["supporting_file_paths"] = [item["path"] for item in support]
        state["supporting_file_summaries"] = support
        state["analysis_options"] = self._analysis_options(profile)
        extracted = self._extract_request_answers(state["user_request"], profile["columns"])
        mismatches = self._documentation_mismatches(support, profile["columns"])
        profile["document_mismatches"] = mismatches
        state["dataset_profile"] = profile
        self.store.write_json("inputs/dataset_profile.json", profile)
        if mismatches:
            extracted["_documentation_mismatch_detected"] = {
                "question_id": "_documentation_mismatch_detected",
                "choice": "system",
                "value": True,
                "notes": mismatches,
            }
        state["answers"] = {**extracted, **state.get("answers", {})}
        pending = self._select_questions(state, state["answers"])
        state["pending_questions"] = [item.model_dump(mode="json") for item in pending]
        self.store.write_json("questions/questions_v1.json", state["pending_questions"])
        state["status"] = "awaiting_questions" if pending else "questions_complete"
        return state

    def _select_questions(
        self, state: AnalysisState, answers: dict[str, dict[str, Any]]
    ) -> list[Any]:
        candidates = select_questions(answers, self.settings.max_questions_per_round)
        if not candidates or state.get("llm_provider", "deterministic") == "deterministic":
            return candidates
        service = self._require_llm("question_planner")
        profile = state.get("dataset_profile", {})
        result = service.generate(
            "question_planner",
            {
                "request": self._safe_request(state["user_request"]),
                "dataset_metadata": self._profile_metadata(profile),
                "answered_values": {key: value.get("value") for key, value in answers.items()},
                "candidate_questions": [
                    {"id": item.id, "category": item.category, "text": item.text}
                    for item in candidates
                ],
            },
            QuestionSelection,
            allowed_ids={item.id for item in candidates},
        )
        self._record_llm_call(service, state)
        by_id = {item.id: item for item in candidates}
        ordered = [by_id[item] for item in result.question_ids]
        ordered.extend(item for item in candidates if item.id not in result.question_ids)
        return ordered[: self.settings.max_questions_per_round]

    @staticmethod
    def _documentation_mismatches(support: list[dict[str, Any]], columns: list[str]) -> list[str]:
        actual = set(columns)
        mismatches: list[str] = []
        for document in support:
            path = Path(document["path"])
            if "readme" not in path.name.lower():
                continue
            declared = set(
                re.findall(
                    r"(?m)^\s*[-*]\s*`([^`]+)`\s*:",
                    document.get("summary", ""),
                )
            )
            missing = sorted(declared - actual)
            if missing:
                mismatches.append(f"{path.name} に定義され実データにない列: {', '.join(missing)}")
        return mismatches

    @staticmethod
    def _extract_request_answers(request: str, columns: list[str]) -> dict[str, dict[str, Any]]:
        """Extract only high-confidence values; uncertain values remain unanswered."""
        lowered = request.lower()
        scope: str | None = None
        if "因果" in request or "causal" in lowered:
            scope = "因果効果の推定"
        elif "品質" in request or "quality" in lowered:
            scope = "データ品質確認"
        elif any(token in request for token in ("傾向", "記述統計", "概要")):
            scope = "傾向把握・記述統計"
        extracted: dict[str, dict[str, Any]] = {}
        if scope:
            extracted["analysis_scope"] = {
                "question_id": "analysis_scope",
                "choice": "extracted",
                "value": scope,
                "notes": ["依頼文から高確度で抽出。計画承認時に修正可能"],
            }
        mentioned = [column for column in columns if str(column).lower() in lowered]
        if len(mentioned) == 1:
            target = str(mentioned[0])
            extracted["target_column"] = {
                "question_id": "target_column",
                "choice": target,
                "value": target,
                "notes": ["依頼文で明示された列名から抽出"],
            }
        return extracted

    @staticmethod
    def _analysis_options(profile: dict[str, Any]) -> list[dict[str, Any]]:
        numeric_count = sum(
            any(token in dtype for token in ("int", "float"))
            for dtype in profile["sample_dtypes"].values()
        )
        return [
            {
                "name": "傾向把握・記述統計",
                "feasibility": "実行可能",
                "missing_conditions": [],
            },
            {
                "name": "データ品質確認",
                "feasibility": "実行可能",
                "missing_conditions": [],
            },
            {
                "name": "因果効果の推定",
                "feasibility": "追加情報が必要" if numeric_count else "現状では不適切",
                "missing_conditions": ["処置・対照", "結果指標", "交絡候補", "識別仮定"],
            },
        ]

    def _plan(self, state: AnalysisState) -> AnalysisState:
        scope = state["answers"]["analysis_scope"]["value"]
        steps = [
            PlanStep(
                id="load",
                title="データ読込",
                tool="pandas",
                description="原本を変更せずCSV/XLSXを読み込む",
                inputs=[state["dataset_path"]],
                outputs=["in-memory dataframe"],
            ),
            PlanStep(
                id="preprocess",
                title="承認済み前処理",
                tool="deterministic python",
                description="重複処理を適用し履歴とParquetを保存",
                outputs=["preprocessing/processed.parquet", "preprocessing/preprocessing_log.json"],
            ),
            PlanStep(
                id="eda",
                title="基本統計・可視化",
                tool="pandas + Plotly",
                description="欠損、型、記述統計、分布、相関を計算",
                outputs=["analysis/descriptive.json", "visualizations/*.html"],
            ),
        ]
        if scope in {"因果効果の推定", "傾向把握と因果効果"}:
            steps.append(
                PlanStep(
                    id="causal",
                    title="因果推論",
                    tool="Causal Specialist / statsmodels",
                    description="識別仮定を検査し、成立時のみ回帰調整を実行",
                    outputs=["analysis/causal/result.json"],
                )
            )
        steps.append(
            PlanStep(
                id="report",
                title="検証・レポート",
                tool="deterministic validator + template",
                description="数値整合性を検証し、同一成果物からUIとMarkdownを生成",
                outputs=["report/report_data.json", "report/report.md"],
            )
        )
        proposed_ids: list[str] = []
        used_ai_planner = state.get("llm_provider", "deterministic") != "deterministic"
        if used_ai_planner:
            service = self._require_llm("analysis_planner")
            allowed = {step.id for step in steps}
            proposal = service.generate(
                "analysis_planner",
                {
                    "request": self._safe_request(state["user_request"]),
                    "dataset_metadata": self._profile_metadata(state.get("dataset_profile", {})),
                    "answers": {
                        key: value.get("value") for key, value in state.get("answers", {}).items()
                    },
                    "allowed_tool_ids": sorted(allowed),
                    "mandatory_tool_ids": [step.id for step in steps],
                },
                PlanProposal,
                allowed_ids=allowed,
            )
            self._record_llm_call(service, state)
            proposed_ids = proposal.tool_ids
        state["plan"] = [step.model_dump(mode="json") for step in steps]
        if used_ai_planner:
            priorities = {identifier: index for index, identifier in enumerate(proposed_ids, 1)}
            for item in state["plan"]:
                item["ai_selected"] = item["id"] in priorities
                item["ai_priority"] = priorities.get(item["id"])
        self.store.write_json(f"plans/plan_v{state['plan_version']}.json", state["plan"])
        state["approval_status"] = "pending"
        state["status"] = "awaiting_plan_approval"
        return state

    def _preprocess(self, state: AnalysisState) -> AnalysisState:
        frame = load_dataset(Path(state["dataset_path"]))
        drop_duplicates = bool(state["answers"]["drop_duplicates"]["value"])
        plan = {"drop_duplicates": drop_duplicates, "missing_strategy": "keep"}
        self.store.write_json("preprocessing/preprocessing_plan.json", plan)
        _, log = preprocess(
            frame,
            self.store.root / "preprocessing/processed.parquet",
            drop_duplicates=drop_duplicates,
        )
        self.store.write_json("preprocessing/preprocessing_log.json", log)
        state.setdefault("artifact_paths", []).extend(
            [log["output_path"], self.store.artifact("preprocessing/preprocessing_log.json")]
        )
        state["status"] = "preprocessed"
        return state

    def _load_processed(self):
        log = self.store.read_json("preprocessing/preprocessing_log.json")
        path = Path(log["output_path"])
        return (
            __import__("pandas").read_parquet(path)
            if path.suffix == ".parquet"
            else __import__("pandas").read_csv(path)
        )

    def _validate_data(self, state: AnalysisState) -> AnalysisState:
        results = validate_data(self._load_processed())
        self.store.write_json("validation/data_validation.json", results)
        state.setdefault("validation_results", []).extend(results)
        if has_blocking_failures(results):
            raise ValueError("データ検証の必須ルールに不合格")
        state["status"] = "data_valid"
        return state

    def _eda(self, state: AnalysisState) -> AnalysisState:
        frame = self._load_processed()
        descriptive = describe(frame)
        self.store.write_json("analysis/descriptive.json", descriptive)
        target = str(state["answers"].get("target_column", {}).get("value", ""))
        figures = create_visualizations(frame, self.store.root / "visualizations", target)
        self.store.write_json("analysis/visualizations.json", figures)
        state.setdefault("artifact_paths", []).extend(item["path"] for item in figures)
        state["status"] = "eda_complete"
        return state

    def _validate_results(self, state: AnalysisState) -> AnalysisState:
        frame = self._load_processed()
        descriptive = self.store.read_json("analysis/descriptive.json")
        results = validate_results(frame, descriptive, None)
        self.store.write_json("validation/statistical_validation.json", results)
        state.setdefault("validation_results", []).extend(results)
        if has_blocking_failures(results):
            raise ValueError("計算結果の整合性検証に不合格")
        state["status"] = "awaiting_intermediate_approval"
        return state

    def _specialist(self, state: AnalysisState) -> AnalysisState:
        result = run_causal_analysis(self._load_processed(), state["answers"])
        self.store.write_json("analysis/causal/result.json", result)
        self.store.write_json("analysis/causal/assumptions.json", result.get("assumptions", {}))
        self.store.write_json("analysis/causal/diagnostics.json", result.get("diagnostics", {}))
        state["causal_design"] = result
        state["status"] = "specialist_complete"
        return state

    def _validate_specialist(self, state: AnalysisState) -> AnalysisState:
        frame = self._load_processed()
        descriptive = self.store.read_json("analysis/descriptive.json")
        causal = self.store.read_json("analysis/causal/result.json")
        results = validate_results(frame, descriptive, causal)
        specialist_results = [r for r in results if r["rule_id"].startswith("RESULT-CAUSAL")]
        self.store.write_json("validation/specialist_validation.json", specialist_results)
        state.setdefault("validation_results", []).extend(specialist_results)
        if has_blocking_failures(specialist_results):
            raise ValueError("専門分析の検証に不合格")
        state["status"] = "specialist_valid"
        return state

    def _report(self, state: AnalysisState) -> AnalysisState:
        descriptive_path = self.store.root / "analysis/descriptive.json"
        causal_path = self.store.root / "analysis/causal/result.json"
        validation_paths = list((self.store.root / "validation").glob("*.json"))
        validations: list[dict[str, Any]] = []
        for path in validation_paths:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, list):
                validations.extend(value)
        report_data = build_report_data(
            state,
            self.store.read_json("analysis/descriptive.json"),
            self.store.read_json("analysis/causal/result.json"),
            self.store.read_json("analysis/visualizations.json", []),
            validations,
            [str(descriptive_path.resolve()), str(causal_path.resolve())]
            + [str(path.resolve()) for path in validation_paths],
        )
        if state.get("llm_provider", "deterministic") != "deterministic":
            evidence = self._report_evidence(report_data)
            service = self._require_llm("reporter")
            narrative = service.generate(
                "reporter",
                {
                    "request": self._safe_request(state["user_request"]),
                    "answers": {
                        key: value.get("value") for key, value in state.get("answers", {}).items()
                    },
                    "evidence": evidence,
                },
                ReportNarrative,
                allowed_evidence={item["id"] for item in evidence},
            )
            self._record_llm_call(service, state)
            report_data["narrative"] = narrative.model_dump(mode="json")
        report_data["llm"] = {
            "provider": state.get("llm_provider", "deterministic"),
            "model": state.get("llm_model", ""),
        }
        data_path, report_path, html_path = save_report(report_data, self.store.root / "report")
        final = validate_final(data_path, report_path)
        final_path = self.store.write_json("validation/final_validation.json", final)
        state.setdefault("validation_results", []).extend(final)
        if has_blocking_failures(final):
            raise ValueError("保存レポートと表示データの数値が一致しません")
        # Include the final consistency rules in the shared UI/Markdown source as well.
        report_data["validations"].extend(final)
        report_data["provenance"]["source_artifacts"].append(str(final_path.resolve()))
        data_path, report_path, html_path = save_report(report_data, self.store.root / "report")
        state["report_path"] = str(report_path.resolve())
        state.setdefault("artifact_paths", []).extend(
            [str(data_path.resolve()), str(report_path.resolve()), str(html_path.resolve())]
        )
        state["status"] = "complete"
        return state

    def _require_llm(self, task: str) -> LLMService:
        if self.llm_service is None:
            message = "AI接続がこのプロセスにありません。再接続してください"
            raise ProviderReselectionRequired(task, message)
        return self.llm_service

    @staticmethod
    def _record_llm_call(service: LLMService, state: AnalysisState) -> None:
        if service.last_audit_path:
            state.setdefault("llm_calls", []).append(service.last_audit_path)

    @staticmethod
    def _safe_request(value: str) -> str:
        value = re.sub(r"(?:[A-Za-z]:)?[/\\][^\s]+", "[PATH]", value)
        return re.sub(r"\b[^\s]+\.(?:csv|xlsx)\b", "[DATASET]", value, flags=re.IGNORECASE)

    @staticmethod
    def _profile_metadata(profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "format": profile.get("format"),
            "size_bytes": profile.get("size_bytes"),
            "estimated_rows": profile.get("estimated_rows"),
            "sample_row_count": profile.get("sample_rows"),
            "columns": profile.get("columns", []),
            "dtypes": profile.get("sample_dtypes", {}),
            "missing_counts": profile.get("sample_missing", {}),
        }

    @staticmethod
    def _report_evidence(report: dict[str, Any]) -> list[dict[str, Any]]:
        descriptive = report["descriptive"]
        causal = report["causal_summary"]
        evidence = [
            {
                "id": "descriptive.overview",
                "kind": "aggregate",
                "value": {
                    "row_count": descriptive["row_count"],
                    "column_count": descriptive["column_count"],
                    "duplicate_rows": descriptive["duplicate_rows"],
                    "missing_rate": descriptive.get("missing_rate", {}),
                },
            },
            {
                "id": "descriptive.numeric_summary",
                "kind": "aggregate",
                "value": descriptive.get("numeric_summary", []),
            },
            {"id": "causal.summary", "kind": "validated_result", "value": causal},
            {
                "id": "validation.results",
                "kind": "validation",
                "value": [
                    {
                        "rule_id": item["rule_id"],
                        "passed": item["passed"],
                        "reason": item["reason"],
                    }
                    for item in report["validations"]
                ],
            },
        ]
        return evidence

    def _invalidate_downstream(self) -> None:
        for key in ("causal_design", "report_path"):
            self.state.pop(key, None)
        self.state["validation_results"] = []

    def graph_mermaid(self) -> str:
        return self.graph.get_graph().draw_mermaid()

    def graph_ascii(self) -> str:
        try:
            return self.graph.get_graph().draw_ascii()
        except Exception:
            return (
                "ASCII表示にはgrandalfが必要です。"
                "`data-agent graph --format mermaid`を利用してください。"
            )

from pathlib import Path

from data_agent.config import Settings
from data_agent.llm.base import GenerationResult, ProviderError
from data_agent.llm.service import LLMService
from data_agent.pipeline import Pipeline


class TaskProvider:
    name = "openrouter"
    model = "mock/model"

    def __init__(self, fail_task: str | None = None):
        self.tasks = []
        self.fail_task = fail_task

    def list_models(self):
        return []

    def generate_structured(self, *, task, payload, **kwargs):
        del kwargs
        self.tasks.append(task)
        if task == self.fail_task:
            raise ProviderError("mock provider failure")
        if task == "question_planner":
            return GenerationResult(
                {
                    "question_ids": [item["id"] for item in payload["candidate_questions"]],
                    "rationale": "ok",
                }
            )
        if task == "analysis_planner":
            return GenerationResult({"tool_ids": payload["mandatory_tool_ids"], "rationale": "ok"})
        if task == "reflector":
            return GenerationResult(
                {
                    "classification": "file_or_access",
                    "restart_node": "current",
                    "advice": "入力を確認する",
                }
            )
        return GenerationResult(
            {
                "summary": [
                    {
                        "text": "集計結果には明確な傾向があります",
                        "evidence_ids": ["descriptive.overview"],
                    }
                ],
                "interpretations": [],
                "limitations": [],
                "next_actions": [],
            }
        )


def make_pipeline(tmp_path: Path, provider: TaskProvider) -> Pipeline:
    (tmp_path / "data.csv").write_text("group,value\nA,1\nB,2\n", encoding="utf-8")
    settings = Settings(runs_dir="runs", max_retries=0)
    pipeline = Pipeline.create(
        "売上の傾向を分析",
        workspace=tmp_path,
        settings=settings,
        run_id="ai-run",
        llm_provider="openrouter",
        llm_model=provider.model,
    )
    pipeline.llm_service = LLMService(provider, pipeline.store)
    return pipeline


def test_ai_tasks_drive_questions_plan_and_report(tmp_path: Path):
    provider = TaskProvider()
    pipeline = make_pipeline(tmp_path, provider)
    state = pipeline.run_until_pause()
    assert state["status"] == "awaiting_questions"
    answers = {}
    for question in state["pending_questions"]:
        value = True if question["id"] == "drop_duplicates" else "value"
        answers[question["id"]] = {
            "question_id": question["id"],
            "choice": str(value),
            "value": value,
            "notes": [],
        }
    pipeline.submit_answers(answers)
    state = pipeline.run_until_pause()
    assert state["status"] == "awaiting_plan_approval"
    pipeline.approve_plan(True)
    assert pipeline.run_until_pause()["status"] == "awaiting_intermediate_approval"
    pipeline.approve_intermediate(True)
    state = pipeline.run_until_pause()
    assert state["status"] == "complete"
    assert {"question_planner", "analysis_planner", "reporter"} <= set(provider.tasks)
    assert len(state["llm_calls"]) >= 3


def test_provider_failure_pauses_and_manual_deterministic_reselection_replays(tmp_path: Path):
    provider = TaskProvider(fail_task="question_planner")
    pipeline = make_pipeline(tmp_path, provider)
    state = pipeline.run_until_pause()
    assert state["status"] == "awaiting_llm_provider"
    assert state["pending_llm_operation"]["task"] == "question_planner"
    pipeline.select_llm_provider("deterministic")
    state = pipeline.run_until_pause()
    assert state["status"] == "awaiting_questions"


def test_reflector_advises_on_deterministic_failure(tmp_path: Path):
    provider = TaskProvider()
    settings = Settings(runs_dir="runs", max_retries=0)
    pipeline = Pipeline.create(
        "傾向を分析",
        workspace=tmp_path,
        settings=settings,
        run_id="reflect-run",
        llm_provider="openrouter",
        llm_model=provider.model,
    )
    pipeline.llm_service = LLMService(provider, pipeline.store)
    state = pipeline.run_until_pause()
    assert state["status"] == "human_review"
    assert state["error"]["reflection"]["restart_node"] == "current"
    assert "reflector" in provider.tasks

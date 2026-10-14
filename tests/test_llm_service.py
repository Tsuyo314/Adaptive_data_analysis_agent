import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from data_agent.llm.base import GenerationResult, ProviderError
from data_agent.llm.preferences import Preferences
from data_agent.llm.schemas import QuestionSelection, ReportNarrative
from data_agent.llm.service import LLMService, ProviderReselectionRequired
from data_agent.storage import RunStore


class SequenceProvider:
    name = "openrouter"
    model = "mock/model"

    def __init__(self, values):
        self.values = iter(values)
        self.calls = 0
        self.payloads = []

    def list_models(self):
        return []

    def generate_structured(self, **kwargs):
        self.payloads.append(kwargs["payload"])
        self.calls += 1
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return GenerationResult(value, {"mock_tokens": 1})


def store(tmp_path: Path) -> RunStore:
    result = RunStore(tmp_path / "runs", "llm-test")
    result.initialize(
        {
            "run_id": "llm-test",
            "thread_id": "thread",
            "status": "initialized",
            "working_directory": str(tmp_path),
            "user_request": "test",
        }
    )
    return result


def test_service_repairs_schema_once_and_writes_redacted_audit(tmp_path: Path):
    provider = SequenceProvider(
        [
            {"question_ids": ["invented"], "rationale": "bad"},
            {"question_ids": ["known"], "rationale": "fixed"},
        ]
    )
    activity_events = []

    @contextmanager
    def activity(task, attempt):
        activity_events.append(("start", task, attempt))
        yield
        activity_events.append(("end", task, attempt))

    service = LLMService(provider, store(tmp_path), max_attempts=2, activity=activity)
    result = service.generate(
        "question_planner",
        {
            "request": "private /Users/example/data.csv",
            "credentials": "api_key=must-not-be-stored",
        },
        QuestionSelection,
        allowed_ids={"known"},
    )
    assert result.question_ids == ["known"]
    assert provider.calls == 2
    assert activity_events == [
        ("start", "question_planner", 1),
        ("end", "question_planner", 1),
        ("start", "question_planner", 2),
        ("end", "question_planner", 2),
    ]
    audit = json.loads(Path(service.last_audit_path).read_text())
    assert audit["input_categories"] == ["credentials", "request"]
    assert "must-not-be-stored" not in json.dumps(audit)
    assert "must-not-be-stored" not in json.dumps(provider.payloads)
    assert "[PATH]" in provider.payloads[0]["request"]
    assert audit["attempts"][0]["status"] == "schema_error"


def test_reporter_digits_fail_then_require_reselection(tmp_path: Path):
    invalid = {
        "summary": [{"text": "三件ではなく3件", "evidence_ids": ["e1"]}],
        "interpretations": [],
        "limitations": [],
        "next_actions": [],
    }
    service = LLMService(SequenceProvider([invalid, invalid]), store(tmp_path))
    with pytest.raises(ProviderReselectionRequired):
        service.generate("reporter", {"evidence": []}, ReportNarrative, allowed_evidence={"e1"})


def test_provider_failure_does_not_fall_back(tmp_path: Path):
    service = LLMService(
        SequenceProvider([ProviderError("usage limit")]), store(tmp_path), max_attempts=2
    )
    with pytest.raises(ProviderReselectionRequired, match="usage limit"):
        service.generate("question_planner", {}, QuestionSelection, allowed_ids=set())


def test_preferences_version_consent_and_non_secret_values(tmp_path: Path):
    preferences = Preferences(tmp_path / "preferences.json")
    assert not preferences.has_consent("openrouter", "1")
    preferences.grant_consent("openrouter", "1")
    preferences.remember_selection("openrouter", "mock/model")
    assert preferences.has_consent("openrouter", "1")
    assert not preferences.has_consent("openrouter", "2")
    saved = preferences.load()
    assert saved["last_models"]["openrouter"] == "mock/model"
    assert not any("token" in key or "api_key" in key for key in saved)

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from data_agent.llm.base import InvalidStructuredResponse, LLMProvider, ProviderError
from data_agent.llm.schemas import PlanProposal, QuestionSelection, ReportNarrative
from data_agent.storage import RunStore

SchemaT = TypeVar("SchemaT", bound=BaseModel)

TASK_INSTRUCTIONS = {
    "question_planner": (
        "候補question_idsだけを優先順で返す。IDを創作せず、必須質問を削除しない。"
    ),
    "analysis_planner": (
        "許可されたtool_idsだけを、分析目的に有用な順で返す。コードや新規ツールを生成しない。"
    ),
    "reflector": "失敗を列挙された分類と再開先だけで評価し、短い助言を返す。",
    "reporter": (
        "検証済みevidenceだけを根拠に定性的な日本語を書く。textには数字を一切書かず、"
        "各項目にevidence_idsを付ける。"
    ),
}


class ProviderReselectionRequired(RuntimeError):
    def __init__(self, task: str, reason: str):
        super().__init__(reason)
        self.task = task
        self.reason = reason


class LLMService:
    def __init__(self, provider: LLMProvider, store: RunStore, max_attempts: int = 2):
        self.provider = provider
        self.store = store
        self.max_attempts = max(1, min(max_attempts, 2))
        self.last_audit_path: str | None = None

    def generate(
        self,
        task: str,
        payload: dict[str, Any],
        schema: type[SchemaT],
        *,
        allowed_evidence: set[str] | None = None,
        allowed_ids: set[str] | None = None,
    ) -> SchemaT:
        payload = self._sanitize_payload(payload)
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        started = time.perf_counter()
        attempts: list[dict[str, Any]] = []
        repair_error: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                generated = self.provider.generate_structured(
                    task=task,
                    instructions=TASK_INSTRUCTIONS[task],
                    payload=payload,
                    schema=schema,
                    repair_error=repair_error,
                )
                validated = schema.model_validate(generated.value)
                self._validate_semantics(validated, allowed_evidence, allowed_ids)
                attempts.append({"attempt": attempt, "status": "completed"})
                path = self._write_audit(
                    task,
                    canonical,
                    attempts,
                    validated.model_dump(mode="json"),
                    generated.usage,
                    started,
                )
                self.last_audit_path = str(path.resolve())
                return validated
            except (ValidationError, InvalidStructuredResponse, ValueError) as exc:
                repair_error = str(exc)
                attempts.append({"attempt": attempt, "status": "schema_error", "error": str(exc)})
                if attempt < self.max_attempts:
                    continue
                path = self._write_audit(task, canonical, attempts, None, {}, started)
                self.last_audit_path = str(path.resolve())
                reason = f"構造化応答の検証に失敗しました: {exc}"
                raise ProviderReselectionRequired(task, reason) from exc
            except ProviderError as exc:
                attempts.append({"attempt": attempt, "status": "provider_error", "error": str(exc)})
                path = self._write_audit(task, canonical, attempts, None, {}, started)
                self.last_audit_path = str(path.resolve())
                raise ProviderReselectionRequired(task, str(exc)) from exc
        raise AssertionError("unreachable")

    @classmethod
    def _sanitize_payload(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                cls._redact_text(str(key)): cls._sanitize_payload(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._sanitize_payload(item) for item in value]
        if isinstance(value, tuple):
            return [cls._sanitize_payload(item) for item in value]
        if isinstance(value, str):
            return cls._redact_text(value)
        return value

    @staticmethod
    def _redact_text(value: str) -> str:
        value = re.sub(
            r"(?i)\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S+",
            "[SECRET]",
            value,
        )
        value = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_.-]{20,})", "[SECRET]", value)
        value = re.sub(r"(?:[A-Za-z]:)?[/\\][^\s]+", "[PATH]", value)
        return re.sub(r"\b[^\s]+\.(?:csv|xlsx)\b", "[DATASET]", value, flags=re.IGNORECASE)

    @staticmethod
    def _validate_semantics(
        value: BaseModel,
        allowed_evidence: set[str] | None,
        allowed_ids: set[str] | None,
    ) -> None:
        if isinstance(value, QuestionSelection):
            identifiers = value.question_ids
        elif isinstance(value, PlanProposal):
            identifiers = value.tool_ids
        else:
            identifiers = []
        if allowed_ids is not None and (
            not set(identifiers) <= allowed_ids or len(identifiers) != len(set(identifiers))
        ):
            raise ValueError("許可リスト外または重複したIDが返されました")
        if not isinstance(value, ReportNarrative):
            return
        for section in (
            value.summary,
            value.interpretations,
            value.limitations,
            value.next_actions,
        ):
            for item in section:
                if re.search(r"[0-9０-９]", item.text):
                    raise ValueError("Reporterの定性文に数字を含めることはできません")
                if allowed_evidence is not None and not set(item.evidence_ids) <= allowed_evidence:
                    raise ValueError("Reporterが未知のevidence_idを参照しました")

    def _write_audit(
        self,
        task: str,
        canonical_payload: str,
        attempts: list[dict[str, Any]],
        output: dict[str, Any] | None,
        usage: dict[str, Any],
        started: float,
    ):
        return self.store.write_llm_call(
            task,
            {
                "provider": self.provider.name,
                "model": self.provider.model,
                "task": task,
                "template_version": "1",
                "input_categories": sorted(json.loads(canonical_payload).keys()),
                "input_sha256": hashlib.sha256(canonical_payload.encode()).hexdigest(),
                "structured_output": output,
                "attempts": attempts,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "usage": usage,
            },
        )

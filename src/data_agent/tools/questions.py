from __future__ import annotations

from collections.abc import Callable
from typing import Any

from data_agent.schemas import Answer, Question, QuestionKind

QUESTION_BANK = [
    Question(
        id="documentation_mismatch_ack",
        category="goal",
        text=(
            "README等の列定義と実データに矛盾があります。実データの列を正として分析を続行しますか？"
        ),
        kind=QuestionKind.YES_NO,
        depends_on={"_documentation_mismatch_detected": [True]},
        completion_key="documentation_mismatch_ack",
    ),
    Question(
        id="analysis_scope",
        category="scope",
        text="今回の分析範囲を選択してください",
        kind=QuestionKind.NUMBERED_CHOICE,
        choices=["傾向把握・記述統計", "データ品質確認", "因果効果の推定", "傾向把握と因果効果"],
        completion_key="analysis_scope",
    ),
    Question(
        id="target_column",
        category="goal",
        text="主要な結果指標（目的変数）の列名を入力してください。不明なら unknown",
        kind=QuestionKind.FREE_TEXT,
        completion_key="target_column",
    ),
    Question(
        id="drop_duplicates",
        category="preprocessing",
        text="完全重複行を前処理済みデータから除外しますか？",
        kind=QuestionKind.YES_NO,
        completion_key="drop_duplicates",
    ),
    Question(
        id="treatment_column",
        category="causal",
        text="処置群・対照群を表す列名を入力してください",
        kind=QuestionKind.FREE_TEXT,
        depends_on={"analysis_scope": ["因果効果の推定", "傾向把握と因果効果"]},
        completion_key="treatment_column",
    ),
    Question(
        id="treatment_value",
        category="causal",
        text="処置群を表す値を入力してください（例: 1, treated）",
        kind=QuestionKind.FREE_TEXT,
        depends_on={"analysis_scope": ["因果効果の推定", "傾向把握と因果効果"]},
        completion_key="treatment_value",
    ),
    Question(
        id="intervention_time",
        category="causal",
        text="介入時点がある場合は列名と時点を入力してください。横断データなら none",
        kind=QuestionKind.FREE_TEXT,
        depends_on={"analysis_scope": ["因果効果の推定", "傾向把握と因果効果"]},
        completion_key="intervention_time",
    ),
    Question(
        id="confounders",
        category="causal",
        text=(
            "処置と結果の両方に影響する交絡候補の列名をカンマ区切りで"
            "入力してください。なし/不明なら unknown"
        ),
        kind=QuestionKind.FREE_TEXT,
        depends_on={"analysis_scope": ["因果効果の推定", "傾向把握と因果効果"]},
        completion_key="confounders",
    ),
    Question(
        id="identification_assumption",
        category="causal",
        text="観測済み交絡で条件付ければ交換可能（未観測交絡なし）と仮定できますか？",
        kind=QuestionKind.YES_NO,
        depends_on={"analysis_scope": ["因果効果の推定", "傾向把握と因果効果"]},
        completion_key="identification_assumption",
    ),
]


def _answer_value(answers: dict[str, dict[str, Any]], question_id: str) -> Any:
    return answers.get(question_id, {}).get("value")


def is_applicable(question: Question, answers: dict[str, dict[str, Any]]) -> bool:
    return all(
        _answer_value(answers, key) in allowed for key, allowed in question.depends_on.items()
    )


def select_questions(answers: dict[str, dict[str, Any]], max_count: int = 4) -> list[Question]:
    pending = [
        question
        for question in QUESTION_BANK
        if question.id not in answers and is_applicable(question, answers)
    ]
    return pending[:max_count]


def parse_answer(
    question: Question,
    raw: str,
    existing_notes: list[str] | None = None,
) -> tuple[Answer | None, bool]:
    """Return (answer, needs_note). A raw `0` reserves the next input for notes."""
    raw = raw.strip()
    notes = list(existing_notes or [])
    if question.kind in {QuestionKind.YES_NO, QuestionKind.NUMBERED_CHOICE} and raw == "0":
        return None, True
    if question.kind == QuestionKind.YES_NO:
        normalized = raw.lower()
        if normalized not in {"y", "n"}:
            raise ValueError("y または n を入力してください（0は補足入力）")
        return (
            Answer(
                question_id=question.id,
                choice=normalized,
                value=normalized == "y",
                notes=notes,
            ),
            False,
        )
    if question.kind == QuestionKind.NUMBERED_CHOICE:
        try:
            index = int(raw)
        except ValueError as exc:
            raise ValueError("選択肢の番号を入力してください（0は補足入力）") from exc
        if not 1 <= index <= len(question.choices):
            raise ValueError(f"1〜{len(question.choices)}の番号を入力してください")
        return (
            Answer(
                question_id=question.id,
                choice=raw,
                value=question.choices[index - 1],
                notes=notes,
            ),
            False,
        )
    value: Any = raw or "unknown"
    return Answer(question_id=question.id, choice=value, value=value, notes=notes), False


def ask_interactively(
    question: Question,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> Answer:
    notes: list[str] = []
    while True:
        output_fn(f"\n{question.text}")
        if question.kind == QuestionKind.YES_NO:
            output_fn("  y. はい\n  n. いいえ\n  0. 補足情報を入力")
        elif question.kind == QuestionKind.NUMBERED_CHOICE:
            for number, choice in enumerate(question.choices, start=1):
                output_fn(f"  {number}. {choice}")
            output_fn("  0. 補足情報を入力")
        try:
            answer, needs_note = parse_answer(question, input_fn("> "), notes)
        except ValueError as exc:
            output_fn(f"入力エラー: {exc}")
            continue
        if needs_note:
            note = input_fn("補足: ").strip()
            if note:
                notes.append(note)
            output_fn("補足を保存しました。元の質問に回答してください。")
            continue
        assert answer is not None
        return answer

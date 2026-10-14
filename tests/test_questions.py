import pytest

from data_agent.schemas import Question, QuestionKind
from data_agent.tools.questions import parse_answer, select_questions


def test_numbered_choice_uses_one_based_index_and_zero_for_notes():
    question = Question(
        id="scope",
        category="scope",
        text="scope",
        kind=QuestionKind.NUMBERED_CHOICE,
        choices=["a", "b", "c", "d"],
        completion_key="scope",
    )
    answer, needs_note = parse_answer(question, "0")
    assert answer is None
    assert needs_note is True
    answer, needs_note = parse_answer(question, "3", ["context"])
    assert needs_note is False
    assert answer.value == "c"
    assert answer.notes == ["context"]
    with pytest.raises(ValueError):
        parse_answer(question, "5")


def test_analysis_scope_accepts_multiple_numbered_choices():
    question = next(
        question for question in select_questions({}, 4) if question.id == "analysis_scope"
    )
    assert question.kind == QuestionKind.MULTI_CHOICE
    answer, needs_note = parse_answer(question, "1,3", ["both are required"])
    assert needs_note is False
    assert answer.choice == "1,3"
    assert answer.value == ["傾向把握・記述統計", "因果効果の推定"]
    assert answer.notes == ["both are required"]
    assert parse_answer(question, "0")[1] is True
    with pytest.raises(ValueError):
        parse_answer(question, "1,4")


def test_yes_no_preserves_note_and_returns_to_choice():
    questions = select_questions({}, 4)
    yes_no = next(question for question in questions if question.id == "drop_duplicates")
    assert parse_answer(yes_no, "0")[1] is True
    answer, _ = parse_answer(yes_no, "y", ["keep an audit trail"])
    assert answer.value is True
    assert answer.notes == ["keep an audit trail"]


def test_causal_questions_are_conditional():
    trend_answers = {
        "analysis_scope": {"value": "傾向把握・記述統計"},
        "target_column": {"value": "sales"},
        "drop_duplicates": {"value": True},
    }
    assert select_questions(trend_answers) == []
    causal_answers = {
        **trend_answers,
        "analysis_scope": {"value": ["傾向把握・記述統計", "因果効果の推定"]},
    }
    assert any(question.category == "causal" for question in select_questions(causal_answers))

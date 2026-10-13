from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from data_agent.config import Settings  # noqa: E402
from data_agent.pipeline import Pipeline  # noqa: E402
from data_agent.schemas import Question, QuestionKind  # noqa: E402
from data_agent.storage import RunStore  # noqa: E402

st.set_page_config(page_title="Adaptive Data Analysis Agent", page_icon="📊", layout="wide")
settings = Settings.load(PROJECT_ROOT / "conf/config.yaml")


def load_pipeline(run_id: str) -> Pipeline:
    return Pipeline.resume(run_id, workspace=PROJECT_ROOT, settings=settings)


def events(store: RunStore) -> list[dict[str, Any]]:
    if not store.db_path.exists():
        return []
    with sqlite3.connect(store.db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY id", (store.run_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def phase_progress(event_rows: list[dict[str, Any]], state: dict[str, Any]) -> int:
    node_to_phase = {
        "discover": "discovery",
        "plan": "planning",
        "preprocess": "preprocessing",
        "validate_data": "preprocessing",
        "eda": "eda",
        "validate_results": "eda",
        "causal_specialist": "specialist",
        "validate_specialist": "specialist",
        "report": "reporting",
    }
    completed = {
        node_to_phase[row["node"]]
        for row in event_rows
        if row["event"] == "end" and row["node"] in node_to_phase
    }
    if state.get("answers") and state.get("status") != "awaiting_questions":
        completed.add("questions")
    return min(sum(settings.phase_weights.get(phase, 0) for phase in completed), 100)


def active_agents(event_rows: list[dict[str, Any]]) -> tuple[int, list[str]]:
    active: set[str] = set()
    for row in event_rows:
        if row["event"] == "start":
            active.add(row["node"])
        elif row["event"] in {"end", "failure"}:
            active.discard(row["node"])
    return len(active), sorted(active)


def render_questions(pipeline: Pipeline) -> None:
    questions = [Question.model_validate(item) for item in pipeline.state["pending_questions"]]
    with st.form("questions"):
        values: dict[str, Any] = {}
        notes: dict[str, str] = {}
        for question in questions:
            st.markdown(f"**{question.text}**")
            if question.kind == QuestionKind.YES_NO:
                values[question.id] = st.radio(
                    "回答", ["y", "n"], horizontal=True, key=f"q-{question.id}"
                )
                notes[question.id] = st.text_input("0. 補足情報（任意）", key=f"note-{question.id}")
            elif question.kind == QuestionKind.NUMBERED_CHOICE:
                choices = [f"{index}. {label}" for index, label in enumerate(question.choices, 1)]
                values[question.id] = st.selectbox("番号で選択", choices, key=f"q-{question.id}")
                notes[question.id] = st.text_input("0. 補足情報（任意）", key=f"note-{question.id}")
            else:
                values[question.id] = st.text_input("回答", key=f"q-{question.id}")
        submitted = st.form_submit_button("回答を保存", type="primary")
    if submitted:
        answers = {}
        for question in questions:
            raw = values[question.id]
            if question.kind == QuestionKind.YES_NO:
                value = raw == "y"
                choice = raw
            elif question.kind == QuestionKind.NUMBERED_CHOICE:
                number = int(raw.split(".", 1)[0])
                value = question.choices[number - 1]
                choice = str(number)
            else:
                value = raw.strip() or "unknown"
                choice = value
            answers[question.id] = {
                "question_id": question.id,
                "choice": choice,
                "value": value,
                "notes": [notes.get(question.id, "")] if notes.get(question.id) else [],
            }
        pipeline.submit_answers(answers)
        pipeline.run_until_pause()
        st.rerun()


def render_report(pipeline: Pipeline) -> None:
    report = pipeline.store.read_json("report/report_data.json", {})
    if not report:
        return
    descriptive = report["descriptive"]
    kpi1, kpi2, kpi3, kpi4 = st.columns(4)
    kpi1.metric("行数", f"{descriptive['row_count']:,}")
    kpi2.metric("列数", f"{descriptive['column_count']:,}")
    kpi3.metric("重複行", f"{descriptive['duplicate_rows']:,}")
    kpi4.metric(
        "検証PASS",
        f"{sum(v['passed'] for v in report['validations'])}/{len(report['validations'])}",
    )
    tabs = st.tabs(["最終レポート", "基本統計", "因果分析", "図表", "検証", "履歴"])
    with tabs[0]:
        st.markdown(pipeline.store.root.joinpath("report/report.md").read_text(encoding="utf-8"))
    with tabs[1]:
        st.dataframe(pd.DataFrame(descriptive.get("numeric_summary", [])), use_container_width=True)
        st.subheader("欠損率")
        st.dataframe(
            pd.DataFrame(
                [
                    {"column": key, "missing_rate": value}
                    for key, value in descriptive["missing_rate"].items()
                ]
            ),
            use_container_width=True,
        )
    with tabs[2]:
        causal = report["causal"]
        st.json(causal)
        if causal.get("status") == "estimated":
            st.warning("因果効果としての解釈には、表示された識別仮定の成立が必要です。")
    with tabs[3]:
        for figure in report["visualizations"]:
            st.caption(f"{figure['type']}: {figure.get('column', '')}")
            components.html(
                Path(figure["path"]).read_text(encoding="utf-8"), height=520, scrolling=True
            )
    with tabs[4]:
        st.dataframe(pd.DataFrame(report["validations"]), use_container_width=True)
    with tabs[5]:
        st.dataframe(pd.DataFrame(events(pipeline.store)), use_container_width=True)
        st.json({"plan_version": report["plan_version"], "provenance": report["provenance"]})


st.title("Adaptive Data Analysis Agent")
st.caption("検証済み成果物を唯一の表示元とするHuman-in-the-loop分析")

runs = RunStore.list_runs(PROJECT_ROOT / settings.runs_dir)
with st.sidebar:
    st.header("Run")
    run_ids = [row["run_id"] for row in runs]
    selected = st.selectbox("保存済みRun", ["新規"] + run_ids)
    if selected == "新規":
        request = st.text_area("分析依頼", "売上データの傾向を把握したい")
        allow_outside = st.checkbox("依頼文に記載したcwd外のパス参照を明示的に許可")
        if st.button("cwdを探索して開始", type="primary"):
            pipeline = Pipeline.create(
                request,
                workspace=PROJECT_ROOT,
                settings=settings,
                allow_outside_workspace=allow_outside,
            )
            pipeline.run_until_pause()
            st.session_state["run_id"] = pipeline.state["run_id"]
            st.rerun()
    else:
        st.session_state["run_id"] = selected

run_id = st.session_state.get("run_id")
if run_id:
    pipeline = load_pipeline(run_id)
    state = pipeline.state
    event_rows = events(pipeline.store)
    progress = phase_progress(event_rows, state)
    col1, col2, col3 = st.columns(3)
    active_count, active_nodes = active_agents(event_rows)
    col1.metric("状態", state["status"])
    col2.metric("推定進捗", f"{progress}%")
    col3.metric("稼働Agent", active_count)
    st.progress(progress / 100, text="フェーズ重みから算出した推定進捗")
    active_label = ", ".join(active_nodes) or "停止中"
    st.caption(f"Node: {active_label} / thread_id: {state['thread_id']}")

    if state["status"] == "awaiting_questions":
        st.subheader("質問")
        render_questions(pipeline)
    elif state["status"] == "awaiting_plan_approval":
        st.subheader(f"分析計画 v{state['plan_version']}")
        st.dataframe(pd.DataFrame(state["plan"]), use_container_width=True)
        feedback = st.text_area("差し戻す場合の修正内容")
        approve, reject = st.columns(2)
        if approve.button("承認して実行", type="primary"):
            pipeline.approve_plan(True)
            pipeline.run_until_pause()
            st.rerun()
        if reject.button("差し戻し"):
            pipeline.approve_plan(False, feedback)
            pipeline.run_until_pause()
            st.rerun()
    elif state["status"] == "awaiting_intermediate_approval":
        st.subheader("中間結果")
        descriptive = pipeline.store.read_json("analysis/descriptive.json")
        st.json(
            {
                key: descriptive[key]
                for key in ("row_count", "column_count", "duplicate_rows", "missing")
            }
        )
        feedback = st.text_area("差し戻す場合の理由", key="intermediate-feedback")
        approve, reject = st.columns(2)
        if approve.button("承認して専門分析へ", type="primary"):
            pipeline.approve_intermediate(True)
            pipeline.run_until_pause()
            st.rerun()
        if reject.button("計画へ差し戻し"):
            pipeline.approve_intermediate(False, feedback)
            pipeline.run_until_pause()
            st.rerun()
    elif state["status"] == "human_review":
        st.error("再試行上限に達しました。Human Reviewが必要です。")
        st.json(state.get("error", {}))
    elif state["status"] == "complete":
        render_report(pipeline)
else:
    st.info("サイドバーから新規Runを開始するか、保存済みRunを選択してください。")

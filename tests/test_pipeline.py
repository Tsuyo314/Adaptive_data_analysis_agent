import json
from pathlib import Path

import pandas as pd

from data_agent.config import Settings
from data_agent.pipeline import Pipeline
from data_agent.tools.analysis import preprocess


def test_pipeline_pauses_for_approvals_and_report_matches(tmp_path: Path):
    source = tmp_path / "data.csv"
    source.write_text("group,value\nA,1\nA,2\nB,3\nB,4\nB,4\n", encoding="utf-8")
    original = source.read_bytes()
    settings = Settings(runs_dir="runs", max_retries=1)
    pipeline = Pipeline.create(
        "傾向を分析", workspace=tmp_path, settings=settings, run_id="test-run"
    )
    state = pipeline.run_until_pause()
    assert state["status"] == "awaiting_questions"
    pipeline.submit_answers(
        {
            "analysis_scope": {
                "question_id": "analysis_scope",
                "choice": "1,2",
                "value": ["傾向把握・記述統計", "データ品質確認"],
                "notes": [],
            },
            "target_column": {
                "question_id": "target_column",
                "choice": "value",
                "value": "value",
                "notes": [],
            },
            "drop_duplicates": {
                "question_id": "drop_duplicates",
                "choice": "y",
                "value": True,
                "notes": [],
            },
        }
    )
    state = pipeline.run_until_pause()
    assert state["status"] == "awaiting_plan_approval"
    assert not (pipeline.store.root / "analysis/descriptive.json").exists()
    pipeline.approve_plan(True)
    state = pipeline.run_until_pause()
    assert state["status"] == "awaiting_intermediate_approval"
    pipeline.approve_intermediate(True)
    state = pipeline.run_until_pause()
    assert state["status"] == "complete"
    assert source.read_bytes() == original
    report = json.loads((pipeline.store.root / "report/report_data.json").read_text())
    descriptive = json.loads((pipeline.store.root / "analysis/descriptive.json").read_text())
    assert report["descriptive"]["row_count"] == descriptive["row_count"] == 4
    report_markdown = (pipeline.store.root / "report/report.md").read_text()
    assert "分析対象行数: 4" in report_markdown
    assert "## 基礎統計量" in report_markdown
    assert (
        "| 列 | 有効件数 | 欠損数 | 平均 | 標準偏差 | 最小 | 25% | 中央値 | 75% | 最大 |"
        in report_markdown
    )
    assert "| value | 4 | 0 | 2.5 |" in report_markdown
    assert "### カテゴリ列" in report_markdown
    assert "| group | 4 | 0 | 2 |" in report_markdown
    resumed = Pipeline.resume("test-run", workspace=tmp_path, settings=settings)
    assert resumed.state["thread_id"] == state["thread_id"]


def test_date_conversion_error_recovers_locally(tmp_path: Path):
    frame = pd.DataFrame({"event_date": ["2026-01-01", "not-a-date", "also-bad"]})
    processed, log = preprocess(frame, tmp_path / "processed.parquet", False)
    conversion = log["date_conversions"][0]
    assert conversion["status"] == "skipped_parse_errors"
    assert processed["event_date"].tolist() == frame["event_date"].tolist()

from pathlib import Path

from data_agent.config import Settings
from data_agent.pipeline import Pipeline
from data_agent.tools.discovery import (
    discover_workspace,
    inspect_dataset,
    summarize_supporting_file,
)


def test_discovery_and_lightweight_inspection(tmp_path: Path):
    (tmp_path / "data.csv").write_text("x,y\n1,2\n3,\n", encoding="utf-8")
    generated = tmp_path / ".data-agent" / "runs" / "old"
    generated.mkdir(parents=True)
    (generated / "generated.csv").write_text("secret\n1\n", encoding="utf-8")
    code = tmp_path / "helper.py"
    code.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
    manifest = discover_workspace(tmp_path, Settings())
    assert [item["relative_path"] for item in manifest["data_files"]] == ["data.csv"]
    profile = inspect_dataset(tmp_path / "data.csv", Settings(metadata_sample_rows=1))
    assert profile["sample_rows"] == 1
    assert "先頭1行" in profile["inspection_scope"]
    summary = summarize_supporting_file(code, Settings())
    assert summary["executed"] is False
    assert "must not execute" in summary["summary"]


def test_readme_column_mismatch_is_detected():
    support = [
        {
            "path": "/workspace/README.md",
            "summary": "- `known`: available\n- `missing`: documented only",
        }
    ]
    mismatches = Pipeline._documentation_mismatches(support, ["known"])
    assert mismatches == ["README.md に定義され実データにない列: missing"]

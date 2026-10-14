from pathlib import Path

from data_agent.config import Settings
from data_agent.pipeline import Pipeline
from data_agent.workspace import list_projects, list_workspace_runs, project_path


def test_new_runs_are_hidden_from_analysis_inputs(tmp_path: Path):
    pipeline = Pipeline.create("傾向を分析", workspace=tmp_path, run_id="new-layout")

    assert pipeline.store.root == (tmp_path / ".data-agent/runs/new-layout").resolve()


def test_legacy_run_can_be_listed_and_resumed(tmp_path: Path):
    legacy_settings = Settings(runs_dir="runs")
    original = Pipeline.create(
        "傾向を分析",
        workspace=tmp_path,
        settings=legacy_settings,
        run_id="legacy-run",
    )

    runs = list_workspace_runs(tmp_path, Settings())
    resumed = Pipeline.resume("legacy-run", workspace=tmp_path, settings=Settings())

    assert [row["run_id"] for row in runs] == ["legacy-run"]
    assert resumed.state["thread_id"] == original.state["thread_id"]
    assert resumed.store.runs_root == (tmp_path / "runs").resolve()


def test_bare_project_name_is_resolved_below_analysis_root(tmp_path: Path):
    root = tmp_path / "data-analysis"
    beta = root / "beta"
    alpha = root / "alpha"
    beta.mkdir(parents=True)
    alpha.mkdir()

    assert project_path(Path("alpha"), root) == alpha.resolve()
    assert list_projects(root) == [alpha, beta]

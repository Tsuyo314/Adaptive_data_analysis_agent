from io import StringIO

from rich.console import Console
from typer.testing import CliRunner

import data_agent.main as main
from data_agent.main import app

runner = CliRunner()


def test_init_creates_separated_workspace(tmp_path):
    workspace = tmp_path / "customer-analysis"

    result = runner.invoke(app, ["init", str(workspace)])

    assert result.exit_code == 0
    assert (workspace / "data").is_dir()
    assert (workspace / "references").is_dir()
    assert (workspace / ".data-agent/runs").is_dir()
    assert (workspace / ".data-agent/.gitignore").read_text(encoding="utf-8") == (
        "*\n!.gitignore\n"
    )
    assert "分析workspaceを初期化しました" in result.output


def test_init_with_project_name_uses_default_analysis_root(tmp_path, monkeypatch):
    analysis_root = tmp_path / "data-analysis"
    monkeypatch.setenv("DATA_AGENT_ANALYSIS_ROOT", str(analysis_root))

    result = runner.invoke(app, ["init", "sales-project"])

    assert result.exit_code == 0
    assert (analysis_root / "sales-project/data").is_dir()
    assert (analysis_root / "sales-project/references").is_dir()


def test_run_auto_selects_the_only_default_project(tmp_path, monkeypatch):
    analysis_root = tmp_path / "data-analysis"
    project = analysis_root / "sales-project"
    project.mkdir(parents=True)
    (project / "sales.csv").write_text("value\n1\n", encoding="utf-8")
    monkeypatch.setenv("DATA_AGENT_ANALYSIS_ROOT", str(analysis_root))
    observed = {}

    def fake_drive(pipeline):
        observed["workspace"] = pipeline.state["working_directory"]

    monkeypatch.setattr("data_agent.main._drive", fake_drive)
    result = runner.invoke(
        app,
        ["run", "傾向を分析", "--provider", "deterministic"],
    )

    assert result.exit_code == 0
    assert observed["workspace"] == str(project.resolve())
    assert "分析プロジェクト: sales-project" in result.output


def test_run_prompts_for_project_when_multiple_exist(tmp_path, monkeypatch):
    analysis_root = tmp_path / "data-analysis"
    alpha = analysis_root / "alpha"
    beta = analysis_root / "beta"
    alpha.mkdir(parents=True)
    beta.mkdir()
    (alpha / "data.csv").write_text("value\n1\n", encoding="utf-8")
    (beta / "data.csv").write_text("value\n2\n", encoding="utf-8")
    monkeypatch.setenv("DATA_AGENT_ANALYSIS_ROOT", str(analysis_root))
    observed = {}

    def fake_drive(pipeline):
        observed["workspace"] = pipeline.state["working_directory"]

    monkeypatch.setattr("data_agent.main._drive", fake_drive)
    result = runner.invoke(
        app,
        ["run", "傾向を分析", "--provider", "deterministic"],
        input="2\n",
    )

    assert result.exit_code == 0
    assert "1. alpha" in result.output
    assert "2. beta" in result.output
    assert observed["workspace"] == str(beta.resolve())


def test_run_prompts_for_request_when_argument_is_omitted(tmp_path):
    result = runner.invoke(
        app,
        ["run", "--workspace", str(tmp_path)],
        input="\n",
    )
    assert result.exit_code == 1
    assert "prompt>" in result.output
    assert "分析依頼が空です" in result.output


def test_run_accepts_deterministic_provider_flag(tmp_path, monkeypatch):
    (tmp_path / "data.csv").write_text("value\n1\n", encoding="utf-8")
    observed = {}

    def fake_drive(pipeline):
        observed["provider"] = pipeline.state["llm_provider"]

    monkeypatch.setattr("data_agent.main._drive", fake_drive)
    result = runner.invoke(
        app,
        [
            "run",
            "傾向を分析",
            "--workspace",
            str(tmp_path),
            "--provider",
            "deterministic",
        ],
    )
    assert result.exit_code == 0
    assert observed["provider"] == "deterministic"


def test_llm_activity_has_non_terminal_fallback_and_completion(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(
        main,
        "console",
        Console(file=output, force_terminal=False, color_system=None),
    )

    with main._llm_activity("reporter", 1):
        pass

    rendered = output.getvalue()
    assert "↻ AIがレポート要約の作成を実行しています" in rendered
    assert "✓ AIによるレポート要約の作成が完了しました" in rendered

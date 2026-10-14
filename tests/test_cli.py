from typer.testing import CliRunner

from data_agent.main import app

runner = CliRunner()


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

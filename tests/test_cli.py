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

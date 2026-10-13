from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from data_agent.config import Settings
from data_agent.graph import build_graph
from data_agent.pipeline import Pipeline
from data_agent.schemas import Question
from data_agent.storage import RunStore
from data_agent.tools.discovery import resolve_explicit_dataset
from data_agent.tools.questions import ask_interactively

app = typer.Typer(help="Adaptive Data Analysis Agent")
console = Console()


def _show_discovery(state: dict[str, Any]) -> None:
    profile = state.get("dataset_profile", {})
    console.print(
        Panel(
            f"[bold]主データ[/bold] {state.get('dataset_path')}\n"
            f"形式: {profile.get('format')} / 列数: {len(profile.get('columns', []))} / "
            f"初期確認: {profile.get('inspection_scope')}\n"
            f"関連資料: {len(state.get('supporting_file_paths', []))}件\n"
            f"資料との矛盾: {len(profile.get('document_mismatches', []))}件",
            title="Workspace inspection",
        )
    )
    table = Table(title="分析可能性")
    table.add_column("候補")
    table.add_column("評価")
    table.add_column("不足条件")
    for option in state.get("analysis_options", []):
        table.add_row(
            option["name"], option["feasibility"], ", ".join(option["missing_conditions"]) or "なし"
        )
    console.print(table)


def _show_plan(state: dict[str, Any]) -> None:
    table = Table(title=f"分析計画 v{state['plan_version']}")
    table.add_column("#")
    table.add_column("処理")
    table.add_column("ツール")
    table.add_column("内容")
    for index, step in enumerate(state.get("plan", []), 1):
        table.add_row(str(index), step["title"], step["tool"], step["description"])
    console.print(table)


def _show_intermediate(pipeline: Pipeline) -> None:
    descriptive = pipeline.store.read_json("analysis/descriptive.json", {})
    validations = pipeline.store.read_json("validation/statistical_validation.json", [])
    console.print(
        Panel(
            f"行数: {descriptive.get('row_count', 0):,}\n"
            f"列数: {descriptive.get('column_count', 0):,}\n"
            f"重複: {descriptive.get('duplicate_rows', 0):,}\n"
            f"Validator: {sum(item['passed'] for item in validations)}/{len(validations)} PASS",
            title="中間結果",
        )
    )


def _drive(pipeline: Pipeline) -> None:
    previous_status = ""
    while True:
        state = pipeline.run_until_pause()
        status = state["status"]
        if status == "awaiting_questions":
            if previous_status != status:
                _show_discovery(state)
            answers = {}
            for raw_question in state.get("pending_questions", []):
                question = Question.model_validate(raw_question)
                answer = ask_interactively(
                    question, input_fn=lambda prompt: console.input(prompt), output_fn=console.print
                )
                answers[question.id] = answer.model_dump(mode="json")
            pipeline.submit_answers(answers)
        elif status == "awaiting_plan_approval":
            _show_plan(state)
            approved = typer.confirm("この計画を承認しますか？", default=True)
            feedback = "" if approved else console.input("修正内容: ")
            pipeline.approve_plan(approved, feedback)
        elif status == "awaiting_intermediate_approval":
            _show_intermediate(pipeline)
            approved = typer.confirm("中間結果を承認し、専門分析へ進みますか？", default=True)
            feedback = "" if approved else console.input("差し戻し理由: ")
            pipeline.approve_intermediate(approved, feedback)
        elif status == "complete":
            console.print(
                Panel(f"Run: {state['run_id']}\nReport: {state['report_path']}", title="完了")
            )
            return
        elif status == "human_review":
            error = state.get("error", {})
            console.print(
                Panel(
                    f"Node: {error.get('node')}\n分類: {error.get('classification')}\n"
                    f"理由: {error.get('message')}",
                    title="Human Reviewが必要です",
                    border_style="red",
                )
            )
            raise typer.Exit(code=2)
        else:
            console.print(f"停止状態: {status}")
            return
        previous_status = status


@app.command()
def run(
    request: str | None = typer.Argument(None, help="分析依頼。省略時は起動後に入力"),
    workspace: Path = typer.Option(Path.cwd(), exists=True, file_okay=False, resolve_path=True),
) -> None:
    """新しい分析Runを開始します。"""
    if request is None:
        console.print(
            Panel(
                "分析したい内容を入力してください。\n例: sales.csv の売上傾向を把握したい",
                title="分析依頼",
            )
        )
        request = console.input("[bold cyan]prompt> [/bold cyan]").strip()
        if not request:
            console.print("[red]分析依頼が空です。処理を終了します。[/red]")
            raise typer.Exit(code=1)
    explicit = resolve_explicit_dataset(request, workspace)
    allow_outside = False
    if explicit:
        try:
            explicit.relative_to(workspace.resolve())
        except ValueError:
            allow_outside = typer.confirm(
                f"cwd外のファイルを参照します: {explicit}\n明示的に許可しますか？",
                default=False,
            )
            if not allow_outside:
                raise typer.Abort() from None
    pipeline = Pipeline.create(request, workspace=workspace, allow_outside_workspace=allow_outside)
    console.print(f"Run ID: [bold]{pipeline.state['run_id']}[/bold]")
    _drive(pipeline)


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="再開するrun_id"),
    workspace: Path = typer.Option(Path.cwd(), exists=True, file_okay=False, resolve_path=True),
) -> None:
    """同一thread_idのチェックポイントから再開します。"""
    _drive(Pipeline.resume(run_id, workspace=workspace))


@app.command("list-runs")
def list_runs(
    workspace: Path = typer.Option(Path.cwd(), exists=True, file_okay=False, resolve_path=True),
) -> None:
    """保存済みRunを一覧表示します。"""
    settings = Settings.load(workspace / "conf/config.yaml")
    table = Table("run_id", "status", "updated_at", "working_directory")
    for row in RunStore.list_runs(workspace / settings.runs_dir):
        table.add_row(row["run_id"], row["status"], row["updated_at"], row["working_directory"])
    console.print(table)


@app.command()
def graph(
    format: str = typer.Option("mermaid", help="mermaid または ascii"),
) -> None:
    """LangGraphの静的構造を表示します。"""
    names = [
        "discover",
        "plan",
        "preprocess",
        "validate_data",
        "eda",
        "validate_results",
        "specialist",
        "validate_specialist",
        "report",
    ]

    def no_op(state: dict[str, Any]) -> dict[str, Any]:
        return state

    compiled = build_graph({name: no_op for name in names})
    representation = compiled.get_graph()
    if format.lower() == "ascii":
        try:
            console.print(representation.draw_ascii())
        except Exception as exc:
            raise typer.BadParameter(f"ASCII生成に失敗しました: {exc}") from exc
    elif format.lower() == "mermaid":
        console.print(representation.draw_mermaid())
    else:
        raise typer.BadParameter("formatはmermaidまたはasciiです")


@app.command()
def status(
    run_id: str,
    workspace: Path = typer.Option(Path.cwd(), exists=True, file_okay=False, resolve_path=True),
) -> None:
    """Runの最新StateをJSON表示します。"""
    settings = Settings.load(workspace / "conf/config.yaml")
    store = RunStore(workspace / settings.runs_dir, run_id)
    console.print_json(json.dumps(store.load_state(), ensure_ascii=False))


if __name__ == "__main__":
    app()

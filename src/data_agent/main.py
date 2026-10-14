from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from data_agent.config import Settings
from data_agent.graph import build_graph
from data_agent.llm import LLMService, ModelInfo, ProviderError
from data_agent.llm.chatgpt_auth import ChatGPTAuthClient
from data_agent.llm.preferences import Preferences
from data_agent.llm.providers import ChatGPTProvider, OpenRouterProvider
from data_agent.pipeline import Pipeline
from data_agent.schemas import Question
from data_agent.storage import RunStore
from data_agent.tools.discovery import resolve_explicit_dataset
from data_agent.tools.questions import ask_interactively
from data_agent.workspace import (
    default_analysis_root,
    initialize_workspace,
    list_projects,
    list_workspace_runs,
    project_path,
    resolve_runs_root,
)

app = typer.Typer(help="Adaptive Data Analysis Agent")
console = Console()
PROVIDER_LABELS = {
    "chatgpt": "ChatGPT Plus/Pro（Continue with ChatGPT）",
    "openrouter": "OpenRouter API",
    "deterministic": "AIなし（決定論モード）",
}
LLM_TASK_LABELS = {
    "question_planner": "確認事項の整理",
    "analysis_planner": "分析計画の作成",
    "reflector": "エラーと再開方法の検討",
    "reporter": "レポート要約の作成",
}


@contextmanager
def _llm_activity(task: str, attempt: int):
    action = LLM_TASK_LABELS.get(task, "応答の生成")
    retry = "（応答を修復中）" if attempt > 1 else ""
    running_message = f"AIが{action}を実行しています{retry}..."
    started = time.monotonic()
    try:
        if console.is_terminal:
            with console.status(
                f"[bold cyan]{running_message}[/bold cyan]",
                spinner="arc",
                spinner_style="bold cyan",
                refresh_per_second=12.5,
            ):
                yield
        else:
            console.print(f"[cyan]↻ {running_message}[/cyan]")
            yield
    except Exception:
        elapsed = time.monotonic() - started
        console.print(f"[red]✗ AIによる{action}に失敗しました（{elapsed:.1f}秒）[/red]")
        raise
    else:
        elapsed = time.monotonic() - started
        console.print(f"[green]✓ AIによる{action}が完了しました（{elapsed:.1f}秒）[/green]")


def _choose_numbered(label: str, values: list[tuple[str, str]], default_id: str | None) -> str:
    console.print(f"\n[bold]{label}[/bold]")
    for index, (_, display) in enumerate(values, 1):
        suffix = " [dim](前回)[/dim]" if values[index - 1][0] == default_id else ""
        console.print(f"  {index}. {display}{suffix}")
    default_index = next(
        (index for index, (identifier, _) in enumerate(values, 1) if identifier == default_id), 1
    )
    while True:
        raw = console.input(f"> [{default_index}] ").strip() or str(default_index)
        if raw.isdigit() and 1 <= int(raw) <= len(values):
            return values[int(raw) - 1][0]
        console.print(f"[red]1〜{len(values)}の番号を入力してください。[/red]")


def _select_provider(requested: str | None, preferences: Preferences) -> str:
    if requested:
        if requested not in PROVIDER_LABELS:
            raise typer.BadParameter("providerはchatgpt、openrouter、deterministicのいずれかです")
        return requested
    previous, _ = preferences.last_selection()
    choices = [(key, label) for key, label in PROVIDER_LABELS.items()]
    return _choose_numbered("使用するAI", choices, previous)


def _select_workspace(workspace: Path | None, project: str | None) -> Path:
    if workspace and project:
        raise typer.BadParameter("--workspaceと--projectは同時に指定できません")
    if workspace:
        return workspace.resolve()

    analysis_root = default_analysis_root()
    if project:
        selected = project_path(Path(project), analysis_root)
        if not selected.is_dir():
            raise typer.BadParameter(
                f"分析プロジェクトが見つかりません: {selected}\n"
                f"先に data-agent init {project} を実行してください。"
            )
        return selected

    projects = list_projects(analysis_root)
    current = Path.cwd().resolve()
    for candidate in projects:
        if current == candidate or candidate in current.parents:
            return candidate
    if not projects:
        console.print(
            f"[red]分析プロジェクトがありません: {analysis_root}[/red]\n"
            "先に [bold]data-agent init <プロジェクト名>[/bold] を実行してください。"
        )
        raise typer.Exit(code=1)
    if len(projects) == 1:
        console.print(f"分析プロジェクト: [bold]{projects[0].name}[/bold]")
        return projects[0]
    selected = _choose_numbered(
        "分析プロジェクト",
        [(str(candidate), candidate.name) for candidate in projects],
        None,
    )
    return Path(selected)


def _ensure_consent(provider: str, settings: Settings, preferences: Preferences) -> None:
    if provider == "deterministic" or preferences.has_consent(
        provider, settings.llm.consent_policy_version
    ):
        return
    console.print(
        Panel(
            "外部AIへ送信するのは、分析依頼（ファイルパスは除去）、列名・型・行数・欠損集計、"
            "回答、検証済み集計結果です。生データ、サンプル行、関連資料、ローカルパス、"
            "認証情報は送信しません。",
            title=f"{PROVIDER_LABELS[provider]}へのデータ送信同意",
        )
    )
    if not typer.confirm("このポリシーに同意しますか？", default=False):
        raise typer.Abort()
    preferences.grant_consent(provider, settings.llm.consent_policy_version)


def _select_model(
    models: list[ModelInfo], requested: str | None, provider: str, preferences: Preferences
) -> str:
    if not models:
        raise ProviderError("利用可能な構造化出力対応モデルがありません")
    model_ids = {item.id for item in models}
    if requested:
        if requested not in model_ids:
            raise ProviderError(f"選択したモデルは現在のアカウントで利用できません: {requested}")
        return requested
    _, previous_models = preferences.last_selection()
    values = [(item.id, f"{item.display_name} ({item.id})") for item in models]
    return _choose_numbered("モデル", values, previous_models.get(provider))


def _connect_provider(
    provider_name: str,
    requested_model: str | None,
    settings: Settings,
    preferences: Preferences,
):
    _ensure_consent(provider_name, settings, preferences)
    if provider_name == "deterministic":
        preferences.remember_selection(provider_name)
        return None, ""
    if provider_name == "chatgpt":
        auth = ChatGPTAuthClient(
            preferences=preferences,
            timeout=settings.llm.timeout_seconds,
            reauth_days=settings.llm.chatgpt_reauth_days,
        )
        tokens = auth.authenticate()
        provider = ChatGPTProvider(
            auth,
            base_url=settings.llm.openai_base_url,
            timeout=settings.llm.timeout_seconds,
        )
        source = "保存済み認証" if auth.reused_session else "ブラウザ認証"
        console.print(
            f"[green]Using ChatGPT plan[/green] {tokens.email or tokens.subject}（{source}）"
        )
        if auth.revocation_warning:
            console.print(f"[yellow]{auth.revocation_warning}[/yellow]")
        console.print("Manage usage: https://chatgpt.com/settings/usage")
    else:
        provider = OpenRouterProvider(
            base_url=settings.llm.openrouter_base_url,
            timeout=settings.llm.timeout_seconds,
        )
    model = _select_model(provider.list_models(), requested_model, provider_name, preferences)
    provider.model = model
    preferences.remember_selection(provider_name, model)
    return provider, model


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
        elif status == "awaiting_llm_provider":
            console.print(
                Panel(
                    f"Task: {state.get('pending_llm_operation', {}).get('task')}\n"
                    f"理由: {state.get('llm_reselection_reason', 'AI接続エラー')}",
                    title="AIプロバイダーの再選択が必要です",
                    border_style="yellow",
                )
            )
            preferences = Preferences()
            provider_name = _select_provider(None, preferences)
            try:
                provider, model = _connect_provider(
                    provider_name, None, pipeline.settings, preferences
                )
            except ProviderError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=2) from exc
            service = (
                LLMService(
                    provider,
                    pipeline.store,
                    pipeline.settings.llm.max_attempts,
                    activity=_llm_activity,
                )
                if provider
                else None
            )
            pipeline.select_llm_provider(provider_name, model, service)
        else:
            console.print(f"停止状態: {status}")
            return
        previous_status = status


@app.command("init")
def init_workspace(
    directory: Path = typer.Argument(
        ..., help="プロジェクト名、または明示的な分析workspaceのパス"
    ),
) -> None:
    """入力、関連資料、Run成果物を分離した分析workspaceを作成します。"""
    workspace = initialize_workspace(project_path(directory))
    console.print(
        Panel(
            f"[bold]{workspace}[/bold]\n"
            "├── data/             CSV・XLSX\n"
            "├── references/       README・仕様書など\n"
            "└── .data-agent/runs/ Run成果物（Git対象外）\n\n"
            "実行: data-agent run",
            title="分析workspaceを初期化しました",
        )
    )


@app.command()
def run(
    request: str | None = typer.Argument(None, help="分析依頼。省略時は起動後に入力"),
    workspace: Path | None = typer.Option(
        None, exists=True, file_okay=False, resolve_path=True, help="分析workspaceの明示パス"
    ),
    project: str | None = typer.Option(
        None, "--project", "-p", help="data-analysis配下のプロジェクト名"
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="chatgpt、openrouter、deterministic。省略時は対話選択"
    ),
    model: str | None = typer.Option(None, "--model", help="ライブ一覧にあるモデルID"),
) -> None:
    """新しい分析Runを開始します。"""
    workspace = _select_workspace(workspace, project)
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
                f"workspace外のファイルを参照します: {explicit}\n明示的に許可しますか？",
                default=False,
            )
            if not allow_outside:
                raise typer.Abort() from None
    settings = Settings.load(workspace / "conf/config.yaml")
    preferences = Preferences()
    provider_name = _select_provider(provider, preferences)
    try:
        connected_provider, selected_model = _connect_provider(
            provider_name, model, settings, preferences
        )
    except ProviderError as exc:
        console.print(f"[red]AI接続エラー: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    pipeline = Pipeline.create(
        request,
        workspace=workspace,
        settings=settings,
        allow_outside_workspace=allow_outside,
        llm_provider=provider_name,
        llm_model=selected_model,
    )
    if connected_provider:
        pipeline.llm_service = LLMService(
            connected_provider,
            pipeline.store,
            settings.llm.max_attempts,
            activity=_llm_activity,
        )
    console.print(f"Run ID: [bold]{pipeline.state['run_id']}[/bold]")
    _drive(pipeline)


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="再開するrun_id"),
    workspace: Path | None = typer.Option(
        None, exists=True, file_okay=False, resolve_path=True, help="分析workspaceの明示パス"
    ),
    project: str | None = typer.Option(
        None, "--project", "-p", help="data-analysis配下のプロジェクト名"
    ),
) -> None:
    """同一thread_idのチェックポイントから再開します。"""
    workspace = _select_workspace(workspace, project)
    pipeline = Pipeline.resume(run_id, workspace=workspace)
    provider_name = pipeline.state.get("llm_provider", "deterministic")
    model = pipeline.state.get("llm_model") or None
    if provider_name != "deterministic":
        try:
            connected, selected_model = _connect_provider(
                provider_name, model, pipeline.settings, Preferences()
            )
        except ProviderError as exc:
            console.print(f"[yellow]AI再接続エラー: {exc}[/yellow]")
            console.print("別のプロバイダーまたはAIなしを選択してください。")
            preferences = Preferences()
            provider_name = _select_provider(None, preferences)
            try:
                connected, selected_model = _connect_provider(
                    provider_name, None, pipeline.settings, preferences
                )
            except ProviderError as retry_exc:
                console.print(f"[red]AI再接続エラー: {retry_exc}[/red]")
                raise typer.Exit(code=2) from retry_exc
        pipeline.llm_service = (
            LLMService(
                connected,
                pipeline.store,
                pipeline.settings.llm.max_attempts,
                activity=_llm_activity,
            )
            if connected
            else None
        )
        pipeline.select_llm_provider(provider_name, selected_model, pipeline.llm_service)
    _drive(pipeline)


@app.command("list-runs")
def list_runs(
    workspace: Path | None = typer.Option(
        None, exists=True, file_okay=False, resolve_path=True, help="分析workspaceの明示パス"
    ),
    project: str | None = typer.Option(
        None, "--project", "-p", help="data-analysis配下のプロジェクト名"
    ),
) -> None:
    """保存済みRunを一覧表示します。"""
    workspace = _select_workspace(workspace, project)
    settings = Settings.load(workspace / "conf/config.yaml")
    table = Table("run_id", "status", "updated_at", "working_directory")
    for row in list_workspace_runs(workspace, settings):
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
    workspace: Path | None = typer.Option(
        None, exists=True, file_okay=False, resolve_path=True, help="分析workspaceの明示パス"
    ),
    project: str | None = typer.Option(
        None, "--project", "-p", help="data-analysis配下のプロジェクト名"
    ),
) -> None:
    """Runの最新StateをJSON表示します。"""
    workspace = _select_workspace(workspace, project)
    settings = Settings.load(workspace / "conf/config.yaml")
    store = RunStore(resolve_runs_root(workspace, settings, run_id), run_id)
    console.print_json(json.dumps(store.load_state(), ensure_ascii=False))


if __name__ == "__main__":
    app()

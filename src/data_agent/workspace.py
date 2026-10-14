from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from data_agent.config import Settings
from data_agent.storage import RunStore

LEGACY_RUNS_DIR = "runs"
WORKSPACE_DATA_DIR = "data"
WORKSPACE_REFERENCES_DIR = "references"


def default_analysis_root() -> Path:
    configured = os.getenv("DATA_AGENT_ANALYSIS_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    repository_root = Path(__file__).resolve().parents[2]
    if (repository_root / "pyproject.toml").is_file():
        return repository_root / "data-analysis"
    return Path.home() / "data-analysis"


def list_projects(analysis_root: Path | None = None) -> list[Path]:
    root = (analysis_root or default_analysis_root()).expanduser().resolve()
    if not root.is_dir():
        return []
    return sorted(
        (path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")),
        key=lambda path: path.name.casefold(),
    )


def project_path(name_or_path: Path, analysis_root: Path | None = None) -> Path:
    """Resolve a bare project name below data-analysis while preserving explicit paths."""
    expanded = name_or_path.expanduser()
    if expanded.is_absolute() or expanded.parent != Path("."):
        return expanded.resolve()
    return ((analysis_root or default_analysis_root()) / expanded).resolve()


def primary_runs_root(workspace: Path, settings: Settings) -> Path:
    return workspace.resolve() / settings.runs_dir


def candidate_runs_roots(workspace: Path, settings: Settings) -> list[Path]:
    """Return the current root followed by the pre-workspace-layout root."""
    current = primary_runs_root(workspace, settings)
    legacy = workspace.resolve() / LEGACY_RUNS_DIR
    return [current] if current == legacy else [current, legacy]


def resolve_runs_root(workspace: Path, settings: Settings, run_id: str) -> Path:
    """Find an existing Run without creating directories while probing."""
    for root in candidate_runs_roots(workspace, settings):
        run_root = root / run_id
        if (run_root / "state/latest.json").is_file() or (run_root / "run.yaml").is_file():
            return root
    return primary_runs_root(workspace, settings)


def list_workspace_runs(workspace: Path, settings: Settings) -> list[dict[str, Any]]:
    """List current and legacy Runs, preferring the current location on ID collisions."""
    by_id: dict[str, dict[str, Any]] = {}
    for root in candidate_runs_roots(workspace, settings):
        for row in RunStore.list_runs(root):
            if row["run_id"] not in by_id:
                by_id[row["run_id"]] = row
    return sorted(by_id.values(), key=lambda row: row["updated_at"], reverse=True)


def initialize_workspace(path: Path) -> Path:
    """Create a workspace that keeps inputs and generated state clearly separated."""
    workspace = path.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / WORKSPACE_DATA_DIR).mkdir(exist_ok=True)
    (workspace / WORKSPACE_REFERENCES_DIR).mkdir(exist_ok=True)
    private_dir = workspace / ".data-agent"
    (private_dir / "runs").mkdir(parents=True, exist_ok=True)
    ignore_file = private_dir / ".gitignore"
    if not ignore_file.exists():
        ignore_file.write_text("*\n!.gitignore\n", encoding="utf-8")
    return workspace

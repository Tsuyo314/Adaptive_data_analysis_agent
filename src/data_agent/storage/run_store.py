from __future__ import annotations

import json
import platform
import sqlite3
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import yaml


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _library_versions() -> dict[str, str]:
    packages = (
        "langgraph",
        "pandas",
        "numpy",
        "scipy",
        "statsmodels",
        "pandera",
        "plotly",
        "streamlit",
        "pydantic",
        "httpx",
        "openai",
        "PyJWT",
        "platformdirs",
    )
    versions: dict[str, str] = {}
    for package in packages:
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = "not-installed"
    return versions


class RunStore:
    """Run-scoped artifact storage plus an application event database."""

    DIRECTORIES = (
        "state",
        "inputs",
        "questions",
        "plans",
        "preprocessing",
        "analysis/causal",
        "validation",
        "visualizations",
        "report",
        "llm",
        "logs",
    )

    def __init__(self, runs_root: Path, run_id: str):
        self.runs_root = runs_root.resolve()
        self.run_id = run_id
        self.root = self.runs_root / run_id
        for directory in self.DIRECTORIES:
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.db_path = self.runs_root / "runs.sqlite3"
        self._init_db()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        self.runs_root.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    working_directory TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    node TEXT NOT NULL,
                    event TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms REAL,
                    payload TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    approved INTEGER NOT NULL,
                    feedback TEXT NOT NULL DEFAULT ''
                );
                """
            )

    def initialize(self, state: dict[str, Any], config: dict[str, Any] | None = None) -> None:
        now = self.now()
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO runs
                   (run_id, thread_id, status, working_directory, created_at, updated_at)
                   VALUES (
                     ?, ?, ?, ?, COALESCE((SELECT created_at FROM runs WHERE run_id=?), ?), ?
                   )""",
                (
                    self.run_id,
                    state["thread_id"],
                    state["status"],
                    state["working_directory"],
                    self.run_id,
                    now,
                    now,
                ),
            )
        self.write_yaml(
            "run.yaml",
            {
                "run_id": self.run_id,
                "thread_id": state["thread_id"],
                "created_at": now,
                "working_directory": state["working_directory"],
                "user_request": state["user_request"],
                "runtime": {
                    "python": sys.version,
                    "platform": platform.platform(),
                    "libraries": _library_versions(),
                },
                "config": config or {},
            },
        )
        self.checkpoint(state)

    def write_json(self, relative_path: str, value: Any) -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        return path

    def write_yaml(self, relative_path: str, value: Any) -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return path

    def read_json(self, relative_path: str, default: Any = None) -> Any:
        path = self.root / relative_path
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def checkpoint(self, state: dict[str, Any]) -> Path:
        existing = sorted((self.root / "state").glob("checkpoint_*.json"))
        path = self.write_json(f"state/checkpoint_{len(existing) + 1:03d}.json", state)
        self.write_json("state/latest.json", state)
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET status=?, updated_at=? WHERE run_id=?",
                (state["status"], self.now(), self.run_id),
            )
        return path

    def load_state(self) -> dict[str, Any]:
        state = self.read_json("state/latest.json")
        if state is None:
            raise FileNotFoundError(f"Checkpoint not found for run {self.run_id}")
        return state

    def event(
        self,
        node: str,
        event: str,
        status: str,
        payload: dict[str, Any] | None = None,
        duration_ms: float | None = None,
    ) -> None:
        timestamp = self.now()
        record = {
            "timestamp": timestamp,
            "run_id": self.run_id,
            "node": node,
            "event": event,
            "status": status,
            "duration_ms": duration_ms,
            "payload": payload or {},
        }
        with (self.root / "logs/process.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO events
                   (run_id, timestamp, node, event, status, duration_ms, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.run_id,
                    timestamp,
                    node,
                    event,
                    status,
                    duration_ms,
                    json.dumps(payload or {}, ensure_ascii=False, default=_json_default),
                ),
            )

    def error(self, node: str, error: dict[str, Any]) -> None:
        with (self.root / "logs/errors.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{self.now()} [{node}] {error.get('type')}: {error.get('message')}\n")

    def approval(self, stage: str, approved: bool, feedback: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO approvals
                   (run_id, timestamp, stage, approved, feedback)
                   VALUES (?, ?, ?, ?, ?)""",
                (self.run_id, self.now(), stage, int(approved), feedback),
            )

    def artifact(self, relative_path: str) -> str:
        return str((self.root / relative_path).resolve())

    def write_llm_call(self, task: str, value: dict[str, Any]) -> Path:
        existing = sorted((self.root / "llm").glob("call_*.json"))
        safe_task = "".join(
            character for character in task if character.isalnum() or character in "-_"
        )
        return self.write_json(f"llm/call_{len(existing) + 1:03d}_{safe_task}.json", value)

    @classmethod
    def list_runs(cls, runs_root: Path) -> list[dict[str, Any]]:
        db_path = runs_root.resolve() / "runs.sqlite3"
        if not db_path.exists():
            return []
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT * FROM runs ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

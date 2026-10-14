from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from data_agent.config import Settings

EXCLUDED_PARTS = {
    ".data-agent",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "runs",
    "mlruns",
    "__pycache__",
}


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _is_allowed_path(path: Path, workspace: Path) -> bool:
    try:
        path.resolve().relative_to(workspace.resolve())
        return True
    except ValueError:
        return False


def discover_workspace(workspace: Path, settings: Settings) -> dict[str, Any]:
    workspace = workspace.resolve()
    data_files: list[dict[str, Any]] = []
    supporting_files: list[dict[str, Any]] = []
    for path in workspace.rglob("*"):
        if not path.is_file() or any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        extension = path.suffix.lower()
        size = path.stat().st_size
        base = {
            "path": str(path),
            "relative_path": str(path.relative_to(workspace)),
            "extension": extension,
            "size_bytes": size,
        }
        if extension in settings.allowed_data_extensions:
            base["eligible"] = size <= settings.max_file_size_mb * 1024 * 1024
            base["sha256"] = sha256_file(path) if base["eligible"] else None
            data_files.append(base)
        elif extension in settings.allowed_support_extensions:
            base["eligible"] = size <= settings.max_supporting_file_kb * 1024
            supporting_files.append(base)
    return {
        "workspace": str(workspace),
        "data_files": sorted(data_files, key=lambda item: item["relative_path"]),
        "supporting_files": sorted(supporting_files, key=lambda item: item["relative_path"]),
    }


def resolve_explicit_dataset(user_request: str, workspace: Path) -> Path | None:
    patterns = [r"(?:^|[\s'\"])([^\s'\"]+\.(?:csv|xlsx))(?:$|[\s'\"])"]
    for pattern in patterns:
        match = re.search(pattern, user_request, flags=re.IGNORECASE)
        if match:
            candidate = Path(match.group(1)).expanduser()
            return (
                (workspace / candidate).resolve()
                if not candidate.is_absolute()
                else candidate.resolve()
            )
    return None


def select_dataset(
    manifest: dict[str, Any],
    user_request: str,
    workspace: Path,
    allow_outside_workspace: bool = False,
) -> Path:
    explicit = resolve_explicit_dataset(user_request, workspace)
    if explicit:
        if not explicit.exists() or not explicit.is_file():
            raise FileNotFoundError(f"指定データが見つかりません: {explicit}")
        if explicit.suffix.lower() not in {".csv", ".xlsx"}:
            raise ValueError("主データはCSVまたはXLSXである必要があります")
        if not _is_allowed_path(explicit, workspace) and not allow_outside_workspace:
            raise PermissionError("workspace外のデータ参照には明示承認が必要です")
        return explicit
    candidates = [Path(item["path"]) for item in manifest["data_files"] if item["eligible"]]
    if not candidates:
        raise FileNotFoundError("workspace配下に読み込み可能なCSV/XLSXがありません")
    if len(candidates) == 1:
        return candidates[0]
    # Prefer files close to the workspace root; ties are stable and explicit in the manifest.
    return sorted(candidates, key=lambda path: (len(path.relative_to(workspace).parts), str(path)))[
        0
    ]


def inspect_dataset(path: Path, settings: Settings) -> dict[str, Any]:
    if path.stat().st_size > settings.max_file_size_mb * 1024 * 1024:
        raise ValueError(f"ファイルサイズ上限（{settings.max_file_size_mb}MB）を超えています")
    if path.suffix.lower() == ".csv":
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                dialect = csv.Sniffer().sniff(handle.read(8192))
            separator = dialect.delimiter
        except (UnicodeDecodeError, csv.Error):
            separator = ","
        sample = pd.read_csv(path, sep=separator, nrows=settings.metadata_sample_rows)
        row_count: int | None = None
        with path.open("rb") as handle:
            # Line count is metadata-only and avoids parsing the full dataset.
            row_count = max(sum(1 for _ in handle) - 1, 0)
        sheets: list[str] = []
    else:
        excel = pd.ExcelFile(path)
        sheets = excel.sheet_names
        sample = pd.read_excel(path, sheet_name=sheets[0], nrows=settings.metadata_sample_rows)
        row_count = None  # Avoid materializing a complete workbook during inspection.
        separator = None
    return {
        "path": str(path.resolve()),
        "format": path.suffix.lower().lstrip("."),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "columns": [str(column) for column in sample.columns],
        "sample_rows": len(sample),
        "estimated_rows": row_count,
        "sample_dtypes": {str(key): str(value) for key, value in sample.dtypes.items()},
        "sample_missing": {str(key): int(value) for key, value in sample.isna().sum().items()},
        "sample": sample.head(10).where(sample.notna(), None).to_dict(orient="records"),
        "sheets": sheets,
        "separator": separator,
        "inspection_scope": f"先頭{settings.metadata_sample_rows}行以内（全量統計は未実行）",
    }


def summarize_supporting_file(path: Path, settings: Settings) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path.resolve()),
        "extension": path.suffix.lower(),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "executed": False,
    }
    if path.stat().st_size > settings.max_supporting_file_kb * 1024:
        return {**result, "status": "skipped_too_large", "summary": "サイズ上限により未読"}
    text = path.read_text(encoding="utf-8", errors="replace")
    result["status"] = "read"
    if path.suffix.lower() == ".json":
        try:
            parsed = json.loads(text)
            result["summary"] = json.dumps(parsed, ensure_ascii=False)[:2000]
        except json.JSONDecodeError as exc:
            result["status"] = "invalid"
            result["summary"] = f"JSON parse error: {exc}"
    elif path.suffix.lower() in {".yaml", ".yml"}:
        try:
            parsed = yaml.safe_load(text)
            result["summary"] = yaml.safe_dump(parsed, allow_unicode=True)[:2000]
        except yaml.YAMLError as exc:
            result["status"] = "invalid"
            result["summary"] = f"YAML parse error: {exc}"
    else:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        result["summary"] = "\n".join(lines[:40])[:4000]
    return result


def inspect_supporting_files(
    manifest: dict[str, Any], settings: Settings, dataset_path: Path
) -> list[dict[str, Any]]:
    summaries = []
    for item in manifest["supporting_files"]:
        path = Path(item["path"])
        if path.resolve() == dataset_path.resolve() or not item["eligible"]:
            continue
        summaries.append(summarize_supporting_file(path, settings))
    return summaries

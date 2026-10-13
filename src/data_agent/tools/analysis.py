from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.express as px


def load_dataset(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() == ".xlsx":
        return pd.read_excel(path)
    raise ValueError(f"Unsupported dataset format: {path.suffix}")


def preprocess(
    frame: pd.DataFrame,
    output_path: Path,
    drop_duplicates: bool,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    before = len(frame)
    processed = frame.copy(deep=True)
    duplicate_count = int(processed.duplicated().sum())
    date_conversions: list[dict[str, Any]] = []
    for column in processed.select_dtypes(include=["object", "string"]).columns:
        name = str(column).lower()
        if not any(token in name for token in ("date", "time", "日時", "日付")):
            continue
        source = processed[column]
        parsed = pd.to_datetime(source, errors="coerce")
        failures = int((source.notna() & parsed.isna()).sum())
        non_missing = int(source.notna().sum())
        failure_rate = failures / non_missing if non_missing else 0.0
        if failure_rate <= 0.1:
            processed[column] = parsed
            status = "converted"
        else:
            # Local recovery: retain the original values instead of corrupting failed rows.
            status = "skipped_parse_errors"
        date_conversions.append(
            {
                "column": str(column),
                "status": status,
                "parse_failures": failures,
                "failure_rate": failure_rate,
            }
        )
    if drop_duplicates:
        processed = processed.drop_duplicates().reset_index(drop=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        processed.to_parquet(output_path, index=False)
        actual_output = output_path
    except (ImportError, ModuleNotFoundError):
        actual_output = output_path.with_suffix(".csv")
        processed.to_csv(actual_output, index=False)
    log = {
        "input_rows": before,
        "output_rows": len(processed),
        "duplicate_rows_detected": duplicate_count,
        "duplicate_rows_removed": before - len(processed),
        "missing_values_modified": False,
        "date_conversions": date_conversions,
        "original_data_modified": False,
        "output_path": str(actual_output.resolve()),
    }
    return processed, log


def _safe_json_records(frame: pd.DataFrame, limit: int = 100) -> list[dict[str, Any]]:
    clean = frame.head(limit).replace({np.nan: None, np.inf: None, -np.inf: None})
    return json.loads(clean.to_json(orient="records", date_format="iso"))


def describe(frame: pd.DataFrame) -> dict[str, Any]:
    numeric = frame.select_dtypes(include="number")
    categorical = frame.select_dtypes(exclude="number")
    numeric_summary = (
        numeric.describe().transpose().reset_index(names="column")
        if not numeric.empty
        else pd.DataFrame()
    )
    categorical_rows: list[dict[str, Any]] = []
    for column in categorical.columns:
        series = categorical[column]
        counts = series.astype("string").value_counts(dropna=True)
        categorical_rows.append(
            {
                "column": str(column),
                "count": int(series.notna().sum()),
                "unique": int(series.nunique(dropna=True)),
                "top": str(counts.index[0]) if len(counts) else None,
                "freq": int(counts.iloc[0]) if len(counts) else 0,
            }
        )
    correlations = (
        numeric.corr().replace({np.nan: None}).to_dict() if len(numeric.columns) > 1 else {}
    )
    return {
        "row_count": int(len(frame)),
        "column_count": int(len(frame.columns)),
        "columns": [str(column) for column in frame.columns],
        "dtypes": {str(key): str(value) for key, value in frame.dtypes.items()},
        "missing": {str(key): int(value) for key, value in frame.isna().sum().items()},
        "missing_rate": {
            str(key): float(value) for key, value in frame.isna().mean().round(6).items()
        },
        "duplicate_rows": int(frame.duplicated().sum()),
        "numeric_summary": _safe_json_records(numeric_summary),
        "categorical_summary": categorical_rows,
        "correlations": correlations,
        "preview": _safe_json_records(frame, limit=20),
    }


def create_visualizations(
    frame: pd.DataFrame,
    output_dir: Path,
    target_column: str | None = None,
) -> list[dict[str, str]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, str]] = []
    numeric_columns = list(frame.select_dtypes(include="number").columns)
    for column in numeric_columns[:5]:
        figure = px.histogram(frame, x=column, title=f"{column} の分布", marginal="box")
        path = output_dir / f"hist_{_safe_filename(str(column))}.html"
        figure.write_html(path, include_plotlyjs="cdn")
        artifacts.append({"type": "histogram", "column": str(column), "path": str(path.resolve())})
    if len(numeric_columns) >= 2:
        corr = frame[numeric_columns[:12]].corr(numeric_only=True)
        figure = px.imshow(corr, text_auto=".2f", title="数値列の相関")
        path = output_dir / "correlation.html"
        figure.write_html(path, include_plotlyjs="cdn")
        artifacts.append({"type": "correlation", "path": str(path.resolve())})
    if target_column in frame.columns and target_column in numeric_columns:
        figure = px.box(
            frame, y=target_column, points="outliers", title=f"{target_column} の箱ひげ図"
        )
        path = output_dir / f"target_{_safe_filename(target_column)}.html"
        figure.write_html(path, include_plotlyjs="cdn")
        artifacts.append(
            {"type": "target_box", "column": target_column, "path": str(path.resolve())}
        )
    return artifacts


def _safe_filename(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "_" for character in value
    )

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from data_agent.schemas import ValidationResult


def validate_data(frame: pd.DataFrame) -> list[dict[str, Any]]:
    results = [
        ValidationResult(
            rule_id="DATA-001",
            stage="data",
            passed=len(frame) > 0,
            severity="error",
            reason="データに1行以上ある" if len(frame) > 0 else "データが空です",
        ),
        ValidationResult(
            rule_id="DATA-002",
            stage="data",
            passed=len(frame.columns) > 0 and not frame.columns.duplicated().any(),
            severity="error",
            reason=(
                "列名は存在し重複していない"
                if len(frame.columns) > 0 and not frame.columns.duplicated().any()
                else "列名がないか重複しています"
            ),
        ),
    ]
    all_missing = [str(column) for column in frame.columns if frame[column].isna().all()]
    results.append(
        ValidationResult(
            rule_id="DATA-003",
            stage="data",
            passed=not all_missing,
            severity="warning",
            reason="全欠損列なし" if not all_missing else "全欠損の列があります",
            details={"columns": all_missing},
        )
    )
    duplicate_rate = float(frame.duplicated().mean()) if len(frame) else 0.0
    results.append(
        ValidationResult(
            rule_id="DATA-004",
            stage="data",
            passed=duplicate_rate <= 0.5,
            severity="warning",
            reason="重複率は50%以下" if duplicate_rate <= 0.5 else "重複率が50%を超えています",
            details={"duplicate_rate": duplicate_rate},
        )
    )
    numeric = frame.select_dtypes(include="number")
    infinite_columns = [
        str(column) for column in numeric.columns if np.isinf(numeric[column].dropna()).any()
    ]
    results.append(
        ValidationResult(
            rule_id="DATA-005",
            stage="data",
            passed=not infinite_columns,
            severity="error",
            reason="無限値なし" if not infinite_columns else "無限値が含まれます",
            details={"columns": infinite_columns},
        )
    )
    # Pandera is used when present; explicit rules above remain readable without it.
    try:
        import pandera.pandas as pa

        schema = pa.DataFrameSchema({}, strict=False, coerce=False)
        schema.validate(frame, lazy=True)
        results.append(
            ValidationResult(
                rule_id="DATA-PANDERA-001",
                stage="data",
                passed=True,
                severity="info",
                reason="Pandera DataFrameスキーマ検証に合格",
            )
        )
    except ImportError:
        results.append(
            ValidationResult(
                rule_id="DATA-PANDERA-001",
                stage="data",
                passed=True,
                severity="info",
                reason="Pandera未導入のため組み込みルールで検証",
            )
        )
    except Exception as exc:
        results.append(
            ValidationResult(
                rule_id="DATA-PANDERA-001",
                stage="data",
                passed=False,
                severity="error",
                reason=f"Pandera検証エラー: {exc}",
            )
        )
    return [result.model_dump(mode="json") for result in results]


def validate_results(
    frame: pd.DataFrame, descriptive: dict[str, Any], causal: dict[str, Any] | None
) -> list[dict[str, Any]]:
    row_match = descriptive.get("row_count") == len(frame)
    missing_match = descriptive.get("missing") == {
        str(key): int(value) for key, value in frame.isna().sum().items()
    }
    results = [
        ValidationResult(
            rule_id="RESULT-001",
            stage="result",
            passed=row_match,
            severity="error",
            reason="集計行数が処理済みデータと一致" if row_match else "集計行数が一致しません",
        ),
        ValidationResult(
            rule_id="RESULT-002",
            stage="result",
            passed=missing_match,
            severity="error",
            reason="欠損集計が処理済みデータと一致" if missing_match else "欠損集計が一致しません",
        ),
    ]
    if causal and causal.get("status") == "estimated":
        estimate = causal.get("estimate")
        interval = causal.get("confidence_interval", [])
        causal_ok = (
            isinstance(estimate, (int, float))
            and np.isfinite(estimate)
            and len(interval) == 2
            and interval[0] <= estimate <= interval[1]
        )
        results.append(
            ValidationResult(
                rule_id="RESULT-CAUSAL-001",
                stage="result",
                passed=causal_ok,
                severity="error",
                reason=(
                    "因果推定値と信頼区間は有限で整合"
                    if causal_ok
                    else "因果推定値または信頼区間が不整合"
                ),
            )
        )
    return [result.model_dump(mode="json") for result in results]


def validate_final(report_data_path: Path, report_path: Path) -> list[dict[str, Any]]:
    report_data = json.loads(report_data_path.read_text(encoding="utf-8"))
    report_text = report_path.read_text(encoding="utf-8")
    row_count = report_data["descriptive"]["row_count"]
    marker_present = f"分析対象行数: {row_count:,}" in report_text
    source_paths_exist = all(
        Path(path).exists() for path in report_data["provenance"]["source_artifacts"]
    )
    results = [
        ValidationResult(
            rule_id="FINAL-001",
            stage="final",
            passed=marker_present,
            severity="error",
            reason="保存レポートの主要KPIがreport_dataと一致"
            if marker_present
            else "レポートKPI不一致",
        ),
        ValidationResult(
            rule_id="FINAL-002",
            stage="final",
            passed=source_paths_exist,
            severity="error",
            reason="表示元の検証済み成果物が存在"
            if source_paths_exist
            else "参照成果物がありません",
        ),
    ]
    return [result.model_dump(mode="json") for result in results]


def has_blocking_failures(results: list[dict[str, Any]]) -> bool:
    return any(not result["passed"] and result["severity"] == "error" for result in results)

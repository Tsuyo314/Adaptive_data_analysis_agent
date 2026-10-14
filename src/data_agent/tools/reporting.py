from __future__ import annotations

import html
import json
import math
from pathlib import Path
from typing import Any


def _markdown_cell(value: Any) -> str:
    if value is None:
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _format_stat(value: Any) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _markdown_cell(value)
    return f"{number:,.6g}" if math.isfinite(number) else "—"


def _format_count(value: Any) -> str:
    if value is None:
        return "0"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError, OverflowError):
        return _markdown_cell(value)


def build_report_data(
    state: dict[str, Any],
    descriptive: dict[str, Any],
    causal: dict[str, Any],
    visualizations: list[dict[str, str]],
    validations: list[dict[str, Any]],
    source_artifacts: list[str],
) -> dict[str, Any]:
    causal_summary: dict[str, Any]
    if causal.get("status") == "estimated":
        causal_summary = {
            "status": "estimated",
            "estimate": causal["estimate"],
            "confidence_interval": causal["confidence_interval"],
            "method": causal["method"],
            "p_value": causal["p_value"],
        }
    else:
        causal_summary = {
            "status": causal.get("status", "not_requested"),
            "reason": causal.get("reason", "因果推論は実施していません"),
        }
    return {
        "run_id": state["run_id"],
        "thread_id": state["thread_id"],
        "plan_version": state["plan_version"],
        "dataset": {
            "path": state["dataset_path"],
            "sha256": state["dataset_hash"],
        },
        "request": state["user_request"],
        "answers": state.get("answers", {}),
        "descriptive": descriptive,
        "causal": causal,
        "causal_summary": causal_summary,
        "visualizations": visualizations,
        "validations": validations,
        "limitations": _limitations(descriptive, causal, validations),
        "provenance": {"source_artifacts": source_artifacts},
    }


def _limitations(
    descriptive: dict[str, Any], causal: dict[str, Any], validations: list[dict[str, Any]]
) -> list[str]:
    limitations = ["記述統計は観測データの関連を示すものであり、単独では因果関係を示しません。"]
    if any(rate > 0 for rate in descriptive.get("missing_rate", {}).values()):
        limitations.append(
            "欠損値が含まれるため、欠損メカニズムによって結果が偏る可能性があります。"
        )
    limitations.extend(causal.get("limitations", []))
    warnings = [item["reason"] for item in validations if not item["passed"]]
    limitations.extend(warnings)
    return list(dict.fromkeys(limitations))


def render_markdown(report: dict[str, Any]) -> str:
    descriptive = report["descriptive"]
    causal = report["causal"]
    lines = [
        "# 分析レポート",
        "",
        f"- Run ID: `{report['run_id']}`",
        f"- Plan version: {report['plan_version']}",
        f"- Dataset SHA-256: `{report['dataset']['sha256']}`",
        f"- 分析対象行数: {descriptive['row_count']:,}",
        f"- 列数: {descriptive['column_count']:,}",
        "",
        "## 依頼",
        "",
        report["request"],
        "",
        "## 要約",
        "",
        (
            f"検証済みデータ {descriptive['row_count']:,} 行、"
            f"{descriptive['column_count']:,} 列を分析しました。"
        ),
        f"完全重複行は {descriptive['duplicate_rows']:,} 行です。",
        "",
        "## 基礎統計量",
        "",
        "### 数値列",
        "",
    ]
    numeric_summary = descriptive.get("numeric_summary", [])
    if numeric_summary:
        lines.extend(
            [
                "| 列 | 有効件数 | 欠損数 | 平均 | 標準偏差 | 最小 | 25% | 中央値 | 75% | 最大 |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for row in numeric_summary:
            column = str(row.get("column", ""))
            cells = [
                _markdown_cell(column),
                _format_count(row.get("count")),
                _format_count(descriptive.get("missing", {}).get(column, 0)),
                _format_stat(row.get("mean")),
                _format_stat(row.get("std")),
                _format_stat(row.get("min")),
                _format_stat(row.get("25%")),
                _format_stat(row.get("50%")),
                _format_stat(row.get("75%")),
                _format_stat(row.get("max")),
            ]
            lines.append(f"| {' | '.join(cells)} |")
    else:
        lines.append("_数値列はありません。_")
    lines.extend(["", "### カテゴリ列", ""])
    categorical_summary = descriptive.get("categorical_summary", [])
    if categorical_summary:
        lines.extend(
            [
                "| 列 | 有効件数 | 欠損数 | ユニーク数 | 最頻値 | 最頻値件数 |",
                "|---|---:|---:|---:|---|---:|",
            ]
        )
        for row in categorical_summary:
            column = str(row.get("column", ""))
            cells = [
                _markdown_cell(column),
                _format_count(row.get("count")),
                _format_count(descriptive.get("missing", {}).get(column, 0)),
                _format_count(row.get("unique")),
                _markdown_cell(row.get("top")),
                _format_count(row.get("freq")),
            ]
            lines.append(f"| {' | '.join(cells)} |")
    else:
        lines.append("_カテゴリ列はありません。_")
    narrative = report.get("narrative", {})
    if narrative:
        lines.extend(["", "## AIによる定性的な要約", ""])
        for item in narrative.get("summary", []):
            evidence = ", ".join(item["evidence_ids"])
            lines.append(f"- {item['text']}（根拠: `{evidence}`）")
        lines.extend(["", "### 解釈", ""])
        for item in narrative.get("interpretations", []):
            evidence = ", ".join(item["evidence_ids"])
            lines.append(f"- {item['text']}（根拠: `{evidence}`）")
    lines.extend(["", "## 因果分析", ""])
    if causal.get("status") == "estimated":
        low, high = causal["confidence_interval"]
        lines.extend(
            [
                f"- 手法: {causal['method']}",
                f"- 推定効果: {causal['estimate']:.6g}",
                f"- 95%信頼区間: [{low:.6g}, {high:.6g}]",
                f"- p値: {causal['p_value']:.6g}",
                "",
                "この値は記載した識別仮定が成立する場合にのみ因果効果として解釈できます。",
            ]
        )
    else:
        lines.append(f"推定未実施（{causal.get('status')}）: {causal.get('reason', '対象外')} ")
    lines.extend(["", "## 検証結果", "", "| Rule | 判定 | 理由 |", "|---|---|---|"])
    for result in report["validations"]:
        verdict = "PASS" if result["passed"] else "FAIL"
        lines.append(f"| {result['rule_id']} | {verdict} | {result['reason']} |")
    lines.extend(["", "## 制約", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    for item in narrative.get("limitations", []):
        evidence = ", ".join(item["evidence_ids"])
        lines.append(f"- {item['text']}（根拠: `{evidence}`）")
    lines.extend(
        [
            "",
            "## 次のアクション",
            "",
            "- 欠損・外れ値・期間定義を業務知識と照合してください。",
            "- 因果推論を意思決定に用いる場合は識別仮定と感度分析を追加レビューしてください。",
            "- `logs/process.jsonl` と検証JSONから処理履歴を監査できます。",
        ]
    )
    for item in narrative.get("next_actions", []):
        evidence = ", ".join(item["evidence_ids"])
        lines.append(f"- {item['text']}（根拠: `{evidence}`）")
    lines.extend(
        [
            "",
            "## AI設定",
            "",
            f"- Provider: `{report.get('llm', {}).get('provider', 'deterministic')}`",
            f"- Model: `{report.get('llm', {}).get('model', '') or 'なし'}`",
            "",
        ]
    )
    return "\n".join(lines)


def render_html(markdown_text: str, report: dict[str, Any]) -> str:
    # Keep HTML dependency-free. Streamlit renders richer tables from report_data.json.
    escaped = html.escape(markdown_text)
    payload = html.escape(json.dumps(report, ensure_ascii=False))
    return f"""<!doctype html>
<html lang="ja">
<head><meta charset="utf-8"><title>Analysis report</title>
<style>
body{{font-family:system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem}}
pre{{white-space:pre-wrap}}
</style></head>
<body><pre>{escaped}</pre>
<script type="application/json" id="report-data">{payload}</script>
</body></html>"""


def save_report(report: dict[str, Any], report_dir: Path) -> tuple[Path, Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    data_path = report_dir / "report_data.json"
    markdown_path = report_dir / "report.md"
    html_path = report_dir / "report.html"
    data_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown(report)
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(render_html(markdown, report), encoding="utf-8")
    return data_path, markdown_path, html_path

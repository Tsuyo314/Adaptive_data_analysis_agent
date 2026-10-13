from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import statsmodels.api as sm

CAUSAL_SCOPES = {"因果効果の推定", "傾向把握と因果効果"}


def _value(answers: dict[str, dict[str, Any]], key: str) -> Any:
    return answers.get(key, {}).get("value")


def _coerce_treatment(series: pd.Series, treatment_value: Any) -> pd.Series:
    as_text = series.astype("string")
    indicator = (as_text == str(treatment_value)).astype(float)
    if indicator.nunique() < 2 and pd.api.types.is_numeric_dtype(series):
        try:
            indicator = (series == float(treatment_value)).astype(float)
        except (TypeError, ValueError):
            pass
    return indicator


def run_causal_analysis(frame: pd.DataFrame, answers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    scope = _value(answers, "analysis_scope")
    if scope not in CAUSAL_SCOPES:
        return {"status": "not_requested", "reason": "選択した分析範囲に因果推論を含まない"}
    target = str(_value(answers, "target_column") or "")
    treatment = str(_value(answers, "treatment_column") or "")
    treatment_value = _value(answers, "treatment_value")
    assumption = _value(answers, "identification_assumption")
    confounder_text = str(_value(answers, "confounders") or "unknown")
    confounders = [part.strip() for part in confounder_text.split(",") if part.strip()]
    if confounders == ["unknown"]:
        confounders = []
    missing = [
        column for column in [target, treatment, *confounders] if column not in frame.columns
    ]
    assumptions = {
        "consistency": "同じ処置値は同じ介入を表す必要がある",
        "positivity": "各交絡層で処置・対照の双方が観測される必要がある",
        "conditional_exchangeability": bool(assumption),
        "no_interference": "個体間で処置の影響が波及しない必要がある",
        "confounders": confounders,
    }
    limitations = [
        "観測データから未観測交絡がないことは検証できません",
        "この推定は識別仮定が成立する場合に限り因果効果として解釈できます",
    ]
    if missing:
        return {
            "status": "not_applicable",
            "reason": "指定列がデータにありません",
            "missing_columns": missing,
            "assumptions": assumptions,
            "limitations": limitations,
        }
    if assumption is not True:
        return {
            "status": "not_identified",
            "reason": "条件付き交換可能性（未観測交絡なし）が承認されていません",
            "assumptions": assumptions,
            "limitations": limitations,
        }
    if not pd.api.types.is_numeric_dtype(frame[target]):
        return {
            "status": "not_applicable",
            "reason": "MVPの回帰調整は数値の結果変数のみ対応します",
            "assumptions": assumptions,
            "limitations": limitations,
        }
    treatment_indicator = _coerce_treatment(frame[treatment], treatment_value)
    if treatment_indicator.nunique() < 2:
        return {
            "status": "not_applicable",
            "reason": "処置群と対照群の両方が必要です",
            "assumptions": assumptions,
            "limitations": limitations,
        }
    usable_confounders = [
        column for column in confounders if pd.api.types.is_numeric_dtype(frame[column])
    ]
    excluded_confounders = sorted(set(confounders) - set(usable_confounders))
    model_frame = pd.DataFrame({"outcome": frame[target], "treatment": treatment_indicator})
    for column in usable_confounders:
        model_frame[column] = frame[column]
    model_frame = model_frame.replace([np.inf, -np.inf], np.nan).dropna()
    group_counts = model_frame.groupby("treatment").size().to_dict()
    if len(model_frame) < max(10, len(usable_confounders) + 4) or min(group_counts.values()) < 5:
        return {
            "status": "not_applicable",
            "reason": "回帰調整に必要なサンプル数が不足しています",
            "diagnostics": {"complete_cases": len(model_frame), "group_counts": group_counts},
            "assumptions": assumptions,
            "limitations": limitations,
        }
    design = sm.add_constant(model_frame[["treatment", *usable_confounders]], has_constant="add")
    model = sm.OLS(model_frame["outcome"], design).fit(cov_type="HC3")
    interval = model.conf_int().loc["treatment"].tolist()
    propensity = float(treatment_indicator.mean())
    limitations.extend(
        [f"非数値の交絡候補はMVPで調整対象外: {excluded_confounders}"]
        if excluded_confounders
        else []
    )
    return {
        "status": "estimated",
        "method": "OLS regression adjustment with HC3 robust standard errors",
        "estimand": "conditional average treatment effect coefficient",
        "outcome": target,
        "treatment": treatment,
        "treatment_value": treatment_value,
        "estimate": float(model.params["treatment"]),
        "standard_error": float(model.bse["treatment"]),
        "p_value": float(model.pvalues["treatment"]),
        "confidence_interval": [float(interval[0]), float(interval[1])],
        "n": int(model.nobs),
        "assumptions": assumptions,
        "diagnostics": {
            "r_squared": float(model.rsquared),
            "condition_number": float(model.condition_number),
            "treatment_prevalence": propensity,
            "group_counts": {str(key): int(value) for key, value in group_counts.items()},
            "excluded_confounders": excluded_confounders,
        },
        "limitations": limitations,
    }

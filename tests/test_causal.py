import pandas as pd

from data_agent.specialists.causal import run_causal_analysis


def _answers(assumption=True):
    return {
        "analysis_scope": {"value": ["傾向把握・記述統計", "因果効果の推定"]},
        "target_column": {"value": "y"},
        "treatment_column": {"value": "t"},
        "treatment_value": {"value": "1"},
        "confounders": {"value": "x"},
        "identification_assumption": {"value": assumption},
    }


def test_causal_refuses_when_identification_is_not_accepted():
    frame = pd.DataFrame({"y": range(20), "t": [0] * 10 + [1] * 10, "x": range(20)})
    result = run_causal_analysis(frame, _answers(False))
    assert result["status"] == "not_identified"
    assert "推定" not in result


def test_causal_estimates_only_when_requirements_hold():
    frame = pd.DataFrame(
        {
            "y": [10 + i + 3 * (i >= 10) for i in range(20)],
            "t": [0] * 10 + [1] * 10,
            "x": list(range(20)),
        }
    )
    result = run_causal_analysis(frame, _answers(True))
    assert result["status"] == "estimated"
    assert len(result["confidence_interval"]) == 2
    assert result["limitations"]

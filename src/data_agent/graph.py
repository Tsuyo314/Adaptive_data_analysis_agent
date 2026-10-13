from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:
    from langgraph.graph import END, START, StateGraph
except ImportError:  # Enables discovery/tests before optional environment setup.
    END = "__end__"
    START = "__start__"
    StateGraph = None  # type: ignore[assignment]

from data_agent.state import AnalysisState

AUTOMATIC_STATUS_TO_NODE = {
    "initialized": "discover",
    "questions_complete": "plan",
    "plan_approved": "preprocess",
    "preprocessed": "validate_data",
    "data_valid": "eda",
    "eda_complete": "validate_results",
    "intermediate_approved": "specialist",
    "specialist_complete": "validate_specialist",
    "specialist_valid": "report",
}

WAITING_STATUSES = {
    "awaiting_questions",
    "awaiting_plan_approval",
    "awaiting_intermediate_approval",
    "human_review",
    "complete",
}


def build_graph(nodes: dict[str, Callable[[AnalysisState], AnalysisState]]) -> Any:
    if StateGraph is None:
        return FallbackGraph(nodes)
    builder = StateGraph(AnalysisState)
    builder.add_node("router", lambda state: state)
    for name, function in nodes.items():
        builder.add_node(name, function)
    builder.add_edge(START, "router")
    builder.add_conditional_edges(
        "router",
        lambda state: AUTOMATIC_STATUS_TO_NODE.get(state["status"], "stop"),
        {**{name: name for name in nodes}, "stop": END},
    )
    for name in nodes:
        builder.add_edge(name, "router")
    return builder.compile()


class FallbackGraph:
    """Small compatibility runner used only if LangGraph is not installed."""

    def __init__(self, nodes: dict[str, Callable[[AnalysisState], AnalysisState]]):
        self.nodes = nodes

    def invoke(self, state: AnalysisState, config: dict[str, Any] | None = None) -> AnalysisState:
        del config
        while state["status"] in AUTOMATIC_STATUS_TO_NODE:
            state = self.nodes[AUTOMATIC_STATUS_TO_NODE[state["status"]]](state)
        return state

    def get_graph(self) -> Any:
        return self

    def draw_mermaid(self) -> str:
        edges = ["flowchart TB", "    START --> router"]
        edges.extend(f"    router --> {node}" for node in self.nodes)
        edges.extend(f"    {node} --> router" for node in self.nodes)
        edges.append("    router --> END")
        return "\n".join(edges)

    def draw_ascii(self) -> str:
        return "START -> router -> [discover|plan|preprocess|validate|eda|specialist|report] -> END"

"""A sequential, bounded StateGraph with explicit conditional failure routes."""

from collections.abc import Callable
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from deepresearch_ai.graph.state import ResearchState

Node = Callable[[ResearchState], dict[str, object]]


def after_planner(state: ResearchState) -> Literal["search", "writer"]:
    usable = state["step_status"]["planner"] == "completed" and state["plan"] and state["subquestions"]
    return "search" if usable and not state["usage"].limit_reason else "writer"


def after_search(state: ResearchState) -> Literal["evidence", "writer"]:
    usable = state["step_status"]["search"] in {"completed", "partial"} and state["sources"]
    return "evidence" if usable and not state["usage"].limit_reason else "writer"


def after_evidence(state: ResearchState) -> Literal["verification", "writer"]:
    usable = state["step_status"]["evidence"] in {"completed", "partial"} and state["claims"]
    return "verification" if usable and not state["usage"].limit_reason else "writer"


def build_research_graph(
    *, planner: Node, search: Node, evidence: Node, verification: Node, writer: Node,
    retry: Node | None = None, should_retry: Callable[[ResearchState], bool] | None = None,
    should_retry_sources: Callable[[ResearchState], bool] | None = None,
    entry_node: str = 'planner',
) -> CompiledStateGraph:
    if retry is not None and should_retry is None:
        raise ValueError("A retry node requires an explicit bounded retry predicate.")
    builder = StateGraph(ResearchState)
    builder.add_node("planner", planner)
    builder.add_node("search", search)
    builder.add_node("evidence", evidence)
    builder.add_node("verification", verification)
    builder.add_node("writer", writer)
    if entry_node not in {'planner','search','evidence','verification','retry','writer'}:
        raise ValueError('Unknown research entry node.')
    builder.add_edge(START, entry_node)
    builder.add_conditional_edges("planner", after_planner, {"search": "search", "writer": "writer"})
    if retry is not None and should_retry_sources is not None:
        def after_search_with_retries(state):
            return "retry" if should_retry_sources(state) else after_search(state)
        builder.add_conditional_edges("search", after_search_with_retries,
                                      {"evidence": "evidence", "retry": "retry", "writer": "writer"})
    else:
        builder.add_conditional_edges("search", after_search, {"evidence": "evidence", "writer": "writer"})
    builder.add_conditional_edges("evidence", after_evidence, {"verification": "verification", "writer": "writer"})
    if retry is not None:
        builder.add_node("retry", retry)
        def after_verification(state):
            return "retry" if should_retry(state) else "writer"
        def after_retry(state):
            return "search" if state["retry_queries"] and not state["usage"].limit_reason else "writer"
        builder.add_conditional_edges("verification", after_verification, {"retry": "retry", "writer": "writer"})
        builder.add_conditional_edges("retry", after_retry, {"search": "search", "writer": "writer"})
    else:
        builder.add_edge("verification", "writer")
    builder.add_edge("writer", END)
    return builder.compile(name="deepresearch")

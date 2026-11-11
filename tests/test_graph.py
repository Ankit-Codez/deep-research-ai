import json
from unittest.mock import Mock

import pytest

from deepresearch_ai import cli
from deepresearch_ai.config import Settings
from deepresearch_ai.graph.state import STEPS, initial_state
from deepresearch_ai.graph.workflow import after_evidence, after_planner, after_search
from deepresearch_ai.models import ClaimVerification, ResearchReport, ResearchRequest
from deepresearch_ai.models.research import PassageAssessment
from deepresearch_ai.models.agents import EvidenceExtraction, PlannerOutput, VerificationOutput
from deepresearch_ai.services import llm
from deepresearch_ai.services.mock_data import SAMPLE_QUERY, sample_sources
from deepresearch_ai.services.mock_llm import MockChatModel
from deepresearch_ai.services.search import MockWebSearch, SearchError
from deepresearch_ai.services.workflow import GraphResearchService


def scripted_model(overrides=None):
    overrides = overrides or {}
    model = Mock()

    def structured(schema):
        if schema not in overrides:
            return MockChatModel().with_structured_output(schema)
        runner = Mock()
        override = overrides[schema]
        if isinstance(override, Exception) or callable(override):
            runner.invoke.side_effect = override
        else:
            runner.invoke.return_value = override
        return runner

    model.with_structured_output.side_effect = structured
    return model


def graph_service(model=None, search=None):
    return GraphResearchService(
        Settings(mock_mode=True), model=model if model is not None else MockChatModel(),
        search=search if search is not None else MockWebSearch(),
    )


def test_graph_runs_five_nodes_in_sequence_with_explicit_updates():
    graph = graph_service().build_graph()
    updates = list(graph.stream(initial_state(ResearchRequest(query=SAMPLE_QUERY))))
    assert [next(iter(event)) for event in updates] == [step for step in STEPS if step != "retry"]
    for event in updates:
        name = next(iter(event))
        assert event[name]["step_status"][name] == "completed"
    assert updates[-1]["writer"]["report"].status == "complete"


def test_graph_model_responsibilities_are_separate_and_no_tools_are_bound():
    model = scripted_model()
    graph_service(model).run(ResearchRequest(query=SAMPLE_QUERY))
    assert [call.args[0] for call in model.with_structured_output.call_args_list] == [
        PlannerOutput, EvidenceExtraction, VerificationOutput,
    ]
    model.bind_tools.assert_not_called()


def test_graph_final_state_contains_all_required_research_fields():
    state = graph_service().run_state(ResearchRequest(query=SAMPLE_QUERY))
    assert state["question"] == SAMPLE_QUERY
    assert state["plan"].query == SAMPLE_QUERY
    assert len(state["subquestions"]) == 3
    assert len(state["sources"]) == len(state["evidence"]) == len(state["verification_results"]) == 2
    assert state["report"].status == "complete"
    assert state["errors"] == []
    assert state["iteration"] == 1
    assert state["page_attempts"] == 2
    assert all(status == "completed" for step, status in state["step_status"].items() if step != "retry")


def test_graph_mock_mode_is_deterministic_and_never_creates_real_clients(monkeypatch):
    monkeypatch.setattr(llm, "init_chat_model", Mock(side_effect=AssertionError("Must not create real clients")))
    request = ResearchRequest(query=SAMPLE_QUERY)
    first = GraphResearchService(Settings(mock_mode=True)).run(request)
    second = GraphResearchService(Settings(mock_mode=True)).run(request)
    first.usage.pop("elapsed_seconds")
    second.usage.pop("elapsed_seconds")
    assert first.model_dump_json() == second.model_dump_json()
    assert first.status == "complete"
    assert "MOCK SAMPLE" in first.body_markdown


def test_initial_state_does_not_share_mutable_collections_across_runs():
    first = initial_state(ResearchRequest(query=SAMPLE_QUERY))
    second = initial_state(ResearchRequest(query=SAMPLE_QUERY))
    first["searched_queries"].append("changed")
    first["step_status"]["planner"] = "failed"
    assert second["searched_queries"] == []
    assert second["step_status"]["planner"] == "pending"


def test_graph_uses_only_real_source_and_evidence_ids_in_report():
    report = graph_service().run(ResearchRequest(query=SAMPLE_QUERY))
    source_ids = {source.source_id for source in report.sources}
    assert set(report.citations.values()) == source_ids
    evidence_ids = {item.evidence_id for item in report.evidence}
    assert all(set(result.evidence_ids) <= evidence_ids for result in report.claims)
    assert ResearchReport.model_validate_json(report.model_dump_json()) == report


@pytest.mark.parametrize(("schema", "expected_nodes"), [
    (PlannerOutput, ["planner", "writer"]),
    (EvidenceExtraction, ["planner", "search", "evidence", "writer"]),
    (VerificationOutput, ["planner", "search", "evidence", "verification", "retry", "search", "evidence", "verification", "retry", "search", "evidence", "verification", "writer"]),
])
def test_graph_failure_routes_terminate_with_uncertainty(schema, expected_nodes):
    model = scripted_model({schema: RuntimeError("sensitive-provider-error")})
    graph = graph_service(model).build_graph()
    updates = list(graph.stream(initial_state(ResearchRequest(query=SAMPLE_QUERY))))
    assert [next(iter(event)) for event in updates] == expected_nodes
    report = updates[-1]["writer"]["report"]
    assert report.status == "insufficient_evidence"
    assert "## Uncertainty" in report.body_markdown
    assert report.citations == {}
    assert "sensitive-provider-error" not in report.model_dump_json()
    assert report.step_status["writer"] == "completed"
    assert not any(status == "pending" for status in report.step_status.values())


def test_empty_search_tries_bounded_followups_then_skips_evidence_verification():
    provider = Mock()
    provider.search.return_value = []
    graph = graph_service(search=provider).build_graph()
    updates = list(graph.stream(initial_state(ResearchRequest(query=SAMPLE_QUERY))))
    assert [next(iter(event)) for event in updates] == [
        "planner", "search", "retry", "search", "retry", "search", "writer"]
    report = updates[-1]["writer"]["report"]
    assert report.sources == []
    assert report.step_status["evidence"] == report.step_status["verification"] == "skipped"


def test_search_failures_are_preserved_after_bounded_new_query_followups():
    provider = Mock()
    provider.search.side_effect = SearchError("sensitive-error")
    state = graph_service(search=provider).run_state(ResearchRequest(query=SAMPLE_QUERY))
    assert provider.search.call_count == 7
    assert state["iteration"] == 3
    assert state["source_retry_count"] == 2
    assert len(state["errors"]) == 7
    queries = [call.args[0] for call in provider.search.call_args_list]
    assert len(set(queries)) == 7
    assert "sensitive-error" not in state["report"].model_dump_json()
    assert state["report"].status == "insufficient_evidence"


def test_empty_evidence_routes_to_writer_without_verification():
    model = scripted_model({EvidenceExtraction: EvidenceExtraction(coverage_sufficient=False)})
    graph = graph_service(model).build_graph()
    updates = list(graph.stream(initial_state(ResearchRequest(query=SAMPLE_QUERY))))
    assert [next(iter(event)) for event in updates] == ["planner", "search", "evidence", "writer"]
    report = updates[-1]["writer"]["report"]
    assert report.step_status["verification"] == "skipped"
    assert report.status == "insufficient_evidence"


@pytest.mark.parametrize("verdict", ["contradicted", "insufficient_evidence"])
def test_non_supported_claims_are_not_published_as_findings(verdict):
    def verify(messages):
        payload = json.loads(messages[-1].content)
        return VerificationOutput(results=[ClaimVerification(
            claim_id=claim["claim_id"], claim=claim["claim"], verdict=verdict,
            evidence_ids=[item["evidence_id"] for item in payload["evidence"]], rationale="Mocked verification outcome.",
            passage_assessments=[PassageAssessment(evidence_id=item["evidence_id"],
                relation="contradicts" if verdict == "contradicted" else "irrelevant", explanation="Scripted verdict.")
                for item in payload["evidence"]],
        ) for claim in payload["claims"]])

    report = graph_service(scripted_model({VerificationOutput: verify})).run(ResearchRequest(query=SAMPLE_QUERY))
    assert report.citations == {}
    assert report.status == "insufficient_evidence"
    assert all(claim.verdict == verdict for claim in report.claims)
    assert not any(line.startswith("> ") for line in report.body_markdown.splitlines())
    assert "## Unresolved claims" in report.body_markdown


def test_one_source_remains_insufficient_after_verification():
    report = graph_service().run(ResearchRequest(query=SAMPLE_QUERY, max_sources=1))
    assert len(report.sources) == len(report.citations) == 1
    assert report.status == "insufficient_evidence"


def test_unrelated_mock_question_does_not_claim_adequate_research():
    report = graph_service().run(ResearchRequest(query="What causes ocean tides?"))
    assert report.query == "What causes ocean tides?"
    assert report.research_plan.query == report.query
    assert report.status == "insufficient_evidence"
    assert any("does not adequately cover" in item for item in report.uncertainties)


def test_routes_follow_explicit_output_presence():
    state = initial_state(ResearchRequest(query=SAMPLE_QUERY))
    assert after_planner(state) == after_search(state) == after_evidence(state) == "writer"
    state = graph_service().run_state(ResearchRequest(query=SAMPLE_QUERY))
    assert after_planner(state) == "search"
    assert after_search(state) == "evidence"
    assert after_evidence(state) == "verification"


@pytest.mark.parametrize(("step", "route"), [("planner", after_planner), ("search", after_search), ("evidence", after_evidence)])
def test_failed_steps_do_not_route_forward_using_stale_data(step, route):
    state = graph_service().run_state(ResearchRequest(query=SAMPLE_QUERY))
    state["step_status"][step] = "failed"
    assert route(state) == "writer"


def test_compiled_graph_has_one_controlled_back_edge_and_no_parallel_edges():
    graph = graph_service().build_graph().get_graph()
    order = {name: index for index, name in enumerate(["__start__", "planner", "search", "evidence", "verification", "retry", "writer", "__end__"])}
    backward = [(edge.source, edge.target) for edge in graph.edges if order[edge.source] >= order[edge.target]]
    assert backward == [("retry", "search")]
    # Any alternate destination is a conditional branch, never a fan-out edge.
    for step in STEPS:
        unconditional = [edge for edge in graph.edges if edge.source == step and not edge.conditional]
        assert len(unconditional) <= 1


def test_iteration_guard_prevents_another_round():
    state = initial_state(ResearchRequest(query=SAMPLE_QUERY))
    state["iteration"] = 1
    model = Mock()
    output = graph_service(model).build_graph().invoke(state, config={"recursion_limit": 12})
    assert output["iteration"] == 1
    assert output["report"].status == "insufficient_evidence"
    assert output["errors"][0].kind == "IterationLimit"
    model.with_structured_output.assert_not_called()


def test_mermaid_diagram_is_generated_from_actual_graph():
    diagram = graph_service().build_graph().get_graph().draw_mermaid()
    assert "graph TD" in diagram
    assert "__start__ --> planner" in diagram
    assert "verification -.-> writer" in diagram
    assert "retry -.-> search" in diagram
    assert "writer --> __end__" in diagram
    assert "planner -.-> writer" in diagram


@pytest.mark.parametrize("workflow", ["graph", "single"])
def test_cli_supports_graph_and_original_phase_two_workflow(monkeypatch, capsys, workflow):
    monkeypatch.setenv("MOCK_MODE", "true")
    assert cli.main(["research", SAMPLE_QUERY, "--workflow", workflow, "--format", "json"]) == 0
    report = ResearchReport.model_validate_json(capsys.readouterr().out)
    assert report.status == "complete"
    assert {source.source_id for source in report.sources} == {source.source_id for source in sample_sources()}
    if workflow == "graph":
        assert report.research_iteration == 1
        assert report.research_plan is not None
        assert all(status == "completed" for step, status in report.step_status.items() if step != "retry")
    else:
        assert report.research_iteration == 0
        assert report.research_plan is None
        assert report.step_status == {}

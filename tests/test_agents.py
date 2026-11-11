from copy import deepcopy
from unittest.mock import MagicMock, Mock

import pytest
from pydantic import ValidationError

from deepresearch_ai.agents.evidence import EvidenceAgent
from deepresearch_ai.agents.planner import PlannerAgent
from deepresearch_ai.agents.search import SearchAgent
from deepresearch_ai.agents.verification import VerificationAgent
from deepresearch_ai.agents.writer import WriterAgent
from deepresearch_ai.config import Settings
from deepresearch_ai.graph.state import initial_state
from deepresearch_ai.models import ClaimVerification, EvidenceItem, ResearchReport, ResearchRequest, SubQuestion
from deepresearch_ai.models.research import PassageAssessment
from deepresearch_ai.models.agents import (
    EvidenceCandidate, EvidenceExtraction, PlannerOutput, ResearchClaim, VerificationOutput,
)
from deepresearch_ai.services.mock_data import SAMPLE_QUERY, sample_sources
from deepresearch_ai.services.mock_llm import MockChatModel
from deepresearch_ai.services.retrieval import RetrievalError
from deepresearch_ai.services.search import MockWebSearch, SearchError


def response_model(response=None, error=None):
    model = Mock()
    runner = model.with_structured_output.return_value
    if error:
        runner.invoke.side_effect = error
    else:
        runner.invoke.return_value = response
    return model


def update(state, changes):
    return {**state, **changes}


@pytest.fixture
def settings():
    return Settings(mock_mode=True)


@pytest.fixture
def planned_state():
    state = initial_state(ResearchRequest(query=SAMPLE_QUERY))
    return update(state, PlannerAgent(MockChatModel())(state))


@pytest.fixture
def sourced_state(settings, planned_state):
    return update(planned_state, SearchAgent(settings, MockWebSearch())(planned_state))


@pytest.fixture
def evidence_state(sourced_state):
    return update(sourced_state, EvidenceAgent(MockChatModel())(sourced_state))


@pytest.fixture
def verified_state(evidence_state):
    return update(evidence_state, VerificationAgent(MockChatModel())(evidence_state))


def test_planner_creates_structured_plan_and_deterministic_question_ids():
    state = initial_state(ResearchRequest(query=SAMPLE_QUERY))
    original = deepcopy(state)
    changes = PlannerAgent(MockChatModel())(state)
    assert changes["plan"].query == SAMPLE_QUERY
    assert [question.question_id for question in changes["subquestions"]] == ["sq_1", "sq_2", "sq_3"]
    assert changes["iteration"] == 1
    assert changes["step_status"]["planner"] == "completed"
    assert state == original
    assert "sources" not in changes and "report" not in changes


@pytest.mark.parametrize("questions", [["one"], ["one", " ONE ", "three"], ["x" * 301, "two", "three"]])
def test_planner_output_rejects_invalid_question_sets(questions):
    with pytest.raises(ValidationError):
        PlannerOutput(strategy="Inspect sources.", subquestions=questions)


def test_planner_failure_is_sanitized():
    state = initial_state(ResearchRequest(query=SAMPLE_QUERY))
    changes = PlannerAgent(response_model(error=RuntimeError("secret-key-value")))(state)
    assert changes["plan"] is None
    assert changes["iteration"] == 1
    assert changes["step_status"]["planner"] == "failed"
    assert "secret-key-value" not in changes["errors"][0].model_dump_json()


def test_planner_iteration_limit_does_not_call_model(planned_state):
    model = Mock()
    changes = PlannerAgent(model)(planned_state)
    assert changes["plan"] is None
    assert changes["errors"][-1].kind == "IterationLimit"
    model.with_structured_output.assert_not_called()


def test_search_deduplicates_queries_and_sources_without_mutating_input(settings, planned_state):
    state = deepcopy(planned_state)
    state["subquestions"] = [
        SubQuestion(question_id="sq_1", question="RAG evidence"),
        SubQuestion(question_id="sq_2", question=" rag   EVIDENCE "),
        SubQuestion(question_id="sq_3", question="RAG limitations"),
    ]
    original = deepcopy(state)
    provider = Mock()
    provider.search.return_value = sample_sources() * 2
    retriever = MagicMock()
    retriever.__enter__.return_value = retriever
    retriever.fetch.side_effect = lambda source: source
    changes = SearchAgent(settings, provider, lambda: retriever)(state)
    assert provider.search.call_count == 2
    assert retriever.fetch.call_count == 2
    assert len(changes["sources"]) == 2
    assert changes["page_attempts"] == 2
    assert state == original


def test_search_does_not_repeat_previous_queries_or_refetch_sources(settings, sourced_state):
    provider = Mock()
    retriever_factory = Mock()
    changes = SearchAgent(settings, provider, retriever_factory)(sourced_state)
    provider.search.assert_not_called()
    retriever_factory.assert_not_called()
    assert changes["sources"] == sourced_state["sources"]


def test_search_respects_global_page_attempt_budget(planned_state):
    state = deepcopy(planned_state)
    state["page_attempts"] = 1
    retriever = MagicMock()
    retriever.__enter__.return_value = retriever
    retriever.fetch.side_effect = lambda source: source
    changes = SearchAgent(Settings(mock_mode=True, research_max_pages=2,
                                   research_max_page_attempts=2), MockWebSearch(), lambda: retriever)(state)
    assert retriever.fetch.call_count == 1
    assert changes["page_attempts"] == 2


def test_search_skips_retrieval_when_page_budget_is_exhausted(settings, planned_state):
    state = deepcopy(planned_state)
    state["page_attempts"] = settings.research_max_page_attempts
    factory = Mock()
    changes = SearchAgent(settings, MockWebSearch(), factory)(state)
    factory.assert_not_called()
    assert changes["sources"] == []


def test_search_provider_failures_keep_safe_errors(settings, planned_state):
    provider = Mock()
    provider.search.side_effect = SearchError("sensitive-provider-details")
    changes = SearchAgent(settings, provider)(planned_state)
    assert changes["step_status"]["search"] == "failed"
    assert len(changes["errors"]) == 3
    assert changes["sources"] == []
    assert all("sensitive-provider-details" not in error.model_dump_json() for error in changes["errors"])


def test_search_handles_partial_retrieval_without_using_failed_snippet(settings, planned_state):
    retriever = MagicMock()
    retriever.__enter__.return_value = retriever
    retriever.fetch.side_effect = [RetrievalError("blocked"), sample_sources()[1]]
    changes = SearchAgent(settings, MockWebSearch(), lambda: retriever)(planned_state)
    assert changes["step_status"]["search"] == "partial"
    assert changes["sources"] == sample_sources()[1:]
    assert changes["page_attempts"] == 2


def test_evidence_accepts_only_source_owned_verbatim_passages(sourced_state):
    original = deepcopy(sourced_state)
    changes = EvidenceAgent(MockChatModel())(sourced_state)
    assert len(changes["evidence"]) == len(changes["claims"]) == 2
    for item in changes["evidence"]:
        source = next(source for source in sourced_state["sources"] if source.source_id == item.source_id)
        assert item.excerpt in source.content
    assert sourced_state == original


@pytest.mark.parametrize("kind", ["unknown_source", "fabricated_passage", "wrong_source"])
def test_evidence_rejects_fabricated_or_mismatched_support(sourced_state, kind):
    source = sourced_state["sources"][0]
    source_id = "src_" + "0" * 64 if kind == "unknown_source" else source.source_id
    excerpt = source.content
    if kind == "fabricated_passage":
        excerpt = "A fabricated passage that cannot be found in the document."
    elif kind == "wrong_source":
        excerpt = sourced_state["sources"][1].content
    extracted = EvidenceExtraction(coverage_sufficient=True, candidates=[
        EvidenceCandidate(claim="A claim to check.", source_id=source_id, excerpt=excerpt),
    ])
    changes = EvidenceAgent(response_model(extracted))(sourced_state)
    assert changes["evidence"] == changes["claims"] == []
    assert changes["rejected_evidence"]
    assert changes["step_status"]["evidence"] == "failed"


def test_evidence_deduplicates_passages_and_merges_claim_support(sourced_state):
    candidates = [EvidenceCandidate(
        claim="RAG uses external context.", source_id=source.source_id, excerpt=source.content,
    ) for source in sourced_state["sources"]]
    candidates.append(candidates[0])
    changes = EvidenceAgent(response_model(EvidenceExtraction(
        coverage_sufficient=True, candidates=candidates,
    )))(sourced_state)
    assert len(changes["evidence"]) == 2
    assert len(changes["claims"]) == 1
    assert len(changes["claims"][0].evidence_ids) == 2


def test_evidence_failure_is_reportable_and_does_not_publish_claims(sourced_state):
    changes = EvidenceAgent(response_model(error=RuntimeError("sensitive-error")))(sourced_state)
    assert changes["evidence"] == changes["claims"] == []
    assert changes["step_status"]["evidence"] == "failed"


@pytest.mark.parametrize("verdict", ["supported", "contradicted", "insufficient_evidence"])
def test_verification_returns_all_three_structured_verdicts(evidence_state, verdict):
    results = [ClaimVerification(
        claim_id=claim.claim_id, claim=claim.claim, verdict=verdict,
        rationale="A mocked evidence-relative judgment.", evidence_ids=[item.evidence_id for item in evidence_state["evidence"]],
        passage_assessments=[PassageAssessment(evidence_id=item.evidence_id,
            relation={"supported": "supports", "contradicted": "contradicts", "insufficient_evidence": "irrelevant"}[verdict],
            explanation="Scripted assessment.") for item in evidence_state["evidence"]],
    ) for claim in evidence_state["claims"]]
    original = deepcopy(evidence_state)
    changes = VerificationAgent(response_model(VerificationOutput(results=results)))(evidence_state)
    assert all(result.verdict == verdict for result in changes["verification_results"])
    assert changes["step_status"]["verification"] == "completed"
    assert evidence_state == original


@pytest.mark.parametrize("kind", ["unknown_claim", "changed_claim", "unknown_evidence", "duplicate_claim"])
def test_verification_rejects_invalid_references_and_claim_changes(evidence_state, kind):
    claim = evidence_state["claims"][0]
    result = ClaimVerification(
        claim_id="claim_unknown" if kind == "unknown_claim" else claim.claim_id,
        claim="A changed claim." if kind == "changed_claim" else claim.claim,
        verdict="supported", rationale="Mocked result.",
        evidence_ids=["ev_unknown"] if kind == "unknown_evidence" else claim.evidence_ids,
    )
    results = [result, result] if kind == "duplicate_claim" else [result]
    changes = VerificationAgent(response_model(VerificationOutput(results=results)))(evidence_state)
    assert changes["step_status"]["verification"] == "partial"
    assert all(result.verdict == "insufficient_evidence" for result in changes["verification_results"])
    assert all(reference != "ev_unknown" for result in changes["verification_results"] for reference in result.evidence_ids)


def test_missing_verification_never_becomes_supported(evidence_state):
    changes = VerificationAgent(response_model(VerificationOutput(results=[])))(evidence_state)
    assert len(changes["verification_results"]) == len(evidence_state["claims"])
    assert all(result.verdict == "insufficient_evidence" for result in changes["verification_results"])


def test_verification_model_failure_fails_closed(evidence_state):
    changes = VerificationAgent(response_model(error=RuntimeError("secret-error")))(evidence_state)
    assert changes["step_status"]["verification"] == "failed"
    assert all(result.verdict == "insufficient_evidence" for result in changes["verification_results"])
    assert "secret-error" not in changes["errors"][-1].model_dump_json()


def test_verification_revalidates_input_evidence_membership(evidence_state):
    state = deepcopy(evidence_state)
    state["evidence"][0] = EvidenceItem(
        evidence_id="ev_1", source_id=state["sources"][0].source_id,
        excerpt="A forged quote not present in the source text.",
    )
    changes = VerificationAgent(MockChatModel())(state)
    assert all(item.evidence_id != "ev_1" for item in changes["evidence"])
    assert any(error.kind == "InvalidEvidence" for error in changes["errors"])


def test_writer_publishes_supported_evidence_with_uncertainty_and_stable_ids(settings, verified_state):
    original = deepcopy(verified_state)
    changes = WriterAgent(settings)(verified_state)
    report = changes["report"]
    assert report.status == "complete"
    assert "## Uncertainty" in report.body_markdown
    assert report.uncertainties
    assert set(report.citations.values()) == {source.source_id for source in report.sources}
    assert all(status == "completed" for step, status in report.step_status.items() if step != "retry")
    assert ResearchReport.model_validate_json(report.model_dump_json()) == report
    assert verified_state == original


@pytest.mark.parametrize("verdict", ["contradicted", "insufficient_evidence"])
def test_writer_excludes_unsupported_claims_and_passages(settings, verified_state, verdict):
    state = deepcopy(verified_state)
    result = state["verification_results"][0]
    state["verification_results"][0] = result.model_copy(update={"verdict": verdict})
    report = WriterAgent(settings)(state)["report"]
    assert report.status == "insufficient_evidence"
    assert state["evidence"][0].source_id not in report.citations.values()
    assert all(item.excerpt not in report.body_markdown for item in state["evidence"][:1])
    assert any(verdict in message for message in report.uncertainties)


def test_writer_rejects_unknown_verification_references(settings, verified_state):
    state = deepcopy(verified_state)
    state["verification_results"][0] = state["verification_results"][0].model_copy(update={"evidence_ids": ["ev_invented"]})
    report = WriterAgent(settings)(state)["report"]
    assert "ev_invented" not in report.model_dump_json()
    assert report.status == "insufficient_evidence"


def test_writer_handles_missing_inputs_and_marks_unvisited_nodes_skipped(settings):
    state = initial_state(ResearchRequest(query=SAMPLE_QUERY))
    report = WriterAgent(settings)(state)["report"]
    assert report.status == "insufficient_evidence"
    assert report.citations == {}
    assert report.step_status["planner"] == "skipped"
    assert report.step_status["writer"] == "completed"

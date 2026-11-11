import hashlib

import pytest
from pydantic import ValidationError

from deepresearch_ai.models import (
    ClaimVerification,
    EvidenceItem,
    ResearchPlan,
    ResearchReport,
    ResearchRequest,
    SourceDocument,
    SubQuestion,
)


@pytest.fixture
def source():
    return SourceDocument(
        original_url="https://example.com/study?edition=1#results",
        title="Example study",
        content="The measured value was 42.",
    )


@pytest.fixture
def report(source):
    evidence = EvidenceItem(source_id=source.source_id, excerpt=source.content)
    claim = ClaimVerification(
        claim="The measured value was 42.",
        verdict="supported",
        rationale="The source explicitly reports this value.",
        evidence_ids=[evidence.evidence_id],
    )
    return ResearchReport(
        query="What was measured?",
        title="Example findings",
        body_markdown="The measured value was 42. [1]",
        sources=[source],
        evidence=[evidence],
        claims=[claim],
        citations={"1": source.source_id},
        limitations=["Only one example source was examined."],
    )


def test_request_strips_query_whitespace():
    assert ResearchRequest(query="  Research citations  ").query == "Research citations"


@pytest.mark.parametrize("query", ["", "  ", "x" * 2001])
def test_invalid_query_is_rejected(query):
    with pytest.raises(ValidationError):
        ResearchRequest(query=query)


@pytest.mark.parametrize("max_sources", [0, 21, True, 2.5])
def test_invalid_source_budget_is_rejected(max_sources):
    with pytest.raises(ValidationError):
        ResearchRequest(query="Research citations", max_sources=max_sources)


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError, match="Extra inputs"):
        ResearchRequest(query="Research citations", max_source=3)


def test_plan_contains_unique_identified_questions():
    plan = ResearchPlan(
        query="Research citations",
        strategy="Inspect primary sources.",
        sub_questions=[SubQuestion(question="What counts as evidence?")],
    )
    assert plan.sub_questions[0].question_id.startswith("sq_")


def test_plan_rejects_empty_question_list():
    with pytest.raises(ValidationError):
        ResearchPlan(query="Research citations", strategy="Inspect sources.", sub_questions=[])


def test_plan_rejects_duplicate_question_ids():
    question = SubQuestion(question="What counts as evidence?")
    with pytest.raises(ValidationError, match="IDs must be unique"):
        ResearchPlan(query="Citations", strategy="Inspect sources.", sub_questions=[question, question])


def test_source_id_is_stable_across_content_changes_and_preserves_url(source):
    refreshed = SourceDocument(
        original_url=source.original_url, title="Updated study", content="Updated text."
    )
    digest = hashlib.sha256(source.original_url.encode("utf-8")).hexdigest()
    assert source.source_id == refreshed.source_id == f"src_{digest}"
    assert source.original_url == "https://example.com/study?edition=1#results"
    assert source.retrieved_at.utcoffset().total_seconds() == 0


def test_exact_url_spelling_is_preserved():
    url = "https://EXAMPLE.com:443/study?q=a%20b#Results"
    source = SourceDocument(original_url=url, title="Study", content="Text")
    assert source.original_url == url


def test_distinct_urls_have_distinct_ids(source):
    other = SourceDocument(original_url="https://example.com/other", title="Other", content="Text")
    assert source.source_id != other.source_id


def test_source_rejects_spoofed_id(source):
    data = source.model_dump()
    data["source_id"] = "src_" + "0" * 64
    with pytest.raises(ValidationError, match="must match the SHA-256"):
        SourceDocument.model_validate(data)


def test_source_identity_is_frozen(source):
    with pytest.raises(ValidationError, match="frozen"):
        source.original_url = "https://example.com/changed"


@pytest.mark.parametrize("url", ["not-a-url", "ftp://example.com/data", " https://example.com"])
def test_invalid_original_url_is_rejected(url):
    with pytest.raises(ValidationError):
        SourceDocument(original_url=url, title="Title", content="Text")


def test_retrieval_timestamp_must_be_timezone_aware(source):
    data = source.model_dump()
    data["retrieved_at"] = "2026-01-01T12:00:00"
    with pytest.raises(ValidationError):
        SourceDocument.model_validate(data)


def test_evidence_requires_a_valid_source_id():
    with pytest.raises(ValidationError):
        EvidenceItem(excerpt="An excerpt.")
    with pytest.raises(ValidationError):
        EvidenceItem(source_id="random", excerpt="An excerpt.")


@pytest.mark.parametrize("verdict", ["supported", "contradicted"])
def test_conclusive_verdict_requires_evidence(verdict):
    with pytest.raises(ValidationError, match="must reference evidence"):
        ClaimVerification(claim="A claim.", verdict=verdict, rationale="A rationale.")


def test_insufficient_evidence_verdict_can_have_no_evidence():
    claim = ClaimVerification(
        claim="A claim.", verdict="insufficient_evidence", rationale="No source available."
    )
    assert claim.evidence_ids == []


def test_unknown_verdict_is_rejected():
    with pytest.raises(ValidationError):
        ClaimVerification(claim="A claim.", verdict="probably", rationale="Uncertain.")


def test_claim_rejects_duplicate_evidence_references():
    with pytest.raises(ValidationError, match="references must be unique"):
        ClaimVerification(
            claim="A claim.", verdict="supported", rationale="Source says so.",
            evidence_ids=["ev_1", "ev_1"],
        )


def test_report_json_round_trip_preserves_references(report):
    restored = ResearchReport.model_validate_json(report.model_dump_json())
    assert restored == report
    assert restored.citations["1"] == restored.sources[0].source_id
    assert restored.claims[0].evidence_ids == [restored.evidence[0].evidence_id]


@pytest.mark.parametrize("field", ["sources", "evidence", "claims"])
def test_report_rejects_duplicate_ids(report, field):
    data = report.model_dump()
    data[field].append(data[field][0])
    with pytest.raises(ValidationError, match="IDs must be unique"):
        ResearchReport.model_validate(data)


@pytest.mark.parametrize("reference_type", ["evidence_source", "claim_evidence", "citation_source", "inline_label"])
def test_report_rejects_dangling_references(report, reference_type):
    data = report.model_dump()
    if reference_type == "evidence_source":
        data["evidence"][0]["source_id"] = "src_" + "0" * 64
    elif reference_type == "claim_evidence":
        data["claims"][0]["evidence_ids"] = ["ev_missing"]
    elif reference_type == "citation_source":
        data["citations"]["1"] = "src_" + "0" * 64
    else:
        data["body_markdown"] = "Unresolved reference. [2]"
    with pytest.raises(ValidationError, match="must (reference|have)"):
        ResearchReport.model_validate(data)


@pytest.mark.parametrize("label", ["0", "01", "abc"])
def test_report_rejects_invalid_citation_labels(report, label):
    data = report.model_dump()
    data["citations"] = {label: report.sources[0].source_id}
    with pytest.raises(ValidationError):
        ResearchReport.model_validate(data)


def test_report_can_express_no_findings():
    report = ResearchReport(
        query="An unanswered question", title="No findings",
        body_markdown="Insufficient evidence to answer.",
        limitations=["No reliable sources were available."],
    )
    assert report.citations == {}

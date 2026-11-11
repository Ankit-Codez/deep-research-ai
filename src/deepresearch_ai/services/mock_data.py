"""Fixed demo fixtures, not downloaded pages or newly researched findings.

Text is an authored sample summary inspired by the linked sources, not a
quotation from those pages. All timestamps and record IDs are deliberately fixed.
"""

from datetime import datetime, timezone

from pydantic import BaseModel

from deepresearch_ai.models import (
    ClaimVerification,
    EvidenceItem,
    ResearchPlan,
    ResearchReport,
    ResearchRequest,
    SourceDocument,
    SubQuestion,
)

SAMPLE_QUERY = "How does retrieval-augmented generation ground an LLM response?"
SAMPLE_TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def sample_sources() -> list[SourceDocument]:
    """Return fresh schema-validated records so callers cannot mutate fixtures."""
    return [
        SourceDocument(
            original_url="https://arxiv.org/abs/2005.11401",
            title="Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
            content=(
                "Retrieval-augmented generation combines a language model with "
                "retrieved passages from an external document index."
            ),
            retrieved_at=SAMPLE_TIMESTAMP,
        ),
        SourceDocument(
            original_url="https://docs.langchain.com/oss/python/deepagents/retrieval",
            title="Retrieval - LangChain documentation",
            content=(
                "A retrieval pipeline fetches relevant external information at "
                "query time and supplies it as context for generation."
            ),
            retrieved_at=SAMPLE_TIMESTAMP,
        ),
    ]


def sample_plan() -> ResearchPlan:
    return ResearchPlan(
        query=SAMPLE_QUERY,
        strategy="Compare the RAG paper with retrieval documentation (fixed mock example).",
        sub_questions=[
            SubQuestion(question_id="sq_mock_1", question="What does retrieval add to generation?"),
            SubQuestion(question_id="sq_mock_2", question="When is external context retrieved?"),
        ],
    )


def sample_report() -> ResearchReport:
    """A static report fixture, not an implementation of research generation."""
    sources = sample_sources()
    evidence = [
        EvidenceItem(
            evidence_id=f"ev_mock_{index}",
            source_id=source.source_id,
            excerpt=source.content,
            note="Excerpt from local sample text; not a live page quotation.",
        )
        for index, source in enumerate(sources, start=1)
    ]
    claim = ClaimVerification(
        claim_id="claim_mock_1",
        claim="RAG supplies retrieved external passages as context for generation.",
        verdict="supported",
        rationale="Both local sample passages describe retrieval supplying generation context.",
        evidence_ids=[item.evidence_id for item in evidence],
    )
    return ResearchReport(
        query=SAMPLE_QUERY,
        title="Mock example: retrieval-augmented generation",
        body_markdown=(
            "MOCK SAMPLE: RAG combines generation with passages retrieved from an "
            "external index. [1] Retrieval provides relevant context at query time. [2]"
        ),
        sources=sources,
        evidence=evidence,
        claims=[claim],
        citations={str(index): source.source_id for index, source in enumerate(sources, start=1)},
        limitations=[
            "Fixed demonstration data; no web requests or research were performed.",
            "Source text is authored sample content, not a live snapshot or quotation.",
        ],
    )


def sample_response(schema: type[BaseModel]) -> BaseModel:
    """Use exactly the same Pydantic contracts as real structured responses."""
    report = sample_report()
    plan = sample_plan()
    responses = {
        ResearchRequest: ResearchRequest(query=SAMPLE_QUERY, max_sources=2),
        ResearchPlan: plan,
        SubQuestion: plan.sub_questions[0],
        SourceDocument: report.sources[0],
        EvidenceItem: report.evidence[0],
        ClaimVerification: report.claims[0],
        ResearchReport: report,
    }
    if schema not in responses:
        raise ValueError("Mock structured output supports only the seven project research models.")
    return responses[schema]

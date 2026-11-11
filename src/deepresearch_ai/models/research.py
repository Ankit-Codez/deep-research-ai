"""Validated research contracts and citation integrity, without workflow logic."""

import hashlib
import re
from datetime import date, datetime, timezone
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SourceId = Annotated[str, StringConstraints(pattern=r"^src_[a-f0-9]{64}$")]
CitationLabel = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]*$")]


class ResearchModel(BaseModel):
    """Reject misspelled fields instead of silently dropping research data."""

    model_config = ConfigDict(extra="forbid")


class ResearchRequest(ResearchModel):
    query: Text = Field(max_length=2000)
    max_sources: int = Field(default=5, ge=1, le=20, strict=True)
    instructions: Text | None = Field(default=None, max_length=2000)


class SubQuestion(ResearchModel):
    question_id: Text = Field(default_factory=lambda: f"sq_{uuid4().hex}")
    question: Text


class ResearchPlan(ResearchModel):
    query: Text
    strategy: Text
    sub_questions: list[SubQuestion] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_question_ids(self) -> Self:
        ids = [question.question_id for question in self.sub_questions]
        if len(ids) != len(set(ids)):
            raise ValueError("Sub-question IDs must be unique within a plan.")
        return self


class SourceDocument(ResearchModel):
    """Preserve the supplied HTTP(S) URL exactly; hash it for a stable ID.

    Different original URL spellings produce different IDs. The search service
    deduplicates canonical URLs while retaining the first original URL and ID.
    Content updates at the same original URL retain the same ID.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    original_url: str = Field(min_length=1)
    resolved_url: str | None = None
    source_id: SourceId
    title: Text
    content: Text
    retrieved_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    published_at: date | None = None
    modified_at: date | None = None

    @model_validator(mode="before")
    @classmethod
    def derive_source_id(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("original_url"), str):
            data = dict(data)
            digest = hashlib.sha256(data["original_url"].encode("utf-8")).hexdigest()
            data.setdefault("source_id", f"src_{digest}")
        return data

    @field_validator("original_url")
    @classmethod
    def validate_original_url(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Original URL must not contain surrounding whitespace.")
        HttpUrl(value)  # Validate HTTP(S) without replacing the original string.
        return value

    @field_validator("resolved_url")
    @classmethod
    def validate_resolved_url(cls, value: str | None) -> str | None:
        return cls.validate_original_url(value) if value is not None else None

    @model_validator(mode="after")
    def validate_source_id(self) -> Self:
        expected = "src_" + hashlib.sha256(
            self.original_url.encode("utf-8")
        ).hexdigest()
        if self.source_id != expected:
            raise ValueError("Source ID must match the SHA-256 of the original URL.")
        return self


class EvidenceItem(ResearchModel):
    evidence_id: Text = Field(default_factory=lambda: f"ev_{uuid4().hex}")
    source_id: SourceId
    excerpt: Text = Field(description="Verbatim supporting or contradicting passage.")
    note: Text | None = None


class PassageAssessment(ResearchModel):
    evidence_id: Text
    relation: Literal["supports", "contradicts", "irrelevant"]
    explanation: Text


class ClaimVerification(ResearchModel):
    claim_id: Text = Field(default_factory=lambda: f"claim_{uuid4().hex}")
    claim: Text
    verdict: Literal["supported", "contradicted", "insufficient_evidence"]
    rationale: Text
    evidence_ids: list[Text] = Field(default_factory=list)
    source_ids: list[SourceId] = Field(default_factory=list)
    supporting_source_ids: list[SourceId] = Field(default_factory=list)
    contradicting_source_ids: list[SourceId] = Field(default_factory=list)
    passage_assessments: list[PassageAssessment] = Field(default_factory=list)
    disagreement: bool = False

    @model_validator(mode="after")
    def require_evidence_for_verdict(self) -> Self:
        if self.verdict != "insufficient_evidence" and not self.evidence_ids:
            raise ValueError("Supported or contradicted claims must reference evidence.")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("Evidence references must be unique within a claim.")
        return self


class ResearchReport(ResearchModel):
    """Markdown uses numeric citations, e.g. [1], resolved through citations.

    Validation establishes referential integrity, not factual correctness.
    Future agents must still check whether evidence actually supports a claim.
    """

    query: Text
    title: Text
    status: Literal["complete", "partial", "insufficient_evidence"] = "complete"
    search_queries: list[Text] = Field(default_factory=list)
    research_plan: ResearchPlan | None = None
    step_status: dict[str, Literal["pending", "completed", "partial", "failed", "skipped"]] = Field(default_factory=dict)
    research_iteration: int = Field(default=0, ge=0, le=11)
    usage: dict[str, int | float | str | None] = Field(default_factory=dict)
    uncertainties: list[Text] = Field(default_factory=list)
    body_markdown: Text
    sources: list[SourceDocument] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    claims: list[ClaimVerification] = Field(default_factory=list)
    citations: dict[CitationLabel, SourceId] = Field(default_factory=dict)
    limitations: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        source_ids = {source.source_id for source in self.sources}
        evidence_ids = {item.evidence_id for item in self.evidence}
        claim_ids = {claim.claim_id for claim in self.claims}
        if len(source_ids) != len(self.sources):
            raise ValueError("Report source IDs must be unique.")
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("Report evidence IDs must be unique.")
        if len(claim_ids) != len(self.claims):
            raise ValueError("Report claim IDs must be unique.")
        if any(item.source_id not in source_ids for item in self.evidence):
            raise ValueError("Every evidence source_id must reference a report source.")
        if any(
            reference not in evidence_ids
            for claim in self.claims
            for reference in claim.evidence_ids
        ):
            raise ValueError("Every claim evidence_id must reference report evidence.")
        if any(source_id not in source_ids for source_id in self.citations.values()):
            raise ValueError("Every citation must reference a report source.")
        evidence_by_id = {item.evidence_id: item for item in self.evidence}
        if any(item.excerpt not in next(source.content for source in self.sources if source.source_id == item.source_id)
               for item in self.evidence):
            raise ValueError("Evidence passages must occur verbatim in recorded source content.")
        for claim in self.claims:
            if any(sid not in source_ids for sid in claim.source_ids):
                raise ValueError("Verification source IDs must reference recorded sources.")
            if claim.source_ids and set(claim.source_ids) != {evidence_by_id[eid].source_id for eid in claim.evidence_ids}:
                raise ValueError("Verification source IDs must match its evidence.")
            if any(item.evidence_id not in claim.evidence_ids for item in claim.passage_assessments):
                raise ValueError("Passage assessments must reference the claim's evidence.")
            for relation, references in (("supports", claim.supporting_source_ids),
                                         ("contradicts", claim.contradicting_source_ids)):
                actual = {evidence_by_id[item.evidence_id].source_id for item in claim.passage_assessments
                          if item.relation == relation}
                if references and set(references) != actual:
                    raise ValueError("Supporting and contradicting sources must match passage assessments.")
        inline_labels = set(re.findall(r"\[([0-9]+)\]", self.body_markdown))
        if inline_labels - self.citations.keys():
            raise ValueError("Every inline citation must have an entry in citations.")
        return self

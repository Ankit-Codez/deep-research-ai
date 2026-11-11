"""Structured contracts between the logical research agents."""

import hashlib
from typing import Literal, Self

from pydantic import Field, model_validator

from deepresearch_ai.models.research import ClaimVerification, ResearchModel, SourceId, Text


class PlannerOutput(ResearchModel):
    strategy: Text = Field(max_length=2000)
    subquestions: list[Text] = Field(min_length=3, max_length=5)

    @model_validator(mode="after")
    def validate_questions(self) -> Self:
        normalized = {" ".join(question.casefold().split()) for question in self.subquestions}
        if len(normalized) != len(self.subquestions):
            raise ValueError("Subquestions must be unique.")
        if any(len(question) > 300 for question in self.subquestions):
            raise ValueError("Subquestions must be at most 300 characters for search.")
        return self


class EvidenceCandidate(ResearchModel):
    claim: Text = Field(max_length=1500)
    source_id: SourceId
    excerpt: Text = Field(min_length=20, max_length=1000)
    priority: Literal["high", "normal"] = "high"
    relevance: Literal["direct", "context", "off_topic"] = Field(default="direct", description=(
        'direct answers the user question; context is a clearly scoped example or background; '
        'off_topic introduces an unrequested side issue and must be excluded.'
    ))


class ClaimNote(ResearchModel):
    claim: Text = Field(max_length=1500)
    priority: Literal["high", "normal"] = "high"
    relevance: Literal["direct", "context", "off_topic"] = Field(default="direct", description=(
        'Only direct gaps essential to answering the user question may have high priority. '
        'Unrequested speculative claims are off_topic, not missing evidence for the main answer.'
    ))


class EvidenceExtraction(ResearchModel):
    coverage_sufficient: bool
    candidates: list[EvidenceCandidate] = Field(default_factory=list, max_length=10)
    notes: list[ClaimNote] = Field(default_factory=list, max_length=10)


class ResearchClaim(ResearchModel):
    claim_id: Text
    claim: Text
    evidence_ids: list[Text] = Field(default_factory=list)
    priority: Literal["high", "normal"] = "high"
    relevance: Literal["direct", "context"] = "direct"


def stable_id(prefix: str, text: str, *, normalize: bool = True) -> str:
    normalized = " ".join(text.casefold().split()) if normalize else text
    return prefix + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class RetryQueries(ResearchModel):
    queries: list[Text] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def bounded_queries(self):
        if any(len(query) > 300 for query in self.queries):
            raise ValueError("Retry queries must be at most 300 characters.")
        return self


class VerificationOutput(ResearchModel):
    # Gemini rejects this nested schema with maxItems=120. Keep the same
    # validation bound locally instead of emitting it into the provider schema.
    results: list[ClaimVerification] = Field(default_factory=list, description=(
        'One result for every supplied claim. Each result must assess EVERY supplied evidence ID, '
        'including irrelevant passages. evidence_ids lists all assessed IDs, not just supporting ones. '
        'Even unsupported claims need the complete passage_assessments checklist.'
    ))

    @model_validator(mode="after")
    def bound_results(self) -> Self:
        if len(self.results) > 120:
            raise ValueError("Verification results must contain at most 120 items.")
        return self

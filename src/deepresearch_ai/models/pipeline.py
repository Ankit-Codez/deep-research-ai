"""Small structured LLM contracts; sources and citation metadata stay in code."""

from typing import Self

from pydantic import Field, model_validator

from deepresearch_ai.models.research import ResearchModel, SourceId, Text


class SearchQueries(ResearchModel):
    queries: list[Text] = Field(min_length=3, max_length=5)

    @model_validator(mode="after")
    def focused_unique_queries(self) -> Self:
        if any(len(query) > 300 for query in self.queries):
            raise ValueError("Search queries must be at most 300 characters.")
        if len({query.casefold() for query in self.queries}) != len(self.queries):
            raise ValueError("Search queries must be unique.")
        return self


class SummaryFinding(ResearchModel):
    source_id: SourceId
    excerpt: Text = Field(min_length=20, max_length=1000)


class ResearchSummary(ResearchModel):
    """An extractive summary: the service validates every passage verbatim."""

    sufficient_evidence: bool
    findings: list[SummaryFinding] = Field(default_factory=list, max_length=10)

"""Typed shared state; credentials and provider clients never enter the graph state."""

from typing import Literal
from typing_extensions import TypedDict

from deepresearch_ai.models import (
    ClaimVerification, EvidenceItem, ResearchPlan, ResearchReport,
    ResearchRequest, SourceDocument, SubQuestion,
)
from deepresearch_ai.models.agents import ResearchClaim
from deepresearch_ai.models.research import ResearchModel, Text

StepName = Literal["planner", "search", "evidence", "verification", "retry", "writer"]
StepStatus = Literal["pending", "completed", "partial", "failed", "skipped"]
STEPS: tuple[StepName, ...] = ("planner", "search", "evidence", "verification", "retry", "writer")
MAX_PLANNING_PASSES = 1


class ResearchUsage(ResearchModel):
    llm_calls: int = 0
    searches: int = 0
    page_requests: int = 0
    reserved_tokens: int = 0
    reported_tokens: int = 0
    usage_reported_calls: int = 0
    estimated_cost_usd: float | None = None
    elapsed_seconds: float = 0
    limit_reason: str | None = None


class ResearchError(ResearchModel):
    step: StepName
    kind: Text
    message: Text


class ResearchState(TypedDict):
    question: str
    request: ResearchRequest
    plan: ResearchPlan | None
    subquestions: list[SubQuestion]
    sources: list[SourceDocument]
    evidence: list[EvidenceItem]
    claims: list[ResearchClaim]
    verification_results: list[ClaimVerification]
    report: ResearchReport | None
    errors: list[ResearchError]
    step_status: dict[StepName, StepStatus]
    iteration: int
    searched_queries: list[str]
    page_attempts: int
    coverage_sufficient: bool
    rejected_evidence: bool
    uncertainties: list[str]
    usage: ResearchUsage
    started_at: float | None
    retry_count: int
    source_retry_count: int
    retry_queries: list[str]
    attempted_urls: list[str]


def initial_state(request: ResearchRequest) -> ResearchState:
    return ResearchState(
        question=request.query, request=request, plan=None, subquestions=[],
        sources=[], evidence=[], claims=[], verification_results=[], report=None,
        errors=[], step_status={step: "pending" for step in STEPS}, iteration=0,
        searched_queries=[], page_attempts=0, coverage_sufficient=False,
        rejected_evidence=False, uncertainties=[],
        usage=ResearchUsage(), started_at=None, retry_count=0, retry_queries=[],
        source_retry_count=0,
        attempted_urls=[],
    )


def statuses(state: ResearchState, step: StepName, status: StepStatus) -> dict[StepName, StepStatus]:
    """Return a replacement mapping instead of mutating nested input state."""
    return {**state["step_status"], step: status}

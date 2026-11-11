"""Public API contracts contain research data, never provider configuration."""
from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator

from deepresearch_ai.models import ResearchReport
from deepresearch_ai.models.research import ResearchModel, Text


class RunStatus(StrEnum):
    queued = 'queued'
    running = 'running'
    completed = 'completed'
    failed = 'failed'
    cancelled = 'cancelled'


class ResearchDepth(StrEnum):
    quick = 'quick'
    standard = 'standard'
    deep = 'deep'


class CreateResearch(ResearchModel):
    topic: Text = Field(max_length=2000)
    instructions: Text | None = Field(default=None, max_length=2000)
    depth: ResearchDepth = ResearchDepth.standard

    @field_validator('instructions', mode='before')
    @classmethod
    def blank_instructions(cls, value):
        return None if isinstance(value, str) and not value.strip() else value


class RunSummary(ResearchModel):
    id: UUID
    topic: str
    instructions: str | None
    depth: ResearchDepth
    status: RunStatus
    current_step: str | None = None
    progress: float = Field(default=0, ge=0, le=1)
    iteration: int = 0
    step_status: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    error_code: str | None = None
    resume_count: int = 0

    @field_validator('created_at', 'updated_at', 'started_at', 'finished_at', mode='after')
    @classmethod
    def sqlite_utc(cls, value):
        return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


class RunDetail(RunSummary):
    report: ResearchReport | None = None
    submission_reused: bool = False
    token_budget: int | None = None
    cost_budget_usd: float | None = None
    cost_rates: dict[str, float] = Field(default_factory=dict)
    resumable: bool = False
    resume_blocked_reason: str | None = None


class RunPage(ResearchModel):
    items: list[RunSummary]
    total: int
    limit: int
    offset: int


class ErrorBody(ResearchModel):
    code: str
    message: str
    details: list[dict[str, str]] = Field(default_factory=list)


class ErrorResponse(ResearchModel):
    error: ErrorBody

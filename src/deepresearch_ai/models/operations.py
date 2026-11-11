"""Administrator responses intentionally omit topics, prompts, sources and users."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from deepresearch_ai.models.research import ResearchModel
from deepresearch_ai.models.runs import ResearchDepth, RunStatus


class OperationsAlert(ResearchModel):
    code: str
    severity: Literal['info','warning']
    message: str


class AdminOverview(ResearchModel):
    counts: dict[RunStatus,int]
    oldest_queued_seconds: float
    failed_last_24h: int
    insufficient_last_24h: int
    unknown_provider_calls: int
    day_start: datetime
    estimated_cost_usd: float | None
    estimated_reserved_exposure_usd: float | None
    unknown_cost_runs: int
    spending_alert_usd: float
    alerts: list[OperationsAlert]


class AdminRun(ResearchModel):
    id: UUID
    status: RunStatus
    depth: ResearchDepth
    current_step: str | None
    progress: float
    created_at: datetime
    updated_at: datetime
    error_code: str | None
    resume_count: int


class AdminRunPage(ResearchModel):
    items: list[AdminRun]
    total: int
    limit: int
    offset: int

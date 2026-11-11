"""Personal accounting responses contain no provider settings or credentials."""
from datetime import datetime

from pydantic import Field, SecretStr

from deepresearch_ai.models.research import ResearchModel


class DeleteAccountRequest(ResearchModel):
    password: SecretStr = Field(min_length=1, max_length=128)
    confirmation: str = Field(pattern=r'^DELETE$')


class UsageTotals(ResearchModel):
    submissions: int
    reported_tokens: int | None
    admitted_tokens: int
    llm_calls: int
    searches: int
    estimated_cost_usd: float | None
    runs_without_reported_usage: int
    runs_without_cost: int
    charged_tokens: int
    reserved_tokens: int
    charged_cost_usd: float | None
    reserved_cost_usd: float | None


class QuotaLimits(ResearchModel):
    daily_runs: int
    daily_tokens: int
    daily_cost_usd: float | None
    max_active_runs: int


class RemainingAllowance(ResearchModel):
    runs: int
    tokens: int
    cost_usd: float | None
    active_slots: int


class UsageDashboard(ResearchModel):
    period_start: datetime
    resets_at: datetime
    active_runs: int
    limits: QuotaLimits
    remaining: RemainingAllowance
    today: UsageTotals
    lifetime: UsageTotals

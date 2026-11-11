"""Normalized relational tables; reports are assembled from these records.

Source identity is separate from immutable retrieval snapshots. A run points to
one snapshot per source, so two users can cite different versions of the same URL.
"""
from datetime import date, datetime

from sqlalchemy import (Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey,
                        ForeignKeyConstraint, Index, Integer, MetaData, Numeric, String, Text, UniqueConstraint, false)
from decimal import Decimal
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        'ix': 'ix_%(table_name)s_%(column_0_name)s',
        'uq': 'uq_%(table_name)s_%(column_0_name)s',
        'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
        'pk': 'pk_%(table_name)s', 'ck': 'ck_%(table_name)s_%(constraint_name)s',
    })


class UserRow(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionRow(Base):
    __tablename__ = 'user_sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class RateLimitRow(Base):
    __tablename__ = 'auth_rate_limits'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer)


class RunRow(Base):
    __tablename__ = 'research_runs'
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','completed','failed','cancelled')", name='valid_status'),
        CheckConstraint("depth IN ('quick','standard','deep')", name='valid_depth'),
        CheckConstraint('progress >= 0 AND progress <= 1', name='valid_progress'),
        Index('ix_runs_user_created', 'user_id', 'created_at'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # NULL is reserved for explicitly imported legacy records, never new API runs.
    user_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    topic: Mapped[str] = mapped_column(Text)
    instructions: Mapped[str | None] = mapped_column(Text)
    depth: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), index=True)
    current_step: Mapped[str | None] = mapped_column(String(32))
    progress: Mapped[float] = mapped_column(Float, default=0)
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    request_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    resume_count: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    error_code: Mapped[str | None] = mapped_column(String(40))


class CheckpointRow(Base):
    """Versioned execution snapshot; domain reports remain normalized separately."""
    __tablename__ = 'research_checkpoints'
    run_id: Mapped[str] = mapped_column(ForeignKey('research_runs.id', ondelete='CASCADE'), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer)
    node: Mapped[str] = mapped_column(String(20))
    phase: Mapped[str] = mapped_column(String(16))
    state_json: Mapped[str] = mapped_column(Text)
    settings_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProviderCallRow(Base):
    """Intent before dispatch, then a typed result or a safe failure receipt."""
    __tablename__ = 'provider_calls'
    __table_args__ = (UniqueConstraint('run_id','sequence','position'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey('research_runs.id', ondelete='CASCADE'), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    result_json: Mapped[str | None] = mapped_column(Text)
    failure_status: Mapped[int | None] = mapped_column(Integer)
    failure_reason: Mapped[str | None] = mapped_column(String(20))
    usage_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SubmissionKeyRow(Base):
    """Durable, user-scoped idempotency keys; deleted results cannot be replayed."""
    __tablename__ = 'submission_keys'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(ForeignKey('research_runs.id', ondelete='SET NULL'), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UsageEntryRow(Base):
    """Numeric accounting survives research deletion, without retaining its topic."""
    __tablename__ = 'usage_entries'
    __table_args__ = (
        CheckConstraint('token_budget >= 0 AND token_charge >= 0', name='nonnegative_tokens'),
        Index('ix_usage_user_period', 'user_id', 'period'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    run_id: Mapped[str | None] = mapped_column(ForeignKey('research_runs.id', ondelete='SET NULL'), unique=True)
    period: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(16))
    settled: Mapped[bool] = mapped_column(Boolean, default=False)
    token_budget: Mapped[int] = mapped_column(Integer)
    token_charge: Mapped[int] = mapped_column(Integer)
    admitted_tokens: Mapped[int | None] = mapped_column(Integer)
    reported_tokens: Mapped[int | None] = mapped_column(Integer)
    llm_calls: Mapped[int | None] = mapped_column(Integer)
    searches: Mapped[int | None] = mapped_column(Integer)
    cost_budget: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    cost_charge: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    input_rate: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    output_rate: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    search_rate: Mapped[Decimal] = mapped_column(Numeric(20, 8))


class StepRow(Base):
    __tablename__ = 'research_steps'
    run_id: Mapped[str] = mapped_column(ForeignKey('research_runs.id', ondelete='CASCADE'), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))


class SourceRow(Base):
    __tablename__ = 'sources'
    source_id: Mapped[str] = mapped_column(String(68), primary_key=True)
    original_url: Mapped[str] = mapped_column(Text, unique=True)


class SourceSnapshotRow(Base):
    __tablename__ = 'source_snapshots'
    __table_args__ = (UniqueConstraint('id', 'source_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey('sources.source_id'), index=True)
    resolved_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[date | None] = mapped_column(Date)
    modified_at: Mapped[date | None] = mapped_column(Date)


class RunSourceRow(Base):
    __tablename__ = 'research_sources'
    __table_args__ = (ForeignKeyConstraint(['snapshot_id', 'source_id'],
        ['source_snapshots.id', 'source_snapshots.source_id']), UniqueConstraint('run_id', 'position'))
    run_id: Mapped[str] = mapped_column(ForeignKey('research_runs.id', ondelete='CASCADE'), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey('sources.source_id'), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(36))
    position: Mapped[int] = mapped_column(Integer)


class ReportRow(Base):
    __tablename__ = 'research_reports'
    run_id: Mapped[str] = mapped_column(ForeignKey('research_runs.id', ondelete='CASCADE'), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    evidence_status: Mapped[str] = mapped_column(String(32))
    body_markdown: Mapped[str] = mapped_column(Text)


class PlanRow(Base):
    __tablename__ = 'research_plans'
    run_id: Mapped[str] = mapped_column(ForeignKey('research_runs.id', ondelete='CASCADE'), primary_key=True)
    strategy: Mapped[str] = mapped_column(Text)


class SubQuestionRow(Base):
    __tablename__ = 'research_subquestions'
    __table_args__ = (UniqueConstraint('run_id', 'position'),)
    run_id: Mapped[str] = mapped_column(ForeignKey('research_plans.run_id', ondelete='CASCADE'), primary_key=True)
    question_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    question: Mapped[str] = mapped_column(Text)


class QueryRow(Base):
    __tablename__ = 'research_queries'
    run_id: Mapped[str] = mapped_column(ForeignKey('research_runs.id', ondelete='CASCADE'), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    query: Mapped[str] = mapped_column(Text)


class EvidenceRow(Base):
    __tablename__ = 'research_evidence'
    __table_args__ = (ForeignKeyConstraint(['run_id', 'source_id'],
        ['research_sources.run_id', 'research_sources.source_id']), UniqueConstraint('run_id', 'position'))
    run_id: Mapped[str] = mapped_column(ForeignKey('research_runs.id', ondelete='CASCADE'), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    source_id: Mapped[str] = mapped_column(String(68))
    excerpt: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class ClaimRow(Base):
    __tablename__ = 'research_claims'
    __table_args__ = (UniqueConstraint('run_id', 'position'),)
    run_id: Mapped[str] = mapped_column(ForeignKey('research_runs.id', ondelete='CASCADE'), primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    claim: Mapped[str] = mapped_column(Text)
    verdict: Mapped[str] = mapped_column(String(32))
    rationale: Mapped[str] = mapped_column(Text)
    disagreement: Mapped[bool] = mapped_column(Boolean)


class ClaimEvidenceRow(Base):
    __tablename__ = 'claim_evidence'
    __table_args__ = (
        ForeignKeyConstraint(['run_id', 'claim_id'], ['research_claims.run_id', 'research_claims.claim_id'], ondelete='CASCADE'),
        ForeignKeyConstraint(['run_id', 'evidence_id'], ['research_evidence.run_id', 'research_evidence.evidence_id']),
    )
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)


class PassageAssessmentRow(Base):
    __tablename__ = 'passage_assessments'
    __table_args__ = (ForeignKeyConstraint(['run_id', 'claim_id', 'evidence_id'],
        ['claim_evidence.run_id', 'claim_evidence.claim_id', 'claim_evidence.evidence_id'], ondelete='CASCADE'),)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    relation: Mapped[str] = mapped_column(String(16))
    explanation: Mapped[str] = mapped_column(Text)


class ClaimSourceRow(Base):
    __tablename__ = 'claim_sources'
    __table_args__ = (
        ForeignKeyConstraint(['run_id', 'claim_id'], ['research_claims.run_id', 'research_claims.claim_id'], ondelete='CASCADE'),
        ForeignKeyConstraint(['run_id', 'source_id'], ['research_sources.run_id', 'research_sources.source_id']),
        CheckConstraint("kind IN ('assessed','supporting','contradicting')", name='valid_kind'),
    )
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(68), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)


class CitationRow(Base):
    __tablename__ = 'report_citations'
    __table_args__ = (ForeignKeyConstraint(['run_id', 'source_id'],
        ['research_sources.run_id', 'research_sources.source_id']),)
    run_id: Mapped[str] = mapped_column(ForeignKey('research_reports.run_id', ondelete='CASCADE'), primary_key=True)
    label: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(68))


class ReportNoteRow(Base):
    __tablename__ = 'report_notes'
    run_id: Mapped[str] = mapped_column(ForeignKey('research_reports.run_id', ondelete='CASCADE'), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)


class MetricRow(Base):
    __tablename__ = 'research_metrics'
    run_id: Mapped[str] = mapped_column(ForeignKey('research_reports.run_id', ondelete='CASCADE'), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    value_kind: Mapped[str] = mapped_column(String(8))

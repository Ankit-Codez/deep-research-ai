"""Delete private research transactionally, preserving other runs' source versions."""
from sqlalchemy import delete, select

from deepresearch_ai.storage.tables import (CitationRow, ClaimEvidenceRow, ClaimRow, ClaimSourceRow,
    EvidenceRow, MetricRow, PassageAssessmentRow, PlanRow, QueryRow, ReportNoteRow, ReportRow,
    RunRow, RunSourceRow, SourceRow, SourceSnapshotRow, StepRow, SubQuestionRow)
from deepresearch_ai.storage.usage import settle


def delete_runs(session, rows):
    """Caller holds run locks. Order respects all composite foreign keys."""
    ids = [row.id for row in rows]
    if not ids:
        return
    source_ids = list(session.scalars(select(RunSourceRow.source_id).where(
        RunSourceRow.run_id.in_(ids)).distinct().order_by(RunSourceRow.source_id)))
    if source_ids:
        # Serialize cleanup with another run linking the same source identity.
        list(session.scalars(select(SourceRow).where(SourceRow.source_id.in_(source_ids))
                            .order_by(SourceRow.source_id).with_for_update()))
    for row in rows:
        if row.status in {'queued', 'running'}:
            settle(session, row.id, 'cancelled', never_started=row.started_at is None)
    session.flush()
    children = [PassageAssessmentRow, ClaimEvidenceRow, ClaimSourceRow, CitationRow,
        ReportNoteRow, MetricRow, SubQuestionRow, QueryRow, ClaimRow, EvidenceRow,
        ReportRow, PlanRow, StepRow, RunSourceRow]
    for model in children:
        session.execute(delete(model).where(model.run_id.in_(ids)))
    # SET NULL on usage/idempotency references preserves accounting and replay safety.
    session.execute(delete(RunRow).where(RunRow.id.in_(ids)))
    if source_ids:
        session.execute(delete(SourceSnapshotRow).where(SourceSnapshotRow.source_id.in_(source_ids),
            ~select(RunSourceRow.run_id).where(RunSourceRow.snapshot_id == SourceSnapshotRow.id).exists()))
        session.execute(delete(SourceRow).where(SourceRow.source_id.in_(source_ids),
            ~select(SourceSnapshotRow.id).where(SourceSnapshotRow.source_id == SourceRow.source_id).exists()))

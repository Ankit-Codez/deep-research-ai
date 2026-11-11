"""Translate public report models to normalized rows within one transaction."""
import hashlib
from datetime import timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from deepresearch_ai.models import ClaimVerification, EvidenceItem, ResearchPlan, ResearchReport, SourceDocument, SubQuestion
from deepresearch_ai.models.research import PassageAssessment
from deepresearch_ai.storage.tables import (CitationRow, ClaimEvidenceRow, ClaimRow, ClaimSourceRow,
    EvidenceRow, MetricRow, PassageAssessmentRow, PlanRow, QueryRow, ReportNoteRow, ReportRow,
    RunSourceRow, SourceRow, SourceSnapshotRow, StepRow, SubQuestionRow)


def insert_if_missing(session, table, values, key_columns):
    insert = pg_insert if session.bind.dialect.name == 'postgresql' else sqlite_insert
    session.execute(insert(table).values(**values).on_conflict_do_nothing(index_elements=key_columns))


def save_steps(session, run_id, steps):
    for name, status in steps.items():
        row = session.get(StepRow, (run_id, name))
        if row: row.status = status
        else: session.add(StepRow(run_id=run_id, name=name, status=status))


def load_steps(session, run_id):
    return {row.name: row.status for row in session.scalars(select(StepRow).where(StepRow.run_id == run_id))}


def save_sources(session, run_id, sources):
    # UPSERT takes a row lock even for an existing identity. Cleanup cannot
    # delete it between conflict detection and the new snapshot's FK insert.
    # Lock identities in the same order as deletion to avoid lock-order cycles.
    insert = pg_insert if session.bind.dialect.name == 'postgresql' else sqlite_insert
    for source in sorted(sources, key=lambda item: item.source_id):
        session.execute(insert(SourceRow).values(source_id=source.source_id, original_url=source.original_url)
            .on_conflict_do_update(index_elements=['source_id'], set_={'source_id':source.source_id}))
    for position, source in enumerate(sources):
        link = session.get(RunSourceRow, (run_id, source.source_id))
        snapshot = session.get(SourceSnapshotRow, link.snapshot_id) if link else None
        # Snapshot dates compare consistently across SQLite's naive UTC and PostgreSQL.
        same = snapshot and snapshot.content == source.content and snapshot.title == source.title and (
            snapshot.resolved_url == source.resolved_url and snapshot.published_at == source.published_at
            and snapshot.modified_at == source.modified_at
            and snapshot.retrieved_at.replace(tzinfo=None) == source.retrieved_at.astimezone(timezone.utc).replace(tzinfo=None))
        if same: continue
        snapshot = SourceSnapshotRow(id=str(uuid4()), source_id=source.source_id,
            resolved_url=source.resolved_url, title=source.title, content=source.content,
            content_hash=hashlib.sha256(source.content.encode()).hexdigest(), retrieved_at=source.retrieved_at.astimezone(timezone.utc),
            published_at=source.published_at, modified_at=source.modified_at)
        session.add(snapshot); session.flush()
        if link: link.snapshot_id = snapshot.id
        else: session.add(RunSourceRow(run_id=run_id, source_id=source.source_id, snapshot_id=snapshot.id, position=position))
    session.flush()


def load_sources(session, run_id):
    rows = session.execute(select(SourceRow, SourceSnapshotRow).join(RunSourceRow,
        RunSourceRow.source_id == SourceRow.source_id).join(SourceSnapshotRow,
        SourceSnapshotRow.id == RunSourceRow.snapshot_id).where(RunSourceRow.run_id == run_id).order_by(RunSourceRow.position))
    return [SourceDocument(source_id=source.source_id, original_url=source.original_url,
        resolved_url=snapshot.resolved_url, title=snapshot.title, content=snapshot.content,
        retrieved_at=snapshot.retrieved_at if snapshot.retrieved_at.tzinfo else snapshot.retrieved_at.replace(tzinfo=timezone.utc),
        published_at=snapshot.published_at, modified_at=snapshot.modified_at) for source, snapshot in rows]


def save_report(session, run_id, report):
    session.add(ReportRow(run_id=run_id, title=report.title, evidence_status=report.status, body_markdown=report.body_markdown))
    save_sources(session, run_id, report.sources)
    save_steps(session, run_id, report.step_status)
    if report.research_plan:
        session.add(PlanRow(run_id=run_id, strategy=report.research_plan.strategy)); session.flush()
        for pos, question in enumerate(report.research_plan.sub_questions):
            session.add(SubQuestionRow(run_id=run_id, question_id=question.question_id, position=pos, question=question.question))
    for pos, query in enumerate(report.search_queries): session.add(QueryRow(run_id=run_id, position=pos, query=query))
    for pos, item in enumerate(report.evidence): session.add(EvidenceRow(run_id=run_id, position=pos, **item.model_dump()))
    for pos, claim in enumerate(report.claims):
        session.add(ClaimRow(run_id=run_id, claim_id=claim.claim_id, position=pos, claim=claim.claim,
            verdict=claim.verdict, rationale=claim.rationale, disagreement=claim.disagreement))
    session.flush()
    for claim in report.claims:
        for pos, evidence_id in enumerate(claim.evidence_ids):
            session.add(ClaimEvidenceRow(run_id=run_id, claim_id=claim.claim_id, evidence_id=evidence_id, position=pos))
        for kind, ids in [('assessed', claim.source_ids), ('supporting', claim.supporting_source_ids), ('contradicting', claim.contradicting_source_ids)]:
            for pos, source_id in enumerate(ids): session.add(ClaimSourceRow(run_id=run_id, claim_id=claim.claim_id, source_id=source_id, kind=kind, position=pos))
    session.flush()
    for claim in report.claims:
        for pos, assessment in enumerate(claim.passage_assessments):
            session.add(PassageAssessmentRow(run_id=run_id, claim_id=claim.claim_id, position=pos, **assessment.model_dump()))
    for label, source_id in report.citations.items(): session.add(CitationRow(run_id=run_id, label=label, source_id=source_id))
    for kind, notes in [('uncertainty', report.uncertainties), ('limitation', report.limitations)]:
        for pos, text in enumerate(notes): session.add(ReportNoteRow(run_id=run_id, kind=kind, position=pos, text=text))
    for name, value in report.usage.items():
        kind = 'null' if value is None else type(value).__name__
        session.add(MetricRow(run_id=run_id, name=name, value=None if value is None else str(value), value_kind=kind))


def load_report(session, run, steps):
    stored = session.get(ReportRow, run.id)
    if stored is None: return None
    def ordered(model):
        statement = select(model).where(model.run_id == run.id)
        if hasattr(model, 'position'): statement = statement.order_by(model.position)
        return list(session.scalars(statement))
    evidence = [EvidenceItem(evidence_id=row.evidence_id, source_id=row.source_id, excerpt=row.excerpt, note=row.note) for row in ordered(EvidenceRow)]
    refs, assessments, source_refs = ordered(ClaimEvidenceRow), ordered(PassageAssessmentRow), ordered(ClaimSourceRow)
    claims = [ClaimVerification(claim_id=row.claim_id, claim=row.claim, verdict=row.verdict, rationale=row.rationale,
        disagreement=row.disagreement,
        evidence_ids=[ref.evidence_id for ref in refs if ref.claim_id == row.claim_id],
        passage_assessments=[PassageAssessment(evidence_id=ref.evidence_id, relation=ref.relation, explanation=ref.explanation) for ref in assessments if ref.claim_id == row.claim_id],
        **{field: [ref.source_id for ref in source_refs if ref.claim_id == row.claim_id and ref.kind == kind]
           for kind, field in [('assessed', 'source_ids'), ('supporting', 'supporting_source_ids'), ('contradicting', 'contradicting_source_ids')]}) for row in ordered(ClaimRow)]
    plan = session.get(PlanRow, run.id)
    notes = ordered(ReportNoteRow)
    parsers = {'int': int, 'float': float, 'str': str, 'null': lambda _: None}
    return ResearchReport(query=run.topic, title=stored.title, status=stored.evidence_status, body_markdown=stored.body_markdown,
        sources=load_sources(session, run.id), evidence=evidence, claims=claims,
        citations={row.label: row.source_id for row in ordered(CitationRow)},
        research_plan=ResearchPlan(query=run.topic, strategy=plan.strategy,
            sub_questions=[SubQuestion(question_id=row.question_id, question=row.question) for row in ordered(SubQuestionRow)]) if plan else None,
        search_queries=[row.query for row in ordered(QueryRow)], step_status=steps, research_iteration=run.iteration,
        uncertainties=[row.text for row in notes if row.kind == 'uncertainty'], limitations=[row.text for row in notes if row.kind == 'limitation'],
        usage={row.name: parsers[row.value_kind](row.value) for row in ordered(MetricRow)})

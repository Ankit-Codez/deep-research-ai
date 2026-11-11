"""PostgreSQL-backed repository; SQLite remains available for isolated tests."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import and_, create_engine, event, func, inspect, or_, select, update, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from deepresearch_ai.models.runs import CreateResearch, RunDetail, RunPage, RunSummary
from deepresearch_ai.models import ResearchReport, SourceDocument
from deepresearch_ai.config import ConfigurationError
from deepresearch_ai.storage.tables import Base, RunRow, UserRow
from deepresearch_ai.storage import usage as accounting
from deepresearch_ai.storage.report_store import load_report, load_sources, load_steps, save_report, save_sources, save_steps


def utcnow():
    return datetime.now(timezone.utc)


def summary(row, session):
    values = {name: getattr(row, name) for name in RunSummary.model_fields if name != 'step_status'}
    return RunSummary(**values, step_status=load_steps(session, row.id))


def detail(row, session):
    from deepresearch_ai.storage.recovery import resume_status
    brief = summary(row, session)
    entry = accounting.entry_for(session, row.id)
    resumable, blocked = resume_status(session,row)
    return RunDetail(**brief.model_dump(), report=load_report(session, row, brief.step_status),
        resumable=resumable, resume_blocked_reason=blocked,
        token_budget=entry.token_budget if entry else None,
        cost_budget_usd=float(entry.cost_budget) if entry and entry.cost_budget is not None else None,
        cost_rates={field:float(getattr(entry, column)) for field,column in (
            ('research_input_cost_per_million','input_rate'), ('research_output_cost_per_million','output_rate'),
            ('research_search_cost_usd','search_rate'))} if entry else {})


class SQLRunRepository:
    """Short independent transactions; no database session spans a provider call.

    Atomic conditional updates claim queued rows and fence cancelled/expired workers.
    SQLAlchemy types and queries are portable; PostgreSQL needs a driver and migrations.
    """
    def __init__(self, database_url: str):
        options = {'pool_pre_ping': True}
        if database_url.startswith('sqlite:'):
            options['connect_args'] = {'check_same_thread': False, 'timeout': 10}
            if database_url in {'sqlite://', 'sqlite:///:memory:'}:
                options['poolclass'] = StaticPool
        self.engine = create_engine(database_url, **options)
        if self.engine.dialect.name == 'sqlite':
            @event.listens_for(self.engine, 'connect')
            def configure_sqlite(connection, _record):
                cursor = connection.cursor()
                cursor.execute('PRAGMA foreign_keys=ON')
                cursor.execute('PRAGMA busy_timeout=10000')
                cursor.close()
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        if self.engine.dialect.name == 'sqlite':
            path = self.engine.url.database
            if path and path != ':memory:':
                Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)
            with self.engine.connect() as connection:
                connection.exec_driver_sql('PRAGMA journal_mode=WAL')
            existing = inspect(self.engine)
            if existing.has_table('research_runs') and 'user_id' not in {item['name'] for item in existing.get_columns('research_runs')}:
                raise ConfigurationError('Legacy SQLite history must be imported; do not modify it in place.')
            Base.metadata.create_all(self.engine)
        else:
            inspector = inspect(self.engine)
            if not inspector.has_table('alembic_version'):
                raise ConfigurationError('PostgreSQL needs migrations. Run: python -m deepresearch_ai.db upgrade')
            from deepresearch_ai.db import current_revision
            with self.engine.connect() as connection:
                version = connection.scalar(text('SELECT version_num FROM alembic_version'))
            if version != current_revision():
                raise ConfigurationError('Database schema is outdated. Run: python -m deepresearch_ai.db upgrade')

    def close(self):
        self.engine.dispose()

    def ping(self):
        with self.sessions() as session:
            session.execute(select(1))

    def create(self, request: CreateResearch, *, user_id: str | None = None,
               settings=None, idempotency_key: str | None = None) -> RunDetail:
        if user_id is not None:
            from deepresearch_ai.config import Settings
            with self.sessions.begin() as session:
                row, reused = accounting.admit(session, request, user_id,
                    settings or Settings(_env_file=None), idempotency_key, utcnow())
                session.flush()
                return detail(row, session).model_copy(update={'submission_reused':reused})
        now = utcnow()
        row = RunRow(id=str(uuid4()), user_id=user_id, topic=request.topic, instructions=request.instructions,
                     depth=request.depth.value, status='queued', created_at=now, updated_at=now,
                     progress=0, iteration=0)
        with self.sessions.begin() as session:
            session.add(row)
            session.flush()
            return detail(row, session)

    def get(self, run_id: str) -> RunDetail | None:
        with self.sessions() as session:
            row = session.get(RunRow, run_id)
            return detail(row, session) if row else None

    def list_runs(self, limit: int, offset: int) -> RunPage:
        with self.sessions() as session:
            total = session.scalar(select(func.count()).select_from(RunRow))
            rows = session.scalars(select(RunRow).order_by(RunRow.created_at.desc(), RunRow.id.desc()).offset(offset).limit(limit))
            return RunPage(items=[summary(row, session) for row in rows], total=total, limit=limit, offset=offset)

    def sources(self, run_id: str) -> list[SourceDocument]:
        with self.sessions() as session:
            return load_sources(session, run_id)

    # User-facing methods scope every SQL query. Trusted worker methods above
    # deliberately remain separate from the API's authorization boundary.
    def get_for_user(self, run_id: str, user_id: str) -> RunDetail | None:
        with self.sessions() as session:
            row = session.scalar(select(RunRow).where(RunRow.id == run_id, RunRow.user_id == user_id))
            return detail(row, session) if row else None

    def list_for_user(self, user_id: str, limit: int, offset: int, *, query='', status=None,
                      depth=None, created_from=None, created_to=None) -> RunPage:
        with self.sessions() as session:
            conditions = [RunRow.user_id == user_id]
            if query.strip():
                # Literal substring matching, including '%' and '_', rather than user wildcards.
                conditions.append(or_(RunRow.topic.icontains(query.strip(), autoescape=True),
                                      RunRow.instructions.icontains(query.strip(), autoescape=True)))
            if status:
                conditions.append(RunRow.status == status)
            if depth:
                conditions.append(RunRow.depth == depth)
            if created_from:
                conditions.append(RunRow.created_at >= created_from)
            if created_to:
                conditions.append(RunRow.created_at < created_to)
            condition = and_(*conditions)
            total = session.scalar(select(func.count()).select_from(RunRow).where(condition))
            rows = session.scalars(select(RunRow).where(condition).order_by(RunRow.created_at.desc(), RunRow.id.desc()).offset(offset).limit(limit))
            return RunPage(items=[summary(row, session) for row in rows], total=total, limit=limit, offset=offset)

    def usage_for_user(self, user_id, settings):
        with self.sessions.begin() as session:
            accounting.lock_user(session, user_id)
            accounting.ensure_historical_entries(session, user_id, settings)
            return accounting.dashboard(session, user_id, settings, utcnow())

    def resume_for_user(self, run_id, user_id, settings):
        import json
        from deepresearch_ai.storage.recovery import resume_status, STATE
        from deepresearch_ai.storage.tables import CheckpointRow
        with self.sessions.begin() as session:
            accounting.lock_user(session,user_id)
            row = session.scalar(select(RunRow).where(RunRow.id == run_id,RunRow.user_id == user_id).with_for_update())
            if row is None:
                raise accounting.AdmissionError('run_not_found','Research run not found.',status=404)
            allowed, blocked = resume_status(session,row)
            if not allowed:
                raise accounting.AdmissionError('resume_blocked',blocked or 'This run has no resumable failed checkpoint.',status=409)
            if row.resume_count >= settings.research_max_resumes:
                raise accounting.AdmissionError('resume_limit','This run reached its resume limit.',status=409)
            checkpoint = session.get(CheckpointRow,run_id)
            if json.loads(checkpoint.settings_json)['mock_mode'] != settings.mock_mode:
                raise accounting.AdmissionError('resume_mode_mismatch','Use the original mock/real mode to resume this run.',status=409)
            STATE.validate_json(checkpoint.state_json)
            active = session.scalar(select(func.count()).select_from(RunRow).where(
                RunRow.user_id == user_id,RunRow.status.in_(['queued','running'])))
            if active >= settings.quota_max_active_runs:
                raise accounting.AdmissionError('active_run_limit','Wait for an active run before resuming.',retry_after=5)
            entry = accounting.entry_for(session,run_id)
            if entry is None:
                raise accounting.AdmissionError('resume_blocked','This legacy run has no saved allowance.',status=409)
            entry.settled, entry.outcome = False,'queued'
            # Failed attempts already retain their full ceiling; resumption reuses it.
            entry.token_charge, entry.cost_charge = entry.token_budget,entry.cost_budget
            row.status, row.owner, row.lease_expires_at = 'queued',None,None
            row.error, row.error_code, row.finished_at = None,None,None
            row.resume_count += 1
            row.updated_at = utcnow()
            session.flush()
            return detail(row,session)

    def delete_for_user(self, run_id, user_id):
        from deepresearch_ai.storage.deletion import delete_runs
        with self.sessions.begin() as session:
            accounting.lock_user(session, user_id)
            row = session.scalar(select(RunRow).where(RunRow.id == run_id,
                RunRow.user_id == user_id).with_for_update())
            if row is None:
                return False
            delete_runs(session, [row])
            return True

    def sources_for_user(self, run_id: str, user_id: str) -> list[SourceDocument]:
        with self.sessions() as session:
            owned = session.scalar(select(RunRow.id).where(RunRow.id == run_id, RunRow.user_id == user_id))
            return load_sources(session, run_id) if owned else []

    def cancel_for_user(self, run_id: str, user_id: str) -> bool:
        return self.cancel(run_id, user_id=user_id)

    def cancel(self, run_id: str, *, user_id: str | None = None) -> bool:
        with self.sessions.begin() as session:
            result = session.execute(update(RunRow).where(RunRow.id == run_id,
                RunRow.status.in_(['queued', 'running']), *([RunRow.user_id == user_id] if user_id else [])).values(status='cancelled', updated_at=utcnow(),
                finished_at=utcnow(), owner=None, lease_expires_at=None).returning(RunRow.started_at)).first()
            if result is not None:
                accounting.settle(session, run_id, 'cancelled', never_started=result.started_at is None)
            return result is not None

    def claim_next(self, owner: str, lease_seconds: int) -> RunDetail | None:
        with self.sessions.begin() as session:
            statement = select(RunRow.id).where(RunRow.status == 'queued').order_by(RunRow.created_at, RunRow.id).limit(1)
            if self.engine.dialect.name == 'postgresql':
                statement = statement.with_for_update(skip_locked=True)
            run_id = session.scalar(statement)
            if run_id is None:
                return None
            now = utcnow()
            changed = session.execute(update(RunRow).where(RunRow.id == run_id, RunRow.status == 'queued')
                .values(status='running', owner=owner, started_at=now, updated_at=now,
                        lease_expires_at=now + timedelta(seconds=lease_seconds)))
            if changed.rowcount != 1:
                return None
            entry = accounting.entry_for(session, run_id)
            if entry:
                entry.outcome = 'running'
            return detail(session.get(RunRow, run_id), session)

    def _owned(self, run_id: str, owner: str):
        return (RunRow.id == run_id, RunRow.status == 'running', RunRow.owner == owner,
                RunRow.lease_expires_at > utcnow())

    def renew(self, run_id: str, owner: str, lease_seconds: int) -> bool:
        with self.sessions.begin() as session:
            changed = session.execute(update(RunRow).where(*self._owned(run_id, owner))
                .values(lease_expires_at=utcnow() + timedelta(seconds=lease_seconds)))
            return changed.rowcount == 1

    def update_progress(self, run_id, owner, step, progress, iteration, step_status, sources, usage=None):
        with self.sessions.begin() as session:
            changed = session.execute(update(RunRow).where(*self._owned(run_id, owner))
                .values(current_step=step, progress=progress, iteration=iteration,
                        updated_at=utcnow()))
            if changed.rowcount != 1:
                return False
            save_steps(session, run_id, step_status)
            save_sources(session, run_id, sources)
            accounting.record_measurements(accounting.entry_for(session, run_id), usage)
            return True

    def finish(self, run_id, owner, report):
        # Revalidate even if a caller used Pydantic's unchecked model_copy/model_construct.
        report = ResearchReport.model_validate_json(report.model_dump_json())
        with self.sessions.begin() as session:
            topic = session.scalar(select(RunRow.topic).where(*self._owned(run_id, owner)))
            if topic is None:
                return False
            if topic != report.query:
                raise ValueError('Report question must match its research run.')
            changed = session.execute(update(RunRow).where(*self._owned(run_id, owner))
                .values(status='completed', progress=1, current_step='writer',
                        iteration=report.research_iteration, finished_at=utcnow(), updated_at=utcnow(),
                        owner=None, lease_expires_at=None))
            if changed.rowcount != 1:
                return False
            save_report(session, run_id, report)
            accounting.settle(session, run_id, 'completed', usage=report.usage)
            return True

    def fail(self, run_id, owner, message, *, code='execution_failed'):
        with self.sessions.begin() as session:
            changed = session.execute(update(RunRow).where(*self._owned(run_id, owner))
                .values(status='failed', error=message, error_code=code, finished_at=utcnow(), updated_at=utcnow(),
                        owner=None, lease_expires_at=None))
            if changed.rowcount == 1:
                accounting.settle(session, run_id, 'failed')
            return changed.rowcount == 1

    def expire_abandoned(self):
        with self.sessions.begin() as session:
            changed = session.execute(update(RunRow).where(RunRow.status == 'running', RunRow.lease_expires_at <= utcnow())
                .values(status='failed', error='Worker lease expired. Resume is available when a safe checkpoint exists.', error_code='worker_interrupted',
                        finished_at=utcnow(), updated_at=utcnow(), owner=None, lease_expires_at=None).returning(RunRow.id)).all()
            for row in changed:
                accounting.settle(session, row.id, 'failed')
            return len(changed)

"""Read-only, repeatable import of the original single-user SQLite history."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update

from deepresearch_ai.models import ResearchReport, SourceDocument
from deepresearch_ai.storage.report_store import save_report, save_sources, save_steps
from deepresearch_ai.storage.sql import utcnow
from deepresearch_ai.storage.tables import RunRow, UserRow


def _date(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def import_sqlite(repository, path: Path) -> int:
    """Import atomically, leaving existing IDs and the input file unchanged.

    Imported runs are deliberately unowned. An administrator must explicitly
    assign them after the intended owner has registered. Interrupted jobs are
    marked failed so importing history can never replay paid provider calls.
    """
    source_path = path.resolve(strict=True)
    if repository.engine.url.get_backend_name() == 'sqlite':
        target_path = repository.engine.url.database
        if target_path and Path(target_path).resolve() == source_path:
            raise ValueError('The import source and destination must be different databases.')
    imported = 0
    with sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True) as legacy:
        legacy.row_factory = sqlite3.Row
        with repository.sessions.begin() as session:
            for old in legacy.execute('SELECT * FROM research_runs ORDER BY created_at, id'):
                if session.get(RunRow, old['id']):
                    continue
                interrupted = old['status'] in {'queued', 'running'}
                row = RunRow(id=old['id'], user_id=None, topic=old['topic'],
                    instructions=old['instructions'], depth=old['depth'],
                    status='failed' if interrupted else old['status'],
                    current_step=old['current_step'], progress=old['progress'], iteration=old['iteration'],
                    created_at=_date(old['created_at']), updated_at=_date(old['updated_at']),
                    started_at=_date(old['started_at']),
                    finished_at=utcnow() if interrupted else _date(old['finished_at']),
                    owner=None, lease_expires_at=None,
                    error='Legacy job was interrupted; submit a new run to retry.' if interrupted else old['error'])
                session.add(row)
                session.flush()
                save_steps(session, row.id, json.loads(old['step_status_json'] or '{}'))
                if old['report_json']:
                    report = ResearchReport.model_validate_json(old['report_json'])
                    if report.query != row.topic:
                        raise ValueError('Legacy report question does not match its research run.')
                    save_report(session, row.id, report)
                else:
                    sources = [SourceDocument.model_validate_json(item[0]) for item in legacy.execute(
                        'SELECT document_json FROM research_sources WHERE run_id=? ORDER BY source_id', (row.id,))]
                    save_sources(session, row.id, sources)
                imported += 1
    return imported


def assign_legacy(repository, email: str) -> int:
    """Local administrator operation; never called during public registration."""
    from deepresearch_ai.models.auth import LoginRequest
    normalized = str(LoginRequest(email=email, password='unused').email)
    with repository.sessions.begin() as session:
        user = session.scalar(select(UserRow).where(UserRow.email == normalized, UserRow.is_active.is_(True)))
        if user is None:
            raise ValueError('Register an account with that email before assigning legacy history.')
        changed = session.execute(update(RunRow).where(RunRow.user_id.is_(None)).values(user_id=user.id))
        return changed.rowcount

"""Relational integrity, immutable source versions, and legacy data preservation."""
import hashlib
import sqlite3
from datetime import timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError

from deepresearch_ai.models import ResearchReport, ResearchRequest
from deepresearch_ai.models.runs import CreateResearch
from deepresearch_ai.config import Settings
from deepresearch_ai.services.mock_data import SAMPLE_QUERY, sample_report
from deepresearch_ai.services.workflow import GraphResearchService
from deepresearch_ai.storage.auth import SQLAuthRepository
from deepresearch_ai.storage.legacy import assign_legacy, import_sqlite
from deepresearch_ai.storage.report_store import save_sources
from deepresearch_ai.storage.sql import SQLRunRepository
from deepresearch_ai.storage.tables import CitationRow, SourceRow, SourceSnapshotRow


@pytest.fixture
def repo(tmp_path):
    repository = SQLRunRepository('sqlite:///' + (tmp_path / 'normalized.db').as_posix())
    repository.initialize()
    yield repository
    repository.close()


def test_full_graph_report_roundtrips_as_related_rows(repo):
    report = GraphResearchService(Settings(_env_file=None, mock_mode=True)).run(ResearchRequest(query=SAMPLE_QUERY))
    report.usage.update(optional=None, label='mock')
    run = repo.create(CreateResearch(topic=SAMPLE_QUERY))
    run_id = str(run.id)
    assert repo.claim_next('worker', 60)
    assert repo.finish(run_id, 'worker', report)
    saved = repo.get(run_id).report
    assert saved.model_dump() == report.model_dump()
    inspector = inspect(repo.engine)
    assert len(inspector.get_table_names()) >= 20
    for table in inspector.get_table_names():
        if table in {'research_checkpoints','provider_calls'}:
            continue  # Versioned execution snapshots/receipts are operational data, not domain report blobs.
        assert not any(column['name'].endswith('_json') for column in inspector.get_columns(table))


def test_one_source_identity_with_independent_run_snapshots(repo):
    source = sample_report().sources[0]
    first = str(repo.create(CreateResearch(topic=SAMPLE_QUERY)).id)
    second = str(repo.create(CreateResearch(topic=SAMPLE_QUERY)).id)
    changed = source.model_copy(update={'content':'A newer retrieved passage.',
        'retrieved_at':source.retrieved_at + timedelta(days=1)})
    with repo.sessions.begin() as session:
        save_sources(session, first, [source])
        save_sources(session, first, [source])  # Progress writes do not duplicate a snapshot.
        save_sources(session, second, [changed])
    assert repo.sources(first)[0].content == source.content
    assert repo.sources(second)[0].content == changed.content
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(SourceRow)) == 1
        assert session.scalar(select(func.count()).select_from(SourceSnapshotRow)) == 2


def test_database_rejects_citation_to_another_runs_source(repo):
    report = sample_report()
    first = str(repo.create(CreateResearch(topic=SAMPLE_QUERY)).id)
    assert repo.claim_next('worker', 60)
    assert repo.finish(first, 'worker', report)
    second = str(repo.create(CreateResearch(topic='An unrelated question')).id)
    assert repo.claim_next('worker',60)
    assert repo.finish(second, 'worker', ResearchReport(query='An unrelated question', title='No evidence',
        status='insufficient_evidence', body_markdown='No supporting sources.'))
    with pytest.raises(IntegrityError), repo.sessions.begin() as session:
        session.add(CitationRow(run_id=second, label='1', source_id=report.sources[0].source_id))


def test_snapshot_times_are_normalized_to_utc_without_duplication(repo):
    source = sample_report().sources[0]
    local_time = source.retrieved_at.astimezone(timezone(timedelta(hours=5, minutes=30)))
    source = source.model_copy(update={'retrieved_at':local_time})
    run_id = str(repo.create(CreateResearch(topic=SAMPLE_QUERY)).id)
    with repo.sessions.begin() as session:
        save_sources(session, run_id, [source])
        save_sources(session, run_id, [source])
    assert repo.sources(run_id)[0].retrieved_at == local_time
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(SourceSnapshotRow)) == 1


def test_report_question_cannot_silently_change_on_storage(repo):
    run = repo.create(CreateResearch(topic='A different research question'))
    assert repo.claim_next('worker', 60)
    with pytest.raises(ValueError, match='question'):
        repo.finish(str(run.id), 'worker', sample_report())
    assert repo.get(str(run.id)).status == 'running'


def legacy_database(path):
    report = sample_report()
    run_id = str(uuid4())
    with sqlite3.connect(path) as connection:
        connection.execute('''CREATE TABLE research_runs (
            id TEXT PRIMARY KEY, topic TEXT, instructions TEXT, depth TEXT, status TEXT,
            current_step TEXT, progress REAL, iteration INTEGER, step_status_json TEXT,
            created_at TEXT, updated_at TEXT, started_at TEXT, finished_at TEXT,
            owner TEXT, lease_expires_at TEXT, error TEXT, report_json TEXT)''')
        connection.execute('CREATE TABLE research_sources (run_id TEXT, source_id TEXT, document_json TEXT)')
        connection.execute('INSERT INTO research_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            run_id, report.query, None, 'standard', 'completed', 'writer', 1, 0, '{}',
            '2026-01-01 00:00:00', '2026-01-01 00:01:00', None, None,
            None, None, None, report.model_dump_json()))
    return run_id, report


def test_legacy_import_is_read_only_repeatable_and_explicitly_owned(repo, tmp_path):
    path = tmp_path / 'old.db'
    run_id, report = legacy_database(path)
    fingerprint = hashlib.sha256(path.read_bytes()).digest()
    assert import_sqlite(repo, path) == 1
    assert import_sqlite(repo, path) == 0
    assert hashlib.sha256(path.read_bytes()).digest() == fingerprint
    assert repo.get(run_id).report.model_dump() == report.model_dump()
    users = SQLAuthRepository(repo.sessions)
    user = users.create_user('owner@example.com', 'Owner', 'test-only-hash')
    other = users.create_user('other@example.com', 'Other', 'test-only-hash')
    assert repo.list_for_user(user.id,20,0).total == 0
    assert assign_legacy(repo, ' OWNER@example.com ') == 1
    assert assign_legacy(repo, 'owner@example.com') == 0
    assert repo.get_for_user(run_id, user.id)
    assert repo.get_for_user(run_id, other.id) is None


def test_interrupted_legacy_jobs_are_not_replayed(repo, tmp_path):
    path = tmp_path / 'interrupted.db'
    run_id, _ = legacy_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE research_runs SET status='running', report_json=NULL")
    assert import_sqlite(repo, path) == 1
    assert repo.get(run_id).status == 'failed'
    assert repo.claim_next('worker',60) is None


def test_legacy_assignment_requires_an_existing_account(repo):
    with pytest.raises(ValueError, match='Register'):
        assign_legacy(repo, 'missing@example.com')

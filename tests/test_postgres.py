"""Opt-in PostgreSQL integration using disposable schemas, never public tables.

Set TEST_POSTGRES_URL to a dedicated local test database. Each test migrates a
random schema and drops only that schema. All LLM/search calls remain mocked.
"""
from concurrent.futures import ThreadPoolExecutor
import os
import re
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url

from deepresearch_ai.api.app import create_app
from deepresearch_ai.config import Settings
from deepresearch_ai.db import upgrade
from deepresearch_ai.models.runs import CreateResearch
from deepresearch_ai.models import ResearchRequest
from deepresearch_ai.services.mock_data import SAMPLE_QUERY
from deepresearch_ai.services.workflow import GraphResearchService
from deepresearch_ai.storage.sql import SQLRunRepository
from deepresearch_ai.storage.tables import ClaimRow, EvidenceRow, SourceRow
from deepresearch_ai.worker import ResearchWorker


@pytest.fixture
def postgres():
    raw = os.environ.get('TEST_POSTGRES_URL')
    if not raw:
        pytest.skip('Set TEST_POSTGRES_URL for isolated PostgreSQL integration tests.')
    url = make_url(raw)
    # This repository's verification command targets only its local cluster.
    if url.host not in {'127.0.0.1', 'localhost'} or url.port != 55432:
        pytest.fail('PostgreSQL integration is restricted to the project-local port 55432.')
    schema = 'test_' + uuid4().hex
    assert re.fullmatch(r'test_[a-f0-9]{32}', schema)
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = url.update_query_dict({'options':f'-csearch_path={schema}'})
    repository = SQLRunRepository(scoped.render_as_string(hide_password=False))
    try:
        with repository.engine.begin() as connection:
            upgrade(connection)
        repository.initialize()
        yield repository
    finally:
        repository.close()
        # Identifier is generated internally and validated above, not user input.
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_postgres_partial_report_preserves_supported_findings(postgres):
    from deepresearch_ai.agents.writer import WriterAgent

    settings = Settings(_env_file=None, mock_mode=True)
    state = GraphResearchService(settings).run_state(ResearchRequest(query=SAMPLE_QUERY))
    state['coverage_sufficient'] = False
    report = WriterAgent(settings)(state)['report']
    assert report.status == 'partial'
    run = postgres.create(CreateResearch(topic=SAMPLE_QUERY))
    assert postgres.claim_next('partial-worker', 60)
    assert postgres.finish(str(run.id), 'partial-worker', report)
    saved = postgres.get(str(run.id))
    assert saved.status.value == 'completed'
    assert saved.report.model_dump() == report.model_dump()


def test_postgres_auth_worker_and_normalized_report(postgres, sign_in):
    settings = Settings(_env_file=None, mock_mode=True)
    with TestClient(create_app(settings, repository=postgres)) as client:
        assert client.get('/research').status_code == 401
        user = sign_in(client)
        run = client.post('/research', json={'topic':SAMPLE_QUERY}).json()
        assert run['status'] == 'queued'
        worker = ResearchWorker(postgres, settings,
            service_factory=lambda cfg, **kwargs: GraphResearchService(cfg, **kwargs))
        assert worker.run_once()
        report = client.get('/research/' + run['id'] + '/report')
        assert report.status_code == 200
        assert len(report.json()['citations']) == 2
        assert client.get('/research').json()['total'] == 1
        with postgres.sessions() as session:
            assert len(list(session.scalars(select(SourceRow)))) == 2
            assert len(list(session.scalars(select(EvidenceRow)))) >= 2
            assert len(list(session.scalars(select(ClaimRow)))) >= 1
        assert postgres.get_for_user(run['id'], str(uuid4())) is None
        csrf = client.get('/auth/me').json()['csrf_token']
        assert client.post('/auth/logout', headers={'X-CSRF-Token':csrf}).status_code == 204
        assert client.get('/research').status_code == 401
        assert postgres.list_for_user(user['id'],20,0).total == 1


def test_postgres_concurrent_claims_are_unique(postgres):
    ids = {str(postgres.create(CreateResearch(topic=f'Concurrent topic {i}')).id) for i in range(4)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda owner: postgres.claim_next(owner,60), [str(uuid4()) for _ in range(4)]))
    assert {str(run.id) for run in claims} == ids
    assert postgres.claim_next('no-work',60) is None


def test_postgres_migrations_are_repeatable_and_match_metadata(postgres):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from deepresearch_ai.storage.tables import Base
    with postgres.engine.begin() as connection:
        upgrade(connection)
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, Base.metadata) == []
    assert len(inspect(postgres.engine).get_table_names()) == len(Base.metadata.tables) + 1


def test_postgres_parallel_quota_and_duplicate_admission(postgres, sign_in):
    from deepresearch_ai.storage.usage import AdmissionError
    settings = Settings(_env_file=None, mock_mode=True, quota_daily_runs=2)
    with TestClient(create_app(settings, repository=postgres)) as client:
        user = sign_in(client)
        def submit(index):
            try:
                return postgres.create(CreateResearch(topic=f'Parallel topic {index}'),
                    user_id=user['id'], settings=settings, idempotency_key=f'request-{index}')
            except AdmissionError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(submit,range(5)))
        assert sum(not isinstance(item,str) for item in results) == 2
        assert results.count('daily_run_limit') == 3
        existing = next(item for item in results if not isinstance(item,str))
        # Replays work even when the quota has been reached.
        with ThreadPoolExecutor(max_workers=4) as pool:
            copies = list(pool.map(lambda _: postgres.create(CreateResearch(topic=existing.topic),
                user_id=user['id'], settings=settings, idempotency_key='repeat-key'),range(4)))
        assert {item.id for item in copies} == {existing.id}
        assert client.get('/account/usage').json()['today']['submissions'] == 2


def test_postgres_account_deletion_and_shared_sources(postgres, sign_in):
    from sqlalchemy import func
    from deepresearch_ai.storage.tables import SessionRow, SourceSnapshotRow, UsageEntryRow, UserRow
    settings = Settings(_env_file=None,mock_mode=True)
    app = create_app(settings,repository=postgres)
    with TestClient(app) as alice, TestClient(app) as bob:
        sign_in(alice,'alice@example.com'); sign_in(bob,'bob@example.com')
        first = alice.post('/research',json={'topic':SAMPLE_QUERY}).json()
        assert ResearchWorker(postgres,settings).run_once()
        second = bob.post('/research',json={'topic':SAMPLE_QUERY}).json()
        assert ResearchWorker(postgres,settings).run_once()
        assert alice.delete('/research/'+second['id']).status_code == 404
        assert alice.request('DELETE','/auth/me',json={
            'password':'A private test passphrase 2026!','confirmation':'DELETE'}).status_code == 204
        assert alice.get('/account/usage').status_code == 401
        assert bob.get('/research/'+second['id']+'/report').status_code == 200
        assert len(bob.get('/research/'+second['id']+'/sources').json()) == 2
        assert bob.delete('/research/'+second['id']).status_code == 204
        with postgres.sessions() as session:
            for model in (SourceRow,SourceSnapshotRow):
                assert session.scalar(select(func.count()).select_from(model)) == 0
            assert session.scalar(select(func.count()).select_from(UsageEntryRow)) == 1
            assert session.scalar(select(func.count()).select_from(UserRow)) == 1
        assert bob.request('DELETE','/auth/me',json={
            'password':'A private test passphrase 2026!','confirmation':'DELETE'}).status_code == 204
        with postgres.sessions() as session:
            assert session.scalar(select(func.count()).select_from(UsageEntryRow)) == 0
            assert session.scalar(select(func.count()).select_from(SessionRow)) == 0


def test_postgres_upgrade_preserves_existing_user_history(postgres):
    """Exercise the new migration with populated tables from its parent revision."""
    from alembic import command
    from deepresearch_ai.db import migration_config
    from deepresearch_ai.storage.auth import SQLAuthRepository
    settings = Settings(_env_file=None,mock_mode=True)
    user = SQLAuthRepository(postgres.sessions).create_user('existing@example.com','Existing','test-only-hash')
    report = GraphResearchService(settings).run(ResearchRequest(query=SAMPLE_QUERY))
    run = postgres.create(CreateResearch(topic=SAMPLE_QUERY))
    postgres.claim_next('worker',60)
    assert postgres.finish(str(run.id),'worker',report)
    with postgres.engine.begin() as connection:
        config = migration_config(); config.attributes['connection'] = connection
        command.downgrade(config,'97219b01b451')
        connection.execute(text('UPDATE research_runs SET user_id=:user_id WHERE id=:id'),
                           {'user_id':user.id,'id':str(run.id)})
        upgrade(connection)
    postgres.initialize()
    copied = postgres.get_for_user(str(run.id),user.id)
    assert copied.report.model_dump() == report.model_dump()
    usage = postgres.usage_for_user(user.id,settings)
    assert usage.lifetime.submissions == 1
    assert usage.lifetime.admitted_tokens == report.usage['reserved_tokens']


def test_postgres_source_cleanup_can_race_with_another_run_retrieving_same_url(postgres):
    from threading import Event
    from sqlalchemy import event
    from deepresearch_ai.services.mock_data import sample_sources
    from deepresearch_ai.storage.deletion import delete_runs
    from deepresearch_ai.storage.tables import RunRow
    first = postgres.create(CreateResearch(topic='First question'))
    second = postgres.create(CreateResearch(topic='Second question'))
    postgres.claim_next('first-worker',60)
    postgres.claim_next('second-worker',60)
    sources = sample_sources()
    assert postgres.update_progress(str(first.id),'first-worker','search',.2,1,{},sources)
    inserting = Event()
    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith('INSERT INTO sources'):
            inserting.set()
    event.listen(postgres.engine,'before_cursor_execute',observe)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with postgres.sessions.begin() as session:
                row = session.get(RunRow,str(first.id))
                list(session.scalars(select(SourceRow).order_by(SourceRow.source_id).with_for_update()))
                future = pool.submit(postgres.update_progress,str(second.id),'second-worker','search',.2,1,{},sources)
                assert inserting.wait(5)
                delete_runs(session,[row])
            assert future.result(timeout=10)
    finally:
        event.remove(postgres.engine,'before_cursor_execute',observe)
    assert len(postgres.sources(str(second.id))) == 2


def test_postgres_checkpoint_resume_and_completed_stream(postgres,sign_in):
    from datetime import timedelta
    from sqlalchemy import func,update
    from deepresearch_ai.storage.tables import ProviderCallRow,RunRow
    from deepresearch_ai.storage.sql import utcnow
    class Crash(BaseException): pass
    settings = Settings(_env_file=None,mock_mode=True)
    with TestClient(create_app(settings,repository=postgres)) as client:
        sign_in(client)
        run_id = client.post('/research',json={'topic':SAMPLE_QUERY}).json()['id']
        def factory(config,**callbacks):
            original = callbacks['on_update']
            def crash(state):
                original(state)
                if state['step_status']['planner'] == 'completed': raise Crash()
            return GraphResearchService(config,on_step=callbacks['on_step'],on_update=crash)
        with pytest.raises(Crash):
            ResearchWorker(postgres,settings,service_factory=factory).run_once()
        with postgres.sessions.begin() as session:
            session.execute(update(RunRow).where(RunRow.id == run_id).values(lease_expires_at=utcnow()-timedelta(seconds=1)))
        postgres.expire_abandoned()
        assert client.post('/research/'+run_id+'/resume').status_code == 202
        assert ResearchWorker(postgres,settings).run_once()
        stream = client.get('/research/'+run_id+'/events')
        assert 'event: progress' in stream.text and 'event: end' in stream.text
        with postgres.sessions() as session:
            calls = session.scalar(select(func.count()).select_from(ProviderCallRow).where(ProviderCallRow.kind == 'llm'))
            assert calls == postgres.get(run_id).report.usage['llm_calls'] == 3


def test_postgres_admin_roles_and_consistent_private_archive(postgres,sign_in):
    import io,zipfile,json
    from sqlalchemy import update
    from deepresearch_ai.storage.tables import UserRow
    settings = Settings(_env_file=None,mock_mode=True)
    with TestClient(create_app(settings,repository=postgres)) as client:
        user = sign_in(client)
        assert not user['is_admin'] and client.get('/admin/overview').status_code == 403
        run_id = client.post('/research',json={'topic':SAMPLE_QUERY}).json()['id']
        assert ResearchWorker(postgres,settings).run_once()
        with postgres.sessions.begin() as session:
            session.execute(update(UserRow).where(UserRow.id == user['id']).values(is_admin=True))
        assert client.get('/admin/overview').status_code == 200
        response = client.get('/account/export')
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert f'research/{run_id}/report.md' in archive.namelist()
            assert json.loads(archive.read('account.json'))['is_admin'] is True

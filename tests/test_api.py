"""API + durable queue integration, with offline model/search dependencies."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from deepresearch_ai.api.app import create_app
from deepresearch_ai.api.schemas import CreateResearch, ResearchDepth
from deepresearch_ai.config import Settings
from deepresearch_ai.models import ResearchReport, ResearchRequest
from deepresearch_ai.services.depth import settings_for_depth
from deepresearch_ai.services.mock_data import SAMPLE_QUERY
from deepresearch_ai.services.mock_llm import MockChatModel
from deepresearch_ai.services.search import MockWebSearch
from deepresearch_ai.services.workflow import GraphResearchService
from deepresearch_ai.storage.sql import RunRow, SQLRunRepository, utcnow
from deepresearch_ai.worker import ResearchWorker


@pytest.fixture
def repository(tmp_path):
    repo = SQLRunRepository('sqlite:///' + (tmp_path / 'history.db').as_posix())
    repo.initialize()
    yield repo
    repo.close()


@pytest.fixture
def settings():
    return Settings(_env_file=None, mock_mode=True, quota_max_active_runs=10)


@pytest.fixture
def client(repository, settings, sign_in):
    with TestClient(create_app(settings, repository=repository), raise_server_exceptions=False) as api:
        sign_in(api)
        yield api


def new_run(client, **overrides):
    response = client.post('/research', json={'topic': SAMPLE_QUERY, **overrides})
    assert response.status_code == 202
    return response.json()


def offline_service(settings, **callbacks):
    return GraphResearchService(settings, model=MockChatModel(), search=MockWebSearch(), **callbacks)


def test_create_only_enqueues_and_never_calls_a_model(client, monkeypatch):
    monkeypatch.setattr(GraphResearchService, 'run', Mock(side_effect=AssertionError('Do not run in HTTP requests')))
    run = new_run(client, instructions='Explain limitations.', depth='quick')
    assert run['status'] == 'queued' and run['report'] is None
    assert run['instructions'] == 'Explain limitations.'
    assert client.get('/research/' + run['id']).json()['status'] == 'queued'
    assert client.get('/research/' + run['id'] + '/report').status_code == 409
    assert client.get('/research/' + run['id'] + '/sources').json() == []


def test_all_endpoints_complete_a_real_offline_graph_and_persist_report(client, repository, settings):
    run = new_run(client, instructions='Focus on evidence.', depth='deep')
    worker = ResearchWorker(repository, settings, service_factory=offline_service)
    assert worker.run_once()
    result = client.get('/research/' + run['id']).json()
    assert result['status'] == 'completed' and result['progress'] == 1
    assert result['current_step'] == 'writer' and result['iteration'] == 1
    report_response = client.get('/research/' + run['id'] + '/report')
    assert report_response.status_code == 200
    report = ResearchReport.model_validate(report_response.json())
    assert report.status == 'complete'
    sources = client.get('/research/' + run['id'] + '/sources').json()
    assert {source['source_id'] for source in sources} == {source.source_id for source in report.sources}
    history = client.get('/research').json()
    assert history['total'] == 1 and history['items'][0]['id'] == run['id']
    assert 'report' not in history['items'][0]
    exported = client.get('/research/' + run['id'] + '/report.md')
    assert exported.status_code == 200 and 'text/markdown' in exported.headers['content-type']
    assert '# Research summary' in exported.text and '## Sources' in exported.text
    assert 'attachment;' in exported.headers['content-disposition']
    assert not worker.run_once()


def test_history_and_queued_jobs_survive_repository_restart(tmp_path, settings):
    url = 'sqlite:///' + (tmp_path / 'restart.db').as_posix()
    first = SQLRunRepository(url)
    first.initialize()
    run = first.create(CreateResearch(topic=SAMPLE_QUERY))
    first.close()
    second = SQLRunRepository(url)
    second.initialize()
    assert second.get(str(run.id)).status == 'queued'
    assert ResearchWorker(second, settings, service_factory=offline_service).run_once()
    second.close()
    third = SQLRunRepository(url)
    third.initialize()
    assert third.get(str(run.id)).status == 'completed'
    assert len(third.sources(str(run.id))) == 2
    assert third.list_runs(20, 0).total == 1
    third.close()


def test_unique_ids_and_paginated_newest_first_history(client):
    ids = [new_run(client, topic=f'Topic {i}')['id'] for i in range(4)]
    assert len(set(ids)) == 4
    page = client.get('/research?limit=2&offset=1').json()
    assert page['total'] == 4 and [item['id'] for item in page['items']] == list(reversed(ids))[1:3]


@pytest.mark.parametrize('payload', [
    {}, {'topic': ''}, {'topic': ' '}, {'topic': 'x' * 2001},
    {'topic': 'Hello', 'instructions': 'x' * 2001}, {'topic': 'Hello', 'depth': 'unlimited'},
    {'topic': 'Hello', 'api_key': 'must-not-echo-this-secret'},
])
def test_invalid_requests_have_consistent_safe_errors(client, payload):
    response = client.post('/research', json=payload)
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'validation_error'
    assert response.json()['error']['details']
    assert 'must-not-echo-this-secret' not in response.text


@pytest.mark.parametrize('path', ['/research/not-a-uuid', '/research?limit=101', '/research?offset=-1', '/research?limit=0'])
def test_path_and_query_validation_use_same_error_envelope(client, path):
    response = client.get(path)
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'validation_error'


@pytest.mark.parametrize('suffix', ['', '/sources', '/report', '/report.md'])
def test_missing_runs_are_404(client, suffix):
    response = client.get(f'/research/{uuid4()}' + suffix)
    assert response.status_code == 404 and response.json()['error']['code'] == 'run_not_found'


def test_queued_cancellation_is_persistent_and_idempotent(client, repository, settings):
    run = new_run(client)
    path = '/research/' + run['id'] + '/cancel'
    assert client.post(path).json()['status'] == 'cancelled'
    assert client.post(path).status_code == 200
    assert not ResearchWorker(repository, settings).run_once()
    assert client.get('/research/' + run['id'] + '/report').status_code == 409


def test_running_cancellation_cannot_be_overwritten_by_late_completion(client, repository, settings):
    run = new_run(client)
    class CancelDuringRun:
        def __init__(self, settings, **callbacks):
            self.callbacks = callbacks
        def run(self, request):
            assert repository.get(run['id']).status == 'running'
            assert client.post('/research/' + run['id'] + '/cancel').status_code == 200
            return offline_service(settings).run(request)
    assert ResearchWorker(repository, settings, service_factory=CancelDuringRun).run_once()
    result = repository.get(run['id'])
    assert result.status == 'cancelled' and result.report is None


def test_cancelled_graph_stops_before_next_agent_call(client, repository, settings):
    run = new_run(client)
    calls = []
    def service_factory(config, **callbacks):
        real_step = callbacks['on_step']
        def on_step(step, iteration):
            calls.append(step)
            if step == 'search':
                repository.cancel(run['id'])
            real_step(step, iteration)
        callbacks['on_step'] = on_step
        return offline_service(config, **callbacks)
    ResearchWorker(repository, settings, service_factory=service_factory).run_once()
    assert calls == ['planner', 'search']
    assert repository.get(run['id']).status == 'cancelled'


def test_completed_runs_cannot_be_cancelled(client, repository, settings):
    run = new_run(client)
    ResearchWorker(repository, settings, service_factory=offline_service).run_once()
    response = client.post('/research/' + run['id'] + '/cancel')
    assert response.status_code == 409 and response.json()['error']['code'] == 'run_not_cancellable'


def test_provider_failures_store_only_a_sanitized_error(client, repository, settings):
    run = new_run(client)
    service = Mock(run=Mock(side_effect=RuntimeError('sensitive-api-key-value')))
    ResearchWorker(repository, settings, service_factory=lambda *args, **kwargs: service).run_once()
    response = client.get('/research/' + run['id'])
    assert response.json()['status'] == 'failed'
    assert response.json()['error'] and 'sensitive-api-key-value' not in response.text


def test_missing_live_credentials_fail_the_job_without_blocking_creation(client, repository):
    run = new_run(client)
    ResearchWorker(repository, Settings(_env_file=None, mock_mode=False)).run_once()
    result = client.get('/research/' + run['id']).json()
    assert result['status'] == 'failed' and 'configuration' in result['error']


def test_progress_and_partial_sources_are_visible_during_execution(client, repository, settings):
    run = new_run(client)
    seen = []
    def factory(config, **callbacks):
        on_step, on_update = callbacks['on_step'], callbacks['on_update']
        def step(name, iteration):
            on_step(name, iteration)
            response = client.get('/research/' + run['id']).json()
            seen.append((response['current_step'], response['progress']))
            assert response['status'] == 'running'
            if name == 'evidence':
                assert len(client.get('/research/' + run['id'] + '/sources').json()) == 2
        return offline_service(config, on_step=step, on_update=on_update)
    ResearchWorker(repository, settings, service_factory=factory).run_once()
    assert [name for name, _ in seen] == ['planner', 'search', 'evidence', 'verification', 'writer']
    assert [percent for _, percent in seen] == sorted(percent for _, percent in seen)


def test_atomic_claim_only_one_worker_owns_a_run(repository):
    run = repository.create(CreateResearch(topic=SAMPLE_QUERY))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda owner: repository.claim_next(owner, 60), ['a', 'b']))
    assert sum(result is not None for result in results) == 1
    assert repository.get(str(run.id)).status == 'running'


def test_expired_lease_marks_failed_and_fences_stale_worker(repository, settings):
    run = repository.create(CreateResearch(topic=SAMPLE_QUERY))
    repository.claim_next('stale-owner', 60)
    with repository.sessions.begin() as session:
        session.execute(update(RunRow).where(RunRow.id == str(run.id)).values(lease_expires_at=utcnow() - timedelta(seconds=1)))
    assert not repository.renew(str(run.id), 'stale-owner', 60)
    assert repository.expire_abandoned() == 1
    assert repository.get(str(run.id)).status == 'failed'
    report = offline_service(settings).run(ResearchRequest(query=SAMPLE_QUERY))
    assert not repository.finish(str(run.id), 'stale-owner', report)
    assert repository.get(str(run.id)).report is None


def test_wrong_owner_cannot_renew_update_fail_or_complete(repository):
    run = repository.create(CreateResearch(topic=SAMPLE_QUERY))
    repository.claim_next('right', 60)
    assert not repository.renew(str(run.id), 'wrong', 60)
    assert not repository.update_progress(str(run.id), 'wrong', 'planner', .1, 1, {}, [])
    assert not repository.fail(str(run.id), 'wrong', 'Failure')
    assert repository.get(str(run.id)).status == 'running'


def test_runtime_budgets_remain_global_ceilings_for_every_depth():
    config = Settings(mock_mode=True, research_max_pages=2, research_max_retries=0,
                      research_max_llm_calls=4, research_max_searches=2)
    for depth in ResearchDepth:
        profile = settings_for_depth(config, depth)
        for field in ('research_max_pages', 'research_max_retries', 'research_max_llm_calls', 'research_max_searches'):
            assert getattr(profile, field) <= getattr(config, field)
    assert settings_for_depth(Settings(), ResearchDepth.quick).research_max_retries == 0
    assert settings_for_depth(Settings(), ResearchDepth.standard).research_max_retries == 1
    assert settings_for_depth(Settings(), ResearchDepth.deep).research_max_retries == 2


def test_optional_instructions_reach_planner_and_evidence_payloads(client, repository, settings):
    import json
    run = new_run(client, instructions='Focus on limitations and primary papers.')
    received = []
    class RecordingModel(MockChatModel):
        def with_structured_output(self, schema, **kwargs):
            from langchain_core.runnables import RunnableLambda
            runner = super().with_structured_output(schema, **kwargs)
            def respond(messages):
                received.append(json.loads(messages[-1].content).get('instructions'))
                return runner.invoke(messages)
            return RunnableLambda(respond)
    def factory(config, **callbacks):
        return GraphResearchService(config, model=RecordingModel(), search=MockWebSearch(), **callbacks)
    ResearchWorker(repository, settings, service_factory=factory).run_once()
    assert received == ['Focus on limitations and primary papers.'] * 3


def test_sensitive_settings_never_appear_in_api_responses(client, repository, sign_in):
    secret = 'sensitive-live-key'
    settings = Settings(mock_mode=True, google_api_key=secret, tavily_api_key=secret)
    with TestClient(create_app(settings, repository=repository)) as api:
        sign_in(api)
        run = new_run(api)
        ResearchWorker(repository, settings, service_factory=offline_service).run_once()
        for path in ('/research', '/research/' + run['id'], '/research/' + run['id'] + '/report'):
            assert secret not in api.get(path).text


def test_api_storage_errors_use_safe_consistent_envelope(client, repository, monkeypatch):
    from sqlalchemy.exc import OperationalError
    monkeypatch.setattr(repository, 'list_for_user', Mock(side_effect=OperationalError('secret-sql', {}, RuntimeError('secret-db-url'))))
    response = client.get('/research')
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'storage_unavailable'
    assert 'secret' not in response.text


def test_headers_location_and_health(client):
    response = client.post('/research', json={'topic': SAMPLE_QUERY})
    assert response.headers['location'] == '/research/' + response.json()['id']
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['cache-control'] == 'no-store'
    assert client.get('/health').json() == {'status': 'ok'}

def test_http_requests_remain_responsive_while_a_worker_is_busy(client, repository, settings):
    from threading import Event, Thread
    started, release = Event(), Event()
    first = new_run(client)
    class SlowService:
        def __init__(self, config, **callbacks):
            self.config = config
        def run(self, request):
            started.set()
            assert release.wait(10)
            return offline_service(self.config).run(request)
    thread = Thread(target=lambda: ResearchWorker(repository, settings, service_factory=SlowService).run_once())
    thread.start()
    try:
        assert started.wait(5)
        assert client.get('/research/' + first['id']).json()['status'] == 'running'
        second = new_run(client, topic='A second topic can be queued during research.')
        assert second['status'] == 'queued'
        assert client.get('/research').json()['total'] == 2
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert client.get('/research/' + first['id']).json()['status'] == 'completed'


def test_polling_detects_worker_expiry_without_a_replacement_worker(client, repository):
    run = new_run(client)
    repository.claim_next('dead-worker', 60)
    with repository.sessions.begin() as session:
        session.execute(update(RunRow).where(RunRow.id == run['id']).values(lease_expires_at=utcnow() - timedelta(seconds=1)))
    response = client.get('/research/' + run['id'])
    assert response.json()['status'] == 'failed'
    assert 'lease expired' in response.json()['error']


def test_worker_heartbeat_extends_lease_and_preserves_ownership(repository):
    run = repository.create(CreateResearch(topic=SAMPLE_QUERY))
    repository.claim_next('owner', 10)
    assert repository.renew(str(run.id), 'owner', 60)
    with repository.sessions() as session:
        row = session.get(RunRow, str(run.id))
        recorded = row.lease_expires_at
        if recorded.tzinfo is None:
            from datetime import timezone
            recorded = recorded.replace(tzinfo=timezone.utc)
        assert recorded > utcnow() + timedelta(seconds=50)
    assert repository.cancel(str(run.id))
    assert not repository.renew(str(run.id), 'owner', 60)


def test_invalid_report_is_never_committed_as_a_completed_run(repository):
    from pydantic import ValidationError
    from deepresearch_ai.services.mock_data import sample_report
    run = repository.create(CreateResearch(topic=SAMPLE_QUERY))
    repository.claim_next('owner', 60)
    invalid = sample_report().model_copy(update={'citations': {'1': 'src_' + '0' * 64}})
    with pytest.raises(ValidationError):
        repository.finish(str(run.id), 'owner', invalid)
    assert repository.get(str(run.id)).status == 'running'
    assert repository.get(str(run.id)).report is None


def test_unexpected_api_errors_do_not_echo_exception_details(client, repository, monkeypatch):
    monkeypatch.setattr(repository, 'create', Mock(side_effect=RuntimeError('secret exception body')))
    response = client.post('/research', json={'topic': SAMPLE_QUERY})
    assert response.status_code == 500
    assert response.json()['error']['code'] == 'internal_error'
    assert 'secret' not in response.text

def test_worker_renews_lease_while_provider_work_is_in_flight(repository, settings, monkeypatch):
    from threading import Event
    seen = Event()
    renew = repository.renew
    def observe(*args):
        result = renew(*args)
        seen.set()
        return result
    monkeypatch.setattr(repository, 'renew', observe)
    # Shorten the lease only in this test, without waiting a real production TTL.
    config = settings.model_copy(update={'worker_lease_seconds': .3})
    run = repository.create(CreateResearch(topic=SAMPLE_QUERY))
    class WaitForHeartbeat:
        def __init__(self, config, **callbacks):
            self.config = config
        def run(self, request):
            assert seen.wait(3)
            return offline_service(self.config).run(request)
    assert ResearchWorker(repository, config, service_factory=WaitForHeartbeat).run_once()
    assert seen.is_set() and repository.get(str(run.id)).status == 'completed'

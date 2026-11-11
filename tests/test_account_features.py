"""Account lifecycle, global history queries and durable research allowances."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, update

from deepresearch_ai.api.app import create_app
from deepresearch_ai.config import Settings
from deepresearch_ai.models import ResearchRequest
from deepresearch_ai.models.runs import CreateResearch
from deepresearch_ai.services.mock_data import SAMPLE_QUERY, sample_report
from deepresearch_ai.services.workflow import GraphResearchService
from deepresearch_ai.storage.sql import SQLRunRepository, utcnow
from deepresearch_ai.storage.tables import (Base, RunRow, SessionRow, SourceRow, SourceSnapshotRow,
    SubmissionKeyRow, UsageEntryRow, UserRow)
from deepresearch_ai.storage.usage import AdmissionError
from deepresearch_ai.worker import ResearchWorker


PASSWORD = 'A private test passphrase 2026!'


@pytest.fixture
def account(tmp_path, sign_in):
    repository = SQLRunRepository('sqlite:///' + (tmp_path / 'account.db').as_posix())
    settings = Settings(_env_file=None, mock_mode=True)
    with TestClient(create_app(settings, repository=repository)) as client:
        user = sign_in(client)
        yield client, repository, settings, user
    repository.close()


def submit(client, topic=SAMPLE_QUERY, key=None, **options):
    return client.post('/research', json={'topic':topic, **options},
                       headers={'Idempotency-Key':key} if key else {})


def complete(repository, settings):
    assert ResearchWorker(repository, settings).run_once()


def test_idempotency_replays_completed_jobs_and_rejects_payload_changes(account):
    client, repo, settings, _ = account
    first = submit(client, key='request-0001')
    run_id = first.json()['id']
    assert first.status_code == 202 and first.headers['idempotency-replayed'] == 'false'
    complete(repo, settings)
    replay = submit(client, key='request-0001')
    assert replay.status_code == 202 and replay.json()['id'] == run_id
    assert replay.json()['submission_reused'] is True
    assert replay.headers['idempotency-replayed'] == 'true'
    assert submit(client, topic='Changed payload', key='request-0001').status_code == 409
    assert client.get('/account/usage').json()['today']['submissions'] == 1
    # Completed work may be intentionally rerun with a new key.
    assert submit(client, key='request-0002').json()['id'] != run_id


def test_identical_active_submissions_with_different_keys_are_deduplicated(account):
    client, _, _, _ = account
    first = submit(client, key='request-0001').json()
    again = submit(client, key='request-0002').json()
    assert again['id'] == first['id'] and again['submission_reused']
    assert client.get('/research').json()['total'] == 1
    assert submit(client, instructions='A different focus').json()['id'] != first['id']
    assert submit(client, depth='quick').json()['id'] != first['id']


def test_parallel_admission_cannot_exceed_run_quota_or_duplicate(account):
    _, repo, settings, user = account
    config = settings.model_copy(update={'quota_daily_runs':1})
    def create(index):
        try:
            return repo.create(CreateResearch(topic=f'Topic {index}'), user_id=user['id'], settings=config)
        except AdmissionError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert sum(not isinstance(item,str) for item in results) == 1
    assert results.count('daily_run_limit') == 3
    assert repo.usage_for_user(user['id'], settings).today.submissions == 1


def test_parallel_duplicates_return_one_run(account):
    _, repo, settings, user = account
    def create(_):
        return repo.create(CreateResearch(topic=SAMPLE_QUERY), user_id=user['id'], settings=settings,
                           idempotency_key='same-request')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert len({run.id for run in results}) == 1
    assert sum(run.submission_reused for run in results) == 3


def test_token_reservation_settlement_and_missing_telemetry(account):
    client, repo, settings, _ = account
    first = submit(client).json()
    held = client.get('/account/usage').json()
    assert held['today']['reserved_tokens'] == 150000
    assert held['remaining']['tokens'] == 850000
    assert held['today']['reported_tokens'] is None
    assert held['today']['estimated_cost_usd'] is None
    complete(repo, settings)
    usage = client.get('/account/usage').json()
    report = client.get('/research/' + first['id'] + '/report').json()
    assert usage['today']['reserved_tokens'] == 0
    assert usage['today']['charged_tokens'] == report['usage']['reserved_tokens'] > 0
    assert usage['remaining']['tokens'] == 1000000-report['usage']['reserved_tokens']
    assert usage['today']['runs_without_reported_usage'] == 1  # Offline fixtures do not invent billing metadata.
    assert usage['today']['llm_calls'] == report['usage']['llm_calls']


def test_daily_token_active_and_cost_limits(account):
    client, repo, settings, user = account
    low = settings.model_copy(update={'quota_daily_tokens':60000})
    with pytest.raises(AdmissionError, match='lower depth'):
        repo.create(CreateResearch(topic=SAMPLE_QUERY), user_id=user['id'], settings=low)
    allowed = repo.create(CreateResearch(topic=SAMPLE_QUERY, depth='quick'), user_id=user['id'], settings=low)
    assert allowed.token_budget == 60000
    for index in range(2):
        assert submit(client, topic=f'Additional topic {index}').status_code == 202
    denied = submit(client, topic='Over the active limit')
    assert denied.status_code == 429 and denied.json()['error']['code'] == 'active_run_limit'
    assert denied.headers['retry-after'] == '5'
    repo.cancel(str(allowed.id))
    priced = Settings(_env_file=None, mock_mode=True, research_max_cost_usd=.10, quota_daily_cost_usd=.15,
        research_input_cost_per_million=1, research_output_cost_per_million=2, research_search_cost_usd=.001,
        quota_max_active_runs=10)
    repo.create(CreateResearch(topic='Priced first'), user_id=user['id'], settings=priced)
    with pytest.raises(AdmissionError) as denied_cost:
        repo.create(CreateResearch(topic='Priced second'), user_id=user['id'], settings=priced)
    assert denied_cost.value.code == 'daily_cost_limit'


def test_queued_cancel_releases_reservation_running_cancel_keeps_it(account):
    client, repo, settings, user = account
    queued = submit(client).json()['id']
    assert client.post('/research/' + queued + '/cancel').status_code == 200
    usage = repo.usage_for_user(user['id'], settings)
    assert usage.remaining.tokens == 1000000 and usage.remaining.runs == 19
    running = submit(client, topic='Running question').json()['id']
    assert repo.claim_next('worker',60)
    assert client.post('/research/' + running + '/cancel').status_code == 200
    usage = repo.usage_for_user(user['id'], settings)
    assert usage.today.charged_tokens == 150000 and usage.today.reserved_tokens == 0
    assert not repo.update_progress(running,'worker','search',.1,1,{},[])


def test_daily_reset_keeps_lifetime_and_global_active_slots(account, monkeypatch):
    client, repo, settings, user = account
    submit(client)
    tomorrow = utcnow() + timedelta(days=1)
    monkeypatch.setattr('deepresearch_ai.storage.sql.utcnow', lambda: tomorrow)
    usage = repo.usage_for_user(user['id'], settings)
    assert usage.today.submissions == 0 and usage.lifetime.submissions == 1
    assert usage.remaining.runs == 20 and usage.active_runs == 1
    assert usage.period_start.hour == 0 and usage.period_start.tzinfo == timezone.utc


def test_delete_research_cleans_graph_but_keeps_quota_and_idempotency(account):
    client, repo, settings, _ = account
    run_id = submit(client, key='delete-request').json()['id']
    complete(repo,settings)
    before = client.get('/account/usage').json()
    assert client.delete('/research/' + run_id).status_code == 204
    assert client.get('/research/' + run_id).status_code == 404
    assert client.get('/research').json()['total'] == 0
    after = client.get('/account/usage').json()
    assert after['today'] == before['today']
    assert submit(client, key='delete-request').json()['error']['code'] == 'submission_deleted'
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(SourceRow)) == 0
        assert session.scalar(select(func.count()).select_from(SourceSnapshotRow)) == 0
        assert session.scalar(select(UsageEntryRow)).run_id is None


def test_delete_running_research_fences_worker(account):
    client, repo, settings, user = account
    run_id = submit(client).json()['id']
    repo.claim_next('worker',60)
    assert client.delete('/research/' + run_id).status_code == 204
    assert not repo.renew(run_id,'worker',60)
    assert not repo.update_progress(run_id,'worker','writer',.9,1,{},[])
    assert repo.usage_for_user(user['id'],settings).today.charged_tokens == 150000


def test_account_deletion_requires_csrf_password_and_confirmation(account):
    client, repo, _, _ = account
    payload = {'password':PASSWORD,'confirmation':'DELETE'}
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.request('DELETE','/auth/me', json=payload).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    assert client.request('DELETE','/auth/me',json={'password':PASSWORD,'confirmation':'NO'}).status_code == 422
    assert client.request('DELETE','/auth/me',json={**payload,'password':'wrong password'}).status_code == 401
    assert client.get('/auth/me').status_code == 200
    assert client.request('DELETE','/auth/me',json=payload).status_code == 204
    assert client.get('/account/usage').status_code == 401
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(UserRow)) == 0
        assert session.scalar(select(func.count()).select_from(SessionRow)) == 0


def test_account_deletion_preserves_another_users_shared_sources(account, sign_in):
    alice, repo, settings, _ = account
    first = submit(alice).json()['id']; complete(repo, settings)
    with TestClient(alice.app) as bob:
        sign_in(bob,'bob@example.com')
        second = submit(bob, key='delete-request').json()['id']; complete(repo,settings)
        assert bob.delete('/research/' + first).status_code == 404
        assert bob.get('/account/usage').json()['today']['submissions'] == 1
        assert alice.request('DELETE','/auth/me', json={'password':PASSWORD,'confirmation':'DELETE'}).status_code == 204
        assert len(bob.get('/research/' + second + '/sources').json()) == 2
        assert bob.get('/research/' + second + '/report').status_code == 200
        assert bob.get('/research').json()['total'] == 1
        assert bob.delete('/research/' + second).status_code == 204
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(SourceRow)) == 0
        assert session.scalar(select(func.count()).select_from(UsageEntryRow)) == 1


def test_history_search_filters_apply_before_pagination(account):
    client, repo, settings, user = account
    # Seed trusted historical rows directly so this test is independent of admission caps.
    for index in range(25):
        request = CreateResearch(topic=f'Historical topic {index}', instructions='Find this special focus' if index == 0 else None,
                                 depth='quick' if index == 0 else 'deep')
        run = repo.create(request)
        with repo.sessions.begin() as session:
            session.execute(update(RunRow).where(RunRow.id == str(run.id)).values(
                user_id=user['id'], created_at=datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(days=index)))
    assert len(client.get('/research').json()['items']) == 20
    result = client.get('/research',params={'q':'SPECIAL FOCUS','depth':'quick','status':'queued',
        'created_from':'2026-01-01','created_to':'2026-01-01'}).json()
    assert result['total'] == 1 and result['items'][0]['topic'] == 'Historical topic 0'
    assert client.get('/research',params={'q':'%'}).json()['total'] == 0
    assert client.get('/research',params={'q':'_', 'offset':20}).json()['total'] == 0
    assert client.get('/research',params={'status':'invalid'}).status_code == 422
    assert client.get('/research',params={'created_from':'2026-02-01','created_to':'2026-01-01'}).status_code == 422


def test_new_endpoints_reject_unauthenticated_or_missing_csrf(account):
    client, _, _, _ = account
    run_id = submit(client).json()['id']
    client.headers.pop('X-CSRF-Token')
    assert client.delete('/research/' + run_id).status_code == 403
    client.cookies.clear()
    assert client.delete('/research/' + run_id).status_code == 401
    assert client.get('/account/usage').status_code == 401


def test_account_deletion_removes_all_private_relational_records(account):
    client, repo, settings, _ = account
    submit(client, key='request-0001'); complete(repo,settings)
    submit(client, topic='Queued run')
    assert client.request('DELETE','/auth/me',json={'password':PASSWORD,'confirmation':'DELETE'}).status_code == 204
    with repo.engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name != 'auth_rate_limits':
                assert connection.scalar(select(func.count()).select_from(table)) == 0, table.name


def test_daily_cost_quota_requires_per_run_budget_and_rates():
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match='daily cost quota'):
        Settings(_env_file=None, quota_daily_cost_usd=1)


def test_reported_tokens_and_estimates_are_distinct_and_pricing_is_preserved(account):
    _, repo, settings, user = account
    priced = settings.model_copy(update={'research_max_cost_usd':.1,
        'research_input_cost_per_million':1, 'research_output_cost_per_million':2, 'research_search_cost_usd':.001})
    run = repo.create(CreateResearch(topic=SAMPLE_QUERY),user_id=user['id'],settings=priced)
    repo.claim_next('worker',60)
    report = sample_report().model_copy(update={'usage':{'reserved_tokens':1000,'reported_tokens':200,
        'usage_reported_calls':2,'llm_calls':3,'searches':5,'estimated_cost_usd':.002}})
    assert repo.finish(str(run.id),'worker',report)
    usage = repo.usage_for_user(user['id'],priced)
    assert usage.today.reported_tokens == 200 and usage.today.admitted_tokens == 1000
    assert usage.today.charged_tokens == 1000
    assert usage.today.estimated_cost_usd == .002 and usage.remaining.cost_usd is None


def test_worker_cannot_raise_its_submission_token_budget_or_change_recorded_rates(account):
    _, repo, settings, user = account
    admission = settings.model_copy(update={'research_max_tokens':60000,'research_max_cost_usd':.1,
        'research_input_cost_per_million':1,'research_output_cost_per_million':2,'research_search_cost_usd':.001})
    run = repo.create(CreateResearch(topic=SAMPLE_QUERY),user_id=user['id'],settings=admission)
    later = settings.model_copy(update={'research_max_tokens':999999,'research_max_cost_usd':10,
        'research_input_cost_per_million':10,'research_output_cost_per_million':20,'research_search_cost_usd':.1})
    observed = []
    def factory(config, **callbacks):
        observed.append(config)
        return GraphResearchService(config, **callbacks)
    assert ResearchWorker(repo,later,service_factory=factory).run_once()
    assert observed[0].research_max_tokens == 60000
    assert observed[0].research_max_cost_usd == .1
    assert observed[0].research_input_cost_per_million == 1
    assert observed[0].research_search_cost_usd == .001
    assert repo.get(str(run.id)).status == 'completed'


def test_expired_worker_reservations_are_settled_conservatively(account):
    client, repo, settings, user = account
    run_id = submit(client).json()['id']
    repo.claim_next('crashed-worker',60)
    with repo.sessions.begin() as session:
        session.execute(update(RunRow).where(RunRow.id == run_id).values(lease_expires_at=utcnow()-timedelta(seconds=1)))
    assert repo.expire_abandoned() == 1
    usage = repo.usage_for_user(user['id'],settings)
    assert usage.active_runs == 0 and usage.today.reserved_tokens == 0 and usage.today.charged_tokens == 150000


def test_deleting_queued_research_does_not_reset_daily_run_count(account):
    _, repo, settings, user = account
    config = settings.model_copy(update={'quota_daily_runs':1})
    run = repo.create(CreateResearch(topic=SAMPLE_QUERY),user_id=user['id'],settings=config)
    assert repo.delete_for_user(str(run.id),user['id'])
    with pytest.raises(AdmissionError) as denied:
        repo.create(CreateResearch(topic='A new question'),user_id=user['id'],settings=config)
    assert denied.value.code == 'daily_run_limit'
    usage = repo.usage_for_user(user['id'],config)
    assert usage.remaining.tokens == 1000000 and usage.remaining.runs == 0

"""Durable recovery, live events, administrator isolation and private exports."""
import asyncio
from datetime import timedelta
import io
import json
import zipfile

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import Field
import pytest
from sqlalchemy import func, select, update

from deepresearch_ai.api.app import create_app
from deepresearch_ai.api.events import progress_events
from deepresearch_ai.config import Settings
from deepresearch_ai.models import ResearchRequest
from deepresearch_ai.services.mock_data import SAMPLE_QUERY
from deepresearch_ai.services.mock_llm import MockChatModel
from deepresearch_ai.services.workflow import GraphResearchService
from deepresearch_ai.storage.recovery import DurableExecution
from deepresearch_ai.storage.sql import SQLRunRepository, utcnow
from deepresearch_ai.storage.tables import CheckpointRow, ProviderCallRow, RunRow, UsageEntryRow, UserRow
from deepresearch_ai.worker import ResearchWorker


class ProcessCrash(BaseException):
    pass


class CountingModel(MockChatModel):
    schemas: list[str] = Field(default_factory=list)

    def with_structured_output(self,schema,**options):
        runner = super().with_structured_output(schema,**options)
        model = self
        class Counted:
            def invoke(self,messages):
                model.schemas.append(schema.__name__)
                return runner.invoke(messages)
        return Counted()


@pytest.fixture
def backend(tmp_path,sign_in):
    repo = SQLRunRepository('sqlite:///'+(tmp_path/'recovery.db').as_posix())
    settings = Settings(_env_file=None,mock_mode=True,progress_stream_seconds=1)
    with TestClient(create_app(settings,repository=repo)) as client:
        user = sign_in(client)
        yield client,repo,settings,user
    repo.close()


def submit(client):
    response = client.post('/research',json={'topic':SAMPLE_QUERY})
    assert response.status_code == 202
    return response.json()['id']


def expire(repo,run_id):
    with repo.sessions.begin() as session:
        session.execute(update(RunRow).where(RunRow.id == run_id).values(lease_expires_at=utcnow()-timedelta(seconds=1)))
    repo.expire_abandoned()


def test_resume_skips_completed_planner_and_preserves_allowance(backend):
    client,repo,settings,_ = backend
    run_id = submit(client)
    model = CountingModel()
    def factory(config,**callbacks):
        original = callbacks['on_update']
        def crash(state):
            original(state)
            if state['step_status']['planner'] == 'completed':
                raise ProcessCrash()
        return GraphResearchService(config,model=model,on_step=callbacks['on_step'],on_update=crash)
    with pytest.raises(ProcessCrash):
        ResearchWorker(repo,settings,service_factory=factory).run_once()
    expire(repo,run_id)
    assert client.get('/research/'+run_id).json()['resumable'] is True
    assert client.post('/research/'+run_id+'/resume').status_code == 202
    assert ResearchWorker(repo,settings,service_factory=lambda cfg,**kw:GraphResearchService(cfg,model=model,**kw)).run_once()
    assert model.schemas.count('PlannerOutput') == 1
    assert client.get('/research/'+run_id).json()['status'] == 'completed'
    assert client.get('/account/usage').json()['today']['submissions'] == 1


def test_saved_provider_result_is_reused_after_crash_before_node_commit(backend,monkeypatch):
    client,repo,settings,_ = backend
    run_id = submit(client); model = CountingModel()
    original = DurableExecution._receipt
    crashed = False
    def receipt(self,*args,**kwargs):
        nonlocal crashed
        original(self,*args,**kwargs)
        if not crashed:
            crashed = True
            raise ProcessCrash()
    monkeypatch.setattr(DurableExecution,'_receipt',receipt)
    factory = lambda cfg,**kw:GraphResearchService(cfg,model=model,**kw)
    with pytest.raises(ProcessCrash):
        ResearchWorker(repo,settings,service_factory=factory).run_once()
    expire(repo,run_id)
    assert client.post('/research/'+run_id+'/resume').status_code == 202
    assert ResearchWorker(repo,settings,service_factory=factory).run_once()
    assert model.schemas.count('PlannerOutput') == 1
    assert repo.get(run_id).report.usage['llm_calls'] == len(model.schemas)


def test_settled_token_receipt_is_restored_without_charging_cached_response_twice(backend, monkeypatch):
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableLambda

    client, repo, settings, _ = backend
    run_id = submit(client)
    original_output = MockChatModel.with_structured_output
    def output(model, schema, **options):
        runner = original_output(model, schema, **options)
        def invoke(messages):
            result = runner.invoke(messages)
            result['raw'] = AIMessage(content='', usage_metadata={
                'input_tokens': 500, 'output_tokens': 200, 'total_tokens': 700})
            return result
        return RunnableLambda(invoke)
    monkeypatch.setattr(MockChatModel, 'with_structured_output', output)
    original_receipt = DurableExecution._receipt
    crashed = False
    def receipt(self, *args, **kwargs):
        nonlocal crashed
        original_receipt(self, *args, **kwargs)
        if not crashed:
            crashed = True
            raise ProcessCrash()
    monkeypatch.setattr(DurableExecution, '_receipt', receipt)
    model = CountingModel()
    factory = lambda config, **callbacks: GraphResearchService(config, model=model, **callbacks)
    with pytest.raises(ProcessCrash):
        ResearchWorker(repo, settings, service_factory=factory).run_once()
    expire(repo, run_id)
    assert client.post('/research/'+run_id+'/resume').status_code == 202
    assert ResearchWorker(repo, settings, service_factory=factory).run_once()
    usage = repo.get(run_id).report.usage
    assert usage['reserved_tokens'] == usage['reported_tokens'] == 2100
    assert usage['llm_calls'] == usage['usage_reported_calls'] == 3
    assert model.schemas.count('PlannerOutput') == 1


def test_verification_format_repair_reuses_saved_response_after_interruption(backend, monkeypatch):
    from langchain_core.runnables import RunnableLambda
    from deepresearch_ai.models.agents import VerificationOutput

    client, repo, settings, _ = backend
    run_id = submit(client)
    original_output = MockChatModel.with_structured_output
    def output(model, schema, **options):
        runner = original_output(model, schema, **options)
        def invoke(messages):
            result = runner.invoke(messages)
            if schema is VerificationOutput and 'format_repair' not in json.loads(messages[-1].content):
                # Reproduce a valid JSON response that only assessed supporting
                # passages, omitting other potentially contradictory passages.
                for claim in result['parsed'].results:
                    claim.passage_assessments = [item for item in claim.passage_assessments
                                                if item.relation == 'supports']
                    claim.evidence_ids = [item.evidence_id for item in claim.passage_assessments]
            return result
        return RunnableLambda(invoke)
    monkeypatch.setattr(MockChatModel, 'with_structured_output', output)
    model = CountingModel()
    original_receipt = DurableExecution._receipt
    crashed = False
    def receipt(self, *args, **kwargs):
        nonlocal crashed
        original_receipt(self, *args, **kwargs)
        if not crashed and model.schemas and model.schemas[-1] == 'VerificationOutput':
            crashed = True
            raise ProcessCrash()
    monkeypatch.setattr(DurableExecution, '_receipt', receipt)
    factory = lambda config, **callbacks: GraphResearchService(config, model=model, **callbacks)
    with pytest.raises(ProcessCrash):
        ResearchWorker(repo, settings, service_factory=factory).run_once()
    expire(repo, run_id)
    assert client.post('/research/'+run_id+'/resume').status_code == 202
    assert ResearchWorker(repo, settings, service_factory=factory).run_once()
    run = repo.get(run_id)
    assert run.status.value == 'completed' and run.report.status == 'complete'
    assert model.schemas.count('VerificationOutput') == 2  # original + repair; no replayed call
    assert run.report.usage['llm_calls'] == 4
    assert run.report.research_iteration == 1


def test_unknown_provider_outcome_blocks_resume_and_hides_raw_exception(backend):
    client,repo,settings,_ = backend
    run_id = submit(client)
    class TimedOut(MockChatModel):
        def with_structured_output(self,*args,**kwargs):
            class Runner:
                def invoke(self,_messages):
                    raise TimeoutError('private-api-key-must-not-leak')
            return Runner()
    assert ResearchWorker(repo,settings,service_factory=lambda cfg,**kw:GraphResearchService(cfg,model=TimedOut(),**kw)).run_once()
    run = client.get('/research/'+run_id).json()
    assert run['status'] == 'failed' and run['error_code'] == 'provider_outcome_unknown'
    assert not run['resumable'] and 'may already be billed' in run['resume_blocked_reason']
    assert 'timed out' in run['error'] and 'private-api-key' not in json.dumps(run)
    response = client.post('/research/'+run_id+'/resume')
    assert response.status_code == 409 and response.json()['error']['code'] == 'resume_blocked'


def test_resume_is_bounded_and_requires_ownership_and_csrf(backend,sign_in):
    client,repo,settings,_ = backend
    run_id = submit(client)
    def factory(config,**callbacks):
        def crash(_name,_iteration):
            raise ProcessCrash()
        return GraphResearchService(config,on_step=crash,on_update=callbacks['on_update'])
    for index in range(3):
        with pytest.raises(ProcessCrash):
            ResearchWorker(repo,settings,service_factory=factory).run_once()
        expire(repo,run_id)
        if index < 2:
            assert client.post('/research/'+run_id+'/resume').status_code == 202
    response = client.post('/research/'+run_id+'/resume')
    assert response.status_code == 409 and response.json()['error']['code'] == 'resume_limit'
    with TestClient(client.app) as other:
        sign_in(other,'other@example.com')
        assert other.post('/research/'+run_id+'/resume').status_code == 404
    client.headers.pop('X-CSRF-Token')
    assert client.post('/research/'+run_id+'/resume').status_code == 403


def test_completed_sse_stream_has_progress_and_no_sources_or_provider_config(backend):
    client,repo,settings,_ = backend
    run_id = submit(client)
    assert ResearchWorker(repo,settings).run_once()
    response = client.get('/research/'+run_id+'/events')
    assert response.status_code == 200 and response.headers['content-type'].startswith('text/event-stream')
    assert response.headers['x-accel-buffering'] == 'no'
    assert 'event: progress' in response.text and 'event: end' in response.text
    data = json.loads(response.text.split('event: progress\ndata: ')[1].split('\n\n')[0])
    assert data['status'] == 'completed' and 'report' not in data and 'cost_rates' not in data


def test_sse_checks_revocation_after_connection_opens(backend):
    client,repo,_,user = backend
    run_id = submit(client)
    class Request:
        headers = {}
        async def is_disconnected(self): return False
    class Public:
        id = user['id']
    calls = 0
    def authenticate(_request):
        nonlocal calls
        calls += 1
        if calls > 1: raise HTTPException(401)
        return Public()
    async def collect():
        return [part async for part in progress_events(Request(),repo,authenticate,user['id'],run_id,2)]
    output = ''.join(asyncio.run(collect()))
    assert 'event: progress' in output and 'event: auth_required' in output


def test_admin_routes_require_explicit_role_and_omit_private_topics(backend):
    client,repo,settings,user = backend
    run_id = submit(client)
    assert client.get('/admin/overview').status_code == 403
    assert client.get('/auth/me').json()['user']['is_admin'] is False
    with repo.sessions.begin() as session:
        session.execute(update(UserRow).where(UserRow.id == user['id']).values(is_admin=True))
        session.execute(update(RunRow).where(RunRow.id == run_id).values(updated_at=utcnow()-timedelta(seconds=200)))
    result = client.get('/admin/overview')
    assert result.status_code == 200 and result.json()['counts']['queued'] == 1
    assert any(alert['code'] == 'queue_wait' for alert in result.json()['alerts'])
    jobs = client.get('/admin/runs').json()
    assert jobs['items'][0]['id'] == run_id
    assert SAMPLE_QUERY not in json.dumps(jobs) and 'email' not in json.dumps(jobs)
    with repo.sessions.begin() as session:
        session.execute(update(UserRow).where(UserRow.id == user['id']).values(is_admin=False))
    assert client.get('/admin/runs').status_code == 403


def test_admin_spending_alert_uses_estimates_and_reservations(backend):
    client,repo,settings,user = backend
    with repo.sessions.begin() as session:
        session.execute(update(UserRow).where(UserRow.id == user['id']).values(is_admin=True))
    priced = settings.model_copy(update={'admin_daily_spend_alert_usd':.05,'research_max_cost_usd':.1,
        'research_input_cost_per_million':1,'research_output_cost_per_million':2,'research_search_cost_usd':.001})
    with TestClient(create_app(priced,repository=repo)) as api:
        api.cookies.update(client.cookies)
        api.headers.update(client.headers)
        assert api.post('/research',json={'topic':SAMPLE_QUERY}).status_code == 202
        overview = api.get('/admin/overview').json()
    assert overview['estimated_reserved_exposure_usd'] == .1
    assert any(alert['code'] == 'spending_threshold' for alert in overview['alerts'])


def test_account_export_includes_own_data_and_excludes_secrets_and_other_users(backend,sign_in):
    client,repo,settings,_ = backend
    run_id = submit(client)
    ResearchWorker(repo,settings).run_once()
    with TestClient(client.app) as other:
        sign_in(other,'other@example.com')
        other_id = submit(other)
    response = client.get('/account/export')
    assert response.status_code == 200 and response.headers['content-type'] == 'application/zip'
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert f'research/{run_id}/report.md' in names
        assert not any(other_id in name for name in names)
        profile = json.loads(archive.read('account.json'))
        assert profile['email'] == 'tester@example.com' and 'password_hash' not in profile
        contents = b'\n'.join(archive.read(name) for name in names)
        assert b'A private test passphrase' not in contents and b'token_hash' not in contents
        assert not any('checkpoint' in name or 'provider_calls' in name for name in names)
    assert client.get('/account/export',params={'created_from':'2020-01-01','created_to':'2020-01-02'}).status_code == 200
    assert client.get('/account/export',params={'created_from':'2026-02-01','created_to':'2026-01-01'}).status_code == 422


def test_account_export_limits_and_deletion_remove_recovery_data(backend):
    client,repo,settings,_ = backend
    run_id = submit(client)
    ResearchWorker(repo,settings).run_once()
    tiny = settings.model_copy(update={'account_export_max_bytes':1024})
    with TestClient(create_app(tiny,repository=repo)) as api:
        api.cookies.update(client.cookies)
        response = api.get('/account/export')
        assert response.status_code == 413 and response.json()['error']['code'] == 'export_too_large'
    assert client.delete('/research/'+run_id).status_code == 204
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CheckpointRow)) == 0
        assert session.scalar(select(func.count()).select_from(ProviderCallRow)) == 0


def test_worker_mode_cannot_silently_switch_from_mock_to_real_on_resume(backend):
    client,repo,settings,_ = backend
    run_id = submit(client)
    def factory(config,**callbacks):
        def crash(_node,_iteration): raise ProcessCrash()
        return GraphResearchService(config,on_step=crash,on_update=callbacks['on_update'])
    with pytest.raises(ProcessCrash): ResearchWorker(repo,settings,service_factory=factory).run_once()
    expire(repo,run_id)
    assert client.post('/research/'+run_id+'/resume').status_code == 202
    # No real credentials exist in this test; the mode check must precede SDK setup.
    assert ResearchWorker(repo,settings.model_copy(update={'mock_mode':False})).run_once()
    assert repo.get(run_id).error_code == 'resume_mode_mismatch'
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ProviderCallRow)) == 0


def test_search_and_retrieval_receipts_avoid_repeating_completed_work(backend,monkeypatch):
    from deepresearch_ai.services.search import MockWebSearch
    from deepresearch_ai.services.retrieval import MockSourceRetriever
    client,repo,settings,_ = backend
    run_id = submit(client)
    class Search(MockWebSearch):
        calls = 0
        def search(self,*args,**kwargs):
            self.calls += 1
            return super().search(*args,**kwargs)
    class Retriever(MockSourceRetriever):
        calls = 0
        def fetch(self,source):
            self.calls += 1
            return super().fetch(source)
    search = Search(); retriever = Retriever(settings)
    original = DurableExecution._receipt
    crashed = False
    def receipt(self,*args,**kwargs):
        nonlocal crashed
        original(self,*args,**kwargs)
        with repo.sessions() as session:
            call = session.get(ProviderCallRow,args[0])
            if call.kind == 'fetch' and not crashed:
                crashed = True
                raise ProcessCrash()
    monkeypatch.setattr(DurableExecution,'_receipt',receipt)
    factory = lambda config,**callbacks: GraphResearchService(config,search=search,retriever_factory=lambda:retriever,**callbacks)
    with pytest.raises(ProcessCrash): ResearchWorker(repo,settings,service_factory=factory).run_once()
    expire(repo,run_id)
    assert client.post('/research/'+run_id+'/resume').status_code == 202
    assert ResearchWorker(repo,settings,service_factory=factory).run_once()
    assert search.calls == 3 and retriever.calls == 2
    assert repo.get(run_id).report.usage['page_requests'] == 2


def test_registration_cannot_grant_admin_and_local_role_command_can(backend,monkeypatch):
    from deepresearch_ai.db import main
    import sys
    client,repo,_,_ = backend
    csrf = client.get('/auth/csrf').json()['csrf_token']
    response = client.post('/auth/register',headers={'X-CSRF-Token':csrf},json={
        'name':'Attacker','email':'attacker@example.com','password':'A private test passphrase!','is_admin':True})
    assert response.status_code == 422
    monkeypatch.setenv('DATABASE_URL',repo.engine.url.render_as_string(hide_password=False))
    monkeypatch.setattr(sys,'argv',['db','set-admin','--email','TESTER@example.com'])
    main()
    assert client.get('/admin/overview').status_code == 200
    monkeypatch.setattr(sys,'argv',['db','set-admin','--email','tester@example.com','--revoke'])
    main()
    assert client.get('/admin/overview').status_code == 403


def test_events_and_exports_are_user_scoped_and_require_authentication(backend,sign_in):
    client,repo,settings,_ = backend
    run_id = submit(client)
    ResearchWorker(repo,settings).run_once()
    with TestClient(client.app) as other:
        assert other.get('/account/export').status_code == 401
        assert other.get('/research/'+run_id+'/events').status_code == 401
        sign_in(other,'second@example.com')
        assert other.get('/research/'+run_id+'/events').status_code == 404


def test_changed_prompt_does_not_replay_a_saved_provider_call(backend,monkeypatch):
    from deepresearch_ai.agents import planner
    client,repo,settings,_ = backend
    run_id = submit(client); model = CountingModel()
    original = DurableExecution._receipt
    crashed = False
    def receipt(self,*args,**kwargs):
        nonlocal crashed
        original(self,*args,**kwargs)
        if not crashed:
            crashed = True
            raise ProcessCrash()
    monkeypatch.setattr(DurableExecution,'_receipt',receipt)
    factory = lambda config,**callbacks:GraphResearchService(config,model=model,**callbacks)
    with pytest.raises(ProcessCrash): ResearchWorker(repo,settings,service_factory=factory).run_once()
    expire(repo,run_id)
    monkeypatch.setattr(planner,'PLANNER_INSTRUCTIONS',planner.PLANNER_INSTRUCTIONS+' Changed workflow.')
    assert client.post('/research/'+run_id+'/resume').status_code == 202
    assert ResearchWorker(repo,settings,service_factory=factory).run_once()
    assert model.schemas == ['PlannerOutput']
    assert repo.get(run_id).error_code == 'resume_incompatible'
    assert repo.get(run_id).resumable is False

"""Password/cookie authentication and access control use no external services."""
from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, update

from deepresearch_ai.api.app import create_app
from deepresearch_ai.config import Settings
from deepresearch_ai.models import ResearchRequest
from deepresearch_ai.services.workflow import GraphResearchService
from deepresearch_ai.storage.sql import SQLRunRepository, utcnow
from deepresearch_ai.storage.tables import SessionRow, UserRow


@pytest.fixture
def auth_backend(tmp_path):
    repo = SQLRunRepository('sqlite:///' + (tmp_path / 'auth.db').as_posix())
    settings = Settings(_env_file=None, mock_mode=True)
    with TestClient(create_app(settings, repository=repo)) as client:
        yield client, repo, settings
    repo.close()


def register(client, email='person@example.com', password='A memorable private passphrase!'):
    csrf = client.get('/auth/csrf').json()['csrf_token']
    return client.post('/auth/register', headers={'X-CSRF-Token': csrf},
                       json={'email':email,'name':'Researcher','password':password})


def test_registration_normalizes_email_and_stores_only_argon2_and_hashed_sessions(auth_backend):
    client, repo, _ = auth_backend
    response = register(client, '  PERSON@Example.COM ')
    assert response.status_code == 201
    assert response.json()['user']['email'] == 'person@example.com'
    assert 'password' not in response.text and 'token_hash' not in response.text
    cookie = client.cookies.get('dr_session')
    assert 'HttpOnly' in response.headers.get('set-cookie') and 'SameSite=lax' in response.headers.get('set-cookie')
    with repo.sessions() as session:
        user = session.scalar(select(UserRow))
        stored_session = session.scalar(select(SessionRow))
        assert user.password_hash.startswith('$argon2id$')
        assert 'A memorable' not in user.password_hash
        assert stored_session.token_hash != cookie
    assert client.get('/auth/me').json()['user']['email'] == 'person@example.com'


@pytest.mark.parametrize('path', ['/research', '/research/' + str(uuid4()),
    '/research/' + str(uuid4()) + '/sources', '/research/' + str(uuid4()) + '/report',
    '/research/' + str(uuid4()) + '/report.md'])
def test_anonymous_cannot_read_research(auth_backend, path):
    client, _, _ = auth_backend
    assert client.get(path).status_code == 401


def test_anonymous_cannot_create_or_cancel_research(auth_backend):
    client, repo, _ = auth_backend
    assert client.post('/research', json={'topic':'A valid topic'}).status_code == 401
    assert client.post('/research/' + str(uuid4()) + '/cancel').status_code == 401
    assert repo.list_runs(20,0).total == 0


def test_login_rotates_sessions_and_wrong_credentials_are_generic(auth_backend):
    client, _, _ = auth_backend
    registered = register(client)
    old_cookie = client.cookies.get('dr_session')
    csrf = client.get('/auth/csrf').json()['csrf_token']
    for email in ('person@example.com','missing@example.com'):
        response = client.post('/auth/login', headers={'X-CSRF-Token':csrf},
            json={'email':email,'password':'wrong-password'})
        assert response.status_code == 401 and response.json()['error']['message'] == 'Invalid email or password.'
    response = client.post('/auth/login', headers={'X-CSRF-Token':csrf},
        json={'email':'PERSON@EXAMPLE.COM','password':'A memorable private passphrase!'})
    assert response.status_code == 200 and client.cookies.get('dr_session') != old_cookie
    with TestClient(client.app) as old:
        old.cookies.set('dr_session',old_cookie)
        assert old.get('/auth/me').status_code == 401


def test_csrf_is_required_for_register_login_research_cancel_and_logout(auth_backend):
    client, _, _ = auth_backend
    assert client.post('/auth/login', json={'email':'person@example.com','password':'password'}).status_code == 403
    response = register(client)
    token = response.json()['csrf_token']
    assert client.post('/research', json={'topic':'A valid question'}).status_code == 403
    assert client.post('/auth/logout').status_code == 403
    headers={'X-CSRF-Token':token}
    created = client.post('/research', headers=headers, json={'topic':'A valid question'}).json()
    assert client.post('/research/' + created['id'] + '/cancel').status_code == 403
    assert client.post('/research', headers={**headers,'Origin':'https://attacker.example'}, json={'topic':'A valid question'}).status_code == 403
    assert client.post('/auth/logout', headers=headers).status_code == 204
    assert client.get('/auth/me').status_code == 401


def test_expired_and_disabled_accounts_cannot_use_existing_sessions(auth_backend):
    client, repo, _ = auth_backend
    register(client)
    with repo.sessions.begin() as session:
        session.execute(update(SessionRow).values(expires_at=utcnow()-timedelta(seconds=1)))
    assert client.get('/research').status_code == 401
    response=register(client,'other@example.com')
    with repo.sessions.begin() as session:
        session.execute(update(UserRow).where(UserRow.id==response.json()['user']['id']).values(is_active=False))
    assert client.get('/auth/me').status_code == 401


def test_other_users_cannot_list_read_export_sources_or_cancel(auth_backend):
    alice, repo, settings = auth_backend
    first = register(alice,'alice@example.com').json()
    alice.headers['X-CSRF-Token']=first['csrf_token']
    run=alice.post('/research',json={'topic':'How does RAG ground an answer?'}).json()
    report=GraphResearchService(settings).run(ResearchRequest(query=run['topic']))
    repo.claim_next('worker',60); repo.finish(run['id'],'worker',report)
    with TestClient(alice.app) as bob:
        registered=register(bob,'bob@example.com').json()
        bob.headers['X-CSRF-Token']=registered['csrf_token']
        assert bob.get('/research').json()['total']==0
        for suffix in ('','/sources','/report','/report.md'):
            assert bob.get('/research/'+run['id']+suffix).status_code==404
        assert bob.post('/research/'+run['id']+'/cancel').status_code==404
    assert alice.get('/research').json()['total']==1
    assert alice.get('/research/'+run['id']+'/report.md').status_code==200


def test_auth_rate_limits_are_persistent_and_include_retry_after(auth_backend):
    client, _, _ = auth_backend
    csrf=client.get('/auth/csrf').json()['csrf_token']
    for _ in range(10):
        assert client.post('/auth/login',headers={'X-CSRF-Token':csrf},json={
            'email':'missing@example.com','password':'wrong password'}).status_code==401
    limited=client.post('/auth/login',headers={'X-CSRF-Token':csrf},json={
        'email':'missing@example.com','password':'wrong password'})
    assert limited.status_code==429 and limited.headers['retry-after']=='900'


def test_password_length_email_and_blank_names_are_validated(auth_backend):
    client, _, _ = auth_backend
    assert register(client,password='short').status_code==422
    assert register(client,email='not-an-email').status_code==422
    csrf=client.get('/auth/csrf').json()['csrf_token']
    assert client.post('/auth/register',headers={'X-CSRF-Token':csrf},json={
        'name':'   ','email':'person@example.com','password':'A memorable private passphrase!'}).status_code==422


def test_registration_can_be_disabled_and_secure_cookies_enabled(tmp_path):
    settings=Settings(_env_file=None,mock_mode=True,auth_allow_registration=False,
        auth_cookie_secure=True,database_url='sqlite:///'+(tmp_path/'secure.db').as_posix())
    with TestClient(create_app(settings),base_url='https://testserver') as client:
        csrf=client.get('/auth/csrf')
        assert 'Secure' in csrf.headers['set-cookie'] and '__Host-dr_preauth=' in csrf.headers['set-cookie']
        denied=client.post('/auth/register',headers={'X-CSRF-Token':csrf.json()['csrf_token']},json={
            'name':'Researcher','email':'user@example.com','password':'A memorable private passphrase!'})
        assert denied.status_code==403

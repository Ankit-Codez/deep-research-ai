"""The built React dashboard shares an origin without shadowing API routes."""
from fastapi.testclient import TestClient

from deepresearch_ai.api.app import create_app
from deepresearch_ai.config import Settings


def test_dashboard_and_assets_are_served_alongside_the_existing_api(tmp_path, sign_in):
    build = tmp_path / 'dashboard'
    (build / 'assets').mkdir(parents=True)
    (build / 'index.html').write_text('<html><title>DeepResearch AI</title><div id="root"></div></html>')
    (build / 'assets' / 'app.js').write_text('console.log("dashboard");')
    settings = Settings(mock_mode=True, frontend_dist_dir=build,
                        database_url='sqlite:///' + (tmp_path / 'history.db').as_posix())
    with TestClient(create_app(settings)) as client:
        sign_in(client)
        page = client.get('/')
        assert page.status_code == 200 and '<div id="root">' in page.text
        assert page.headers['x-content-type-options'] == 'nosniff'
        assert client.get('/assets/app.js').status_code == 200
        assert client.get('/health').json() == {'status': 'ok'}
        run = client.post('/research', json={'topic': 'How do citations ground research?'}).json()
        assert run['status'] == 'queued'
        assert client.get('/research').json()['total'] == 1
        assert client.get('/research/' + run['id']).json()['id'] == run['id']
        assert client.get('/docs').status_code == 200


def test_backend_remains_usable_before_the_frontend_is_built(tmp_path):
    settings = Settings(mock_mode=True, frontend_dist_dir=tmp_path / 'missing',
                        database_url='sqlite:///' + (tmp_path / 'history.db').as_posix())
    with TestClient(create_app(settings)) as client:
        assert client.get('/health').status_code == 200
        assert client.get('/').status_code == 404


def test_dashboard_cannot_read_files_outside_its_build_directory(tmp_path):
    build = tmp_path / 'dashboard'
    build.mkdir()
    (build / 'index.html').write_text('<html>Dashboard</html>')
    (tmp_path / 'private.env').write_text('SENSITIVE_TEST_VALUE')
    settings = Settings(mock_mode=True, frontend_dist_dir=build,
                        database_url='sqlite:///' + (tmp_path / 'history.db').as_posix())
    with TestClient(create_app(settings)) as client:
        for path in ('/.env', '/private.env', '/%2e%2e/private.env'):
            response = client.get(path)
            assert response.status_code == 404
            assert 'SENSITIVE_TEST_VALUE' not in response.text

"""Hermetic tests: no developer credentials, local dotenv files, or network."""

import os
import socket

import pytest


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for variable in list(os.environ):
        if variable.upper() in {
            "LLM_PROVIDER", "LLM_MODEL", "LLM_TEMPERATURE", "LLM_TIMEOUT_SECONDS",
            "LLM_MAX_RETRIES", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LOG_LEVEL",
            "MOCK_MODE", "GOOGLE_API_KEY", "GEMINI_API_KEY", "GEMINI_THINKING_LEVEL", "GOOGLE_GENAI_USE_VERTEXAI",
            "SEARCH_PROVIDER", "TAVILY_API_KEY", "DATABASE_URL", "BACKEND_URL", "FRONTEND_DIST_DIR", "LLM_MAX_OUTPUT_TOKENS", "PROVIDER_BACKOFF_SECONDS",
        } or variable.upper().startswith(("LANGCHAIN_", "LANGSMITH_", "RESEARCH_", "PROVIDER_", "WORKER_", "AUTH_", "APP_ENV", "QUOTA_", "ADMIN_", "ACCOUNT_EXPORT_", "PROGRESS_")):
            monkeypatch.delenv(variable, raising=False)
    monkeypatch.chdir(tmp_path)

    # Windows asyncio uses a local socket pair to wake its event loop. Permit
    # that stdlib operation while keeping application connections blocked.
    original_socketpair = socket.socketpair
    original_connect = socket.socket.connect

    def local_socketpair(*args, **kwargs):
        with monkeypatch.context() as local_patch:
            local_patch.setattr(socket.socket, "connect", original_connect)
            return original_socketpair(*args, **kwargs)

    monkeypatch.setattr(socket, "socketpair", local_socketpair)

    def block_network(*args, **kwargs):
        raise AssertionError("Automated tests must not make network requests.")

    monkeypatch.setattr(socket.socket, "connect", block_network)
    monkeypatch.setattr(socket.socket, "connect_ex", block_network)
    monkeypatch.setattr(socket, "create_connection", block_network)


@pytest.fixture
def sign_in():
    def authenticate(client, email='tester@example.com'):
        token = client.get('/auth/csrf').json()['csrf_token']
        headers = {'X-CSRF-Token': token}
        payload = {'email': email, 'name': 'Test User', 'password': 'A private test passphrase 2026!'}
        response = client.post('/auth/register', json=payload, headers=headers)
        if response.status_code == 409:
            response = client.post('/auth/login', json={key: payload[key] for key in ('email','password')}, headers=headers)
        assert response.status_code in (200,201), response.text
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        return response.json()['user']
    return authenticate

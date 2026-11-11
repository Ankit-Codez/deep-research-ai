"""Provider diagnostics must be actionable without leaking raw exception details."""
import httpx
import pytest

from deepresearch_ai.services.runtime import provider_failure_message


@pytest.mark.parametrize(('status', 'expected'), [
    (400, 'request format or settings'), (401, 'denied access'), (403, 'denied access'), (429, 'quota'),
    (500, 'temporarily unavailable'), (503, 'temporarily unavailable'), (504, 'temporarily unavailable'),
])
def test_wrapped_provider_status_has_safe_actionable_message(status, expected):
    cause = RuntimeError('sensitive-api-key-and-request-body')
    cause.code = status
    wrapped = RuntimeError('sensitive wrapper message')
    wrapped.__cause__ = cause
    message = provider_failure_message(wrapped)
    assert expected in message
    assert 'sensitive' not in message


def test_windows_socket_permission_error_explains_worker_permissions():
    cause = PermissionError('secret-request-url')
    cause.winerror = 10013
    wrapped = httpx.ConnectError('secret-api-key')
    wrapped.__cause__ = cause
    message = provider_failure_message(wrapped)
    assert 'Windows blocked' in message and 'Restart the worker' in message
    assert 'secret' not in message


@pytest.mark.parametrize(('error', 'expected'), [
    (httpx.ReadTimeout('secret-key'), 'timed out'),
    (httpx.ConnectError('secret-key'), 'network access'),
    (RuntimeError('secret-key'), 'Research planning failed.'),
])
def test_other_errors_do_not_echo_sdk_messages(error, expected):
    message = provider_failure_message(error)
    assert expected in message and 'secret-key' not in message


def test_http_response_status_is_recognized_without_logging_response_body():
    error = httpx.HTTPStatusError('sensitive details', request=httpx.Request('POST', 'https://example.com'),
                                  response=httpx.Response(503, text='secret-token'))
    assert 'temporarily unavailable' in provider_failure_message(error)
    assert 'secret' not in provider_failure_message(error)


@pytest.mark.parametrize('action', ['Structured evidence extraction', 'Claim verification'])
def test_provider_diagnostic_preserves_failed_stage(action):
    error = RuntimeError('secret')
    error.code = 503
    message = provider_failure_message(error, action)
    assert message.startswith(action + ' failed.')
    assert 'temporarily unavailable' in message and 'secret' not in message

"""Isolated offline end-to-end demo using real local HTTP and separate processes.

Run from the project root: .venv/Scripts/python.exe scripts/local_demo.py
Temporary SQLite history is removed after shutdown; demo_report.md is retained.
No developer dotenv file or provider credentials are passed to the children.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx

from deepresearch_ai.models import ResearchReport

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def wait_ready(client, url, process, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('A demo server exited before becoming ready.')
        try:
            if client.get(url).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(.2)
    raise RuntimeError('A demo server did not become ready before its deadline.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'demo_report.md')
    parser.add_argument('--serve', action='store_true', help='Keep the isolated mock dashboard and worker running until Ctrl+C.')
    parser.add_argument('--port', type=int, default=0, help='Local API/dashboard port; 0 chooses a free port.')
    args = parser.parse_args()
    build = ROOT / 'frontend' / 'dist'
    if not (build / 'index.html').is_file():
        parser.error('Build the React dashboard first: cd frontend; npm.cmd ci; npm.cmd run build')
    environment = {key: value for key, value in os.environ.items()
        if not key.upper().startswith(('LLM_', 'RESEARCH_', 'PROVIDER_', 'LANGCHAIN_', 'LANGSMITH_', 'AUTH_', 'QUOTA_', 'ADMIN_', 'ACCOUNT_EXPORT_', 'PROGRESS_'))
        and key.upper() not in {'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GOOGLE_API_KEY', 'GEMINI_API_KEY',
                               'GEMINI_THINKING_LEVEL', 'TAVILY_API_KEY', 'MOCK_MODE', 'DATABASE_URL', 'BACKEND_URL', 'FRONTEND_DIST_DIR', 'APP_ENV'}}
    environment.update(MOCK_MODE='true', PYTHONUTF8='1', FRONTEND_DIST_DIR=str(build))
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    processes = []
    with tempfile.TemporaryDirectory(prefix='deepresearch-demo-') as temp:
        directory = Path(temp)
        api_port = args.port or free_port()
        api_url = f'http://127.0.0.1:{api_port}'
        environment['AUTH_ALLOWED_ORIGINS'] = json.dumps([api_url])
        environment.update(DATABASE_URL='sqlite:///' + (directory / 'demo.db').as_posix())
        with (directory / 'servers.log').open('w', encoding='utf-8') as log:
            try:
                api = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'deepresearch_ai.api.app:create_app',
                    '--factory', '--host', '127.0.0.1', '--port', str(api_port)], cwd=directory,
                    env=environment, stdout=log, stderr=log, creationflags=flags)
                processes.append(api)
                with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                    wait_ready(client, api_url + '/health', api)
                    import secrets
                    # A random demo-only account lives in the temporary database.
                    csrf = client.get(api_url + '/auth/csrf').json()['csrf_token']
                    demo_password = secrets.token_urlsafe(24)
                    account = client.post(api_url + '/auth/register', headers={'X-CSRF-Token':csrf}, json={
                        'name':'Offline Demo', 'email':'demo@example.com', 'password':demo_password})
                    account.raise_for_status()
                    client.headers['X-CSRF-Token'] = account.json()['csrf_token']
                    payload = {
                        'topic': 'How does RAG ground an answer?', 'instructions': 'Explain limitations.', 'depth': 'quick'}
                    response = client.post(api_url + '/research', json=payload,
                                           headers={'Idempotency-Key':'demo-initial-run'})
                    response.raise_for_status()
                    run = response.json()
                    assert run['status'] == 'queued'
                    print('Created durable queued research run: ' + run['id'])
                    worker = subprocess.Popen([sys.executable, '-m', 'deepresearch_ai.worker', *([] if args.serve else ['--once'])],
                        cwd=directory, env=environment, stdout=log, stderr=log, creationflags=flags)
                    processes.append(worker)
                    if args.serve:
                        deadline = time.monotonic() + 30
                        while time.monotonic() < deadline:
                            status = client.get(api_url + '/research/' + run['id']).json()['status']
                            if status == 'completed':
                                break
                            time.sleep(.2)
                    else:
                        assert worker.wait(timeout=30) == 0
                    result = client.get(api_url + '/research/' + run['id']).json()
                    assert result['status'] == 'completed'
                    report = ResearchReport.model_validate(client.get(api_url + '/research/' + run['id'] + '/report').json())
                    assert len(report.citations) == 2
                    sources = client.get(api_url + '/research/' + run['id'] + '/sources').json()
                    assert len(sources) == 2
                    history = client.get(api_url + '/research').json()
                    assert history['total'] == 1
                    download = client.get(api_url + '/research/' + run['id'] + '/report.md')
                    download.raise_for_status()
                    args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(download.text, encoding='utf-8')
                    page = client.get(api_url)
                    assert page.status_code == 200 and '<div id="root">' in page.text
                    import re
                    for asset in re.findall(r'(?:src|href)="(/assets/[^\"]+)"', page.text):
                        assert client.get(api_url + asset).status_code == 200
                    replay = client.post(api_url + '/research', json=payload,
                                         headers={'Idempotency-Key':'demo-initial-run'})
                    assert replay.status_code == 202 and replay.json()['id'] == run['id']
                    assert replay.json()['submission_reused'] is True
                    usage = client.get(api_url + '/account/usage').json()
                    assert usage['today']['submissions'] == 1 and usage['remaining']['runs'] == 19
                    assert client.get(api_url + '/research', params={'q':'RAG','status':'completed'}).json()['total'] == 1
                    stream = client.get(api_url + '/research/' + run['id'] + '/events')
                    assert 'event: progress' in stream.text and 'event: end' in stream.text
                    import io, zipfile
                    exported = client.get(api_url + '/account/export')
                    exported.raise_for_status()
                    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                        assert 'account.json' in archive.namelist()
                        assert 'research/' + run['id'] + '/report.md' in archive.namelist()
                    assert client.get(api_url + '/admin/overview').status_code == 403
                    granted = subprocess.run([sys.executable,'-m','deepresearch_ai.db','set-admin','--email','demo@example.com'],
                        cwd=directory, env=environment, capture_output=True, creationflags=flags, timeout=15)
                    assert granted.returncode == 0
                    assert client.get(api_url + '/admin/overview').json()['counts']['completed'] == 1
                    if not args.serve:
                        assert client.delete(api_url + '/research/' + run['id']).status_code == 204
                        assert client.get(api_url + '/account/usage').json()['today']['submissions'] == 1
                        assert client.request('DELETE', api_url + '/auth/me', json={
                            'password':demo_password,'confirmation':'DELETE'}).status_code == 204
                        assert client.get(api_url + '/research').status_code == 401
                    print(json.dumps({'status': result['status'], 'report_status': report.status,
                        'sources': len(sources), 'history_runs': history['total'], 'frontend_health': 'ok',
                        'account_features':'ok', 'live_events':'ok', 'private_export':'ok', 'administrator_access':'ok'}))
                    print('Sanitized Markdown saved to: ' + str(args.output.resolve()))
                    if args.serve:
                        print('Offline React dashboard: ' + api_url, flush=True)
                        print('Uses a temporary mock database; press Ctrl+C to stop.', flush=True)
                        print('Create your own temporary account in the dashboard to try it.', flush=True)
                        try:
                            while api.poll() is None and worker.poll() is None:
                                time.sleep(1)
                        except KeyboardInterrupt:
                            pass
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        if os.name == 'nt':
                            # Windows venv launchers have Python children which
                            # otherwise outlive the launcher and keep SQLite open.
                            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           check=False, creationflags=flags)
                        else:
                            process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
    print('Demo processes stopped; temporary database removed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

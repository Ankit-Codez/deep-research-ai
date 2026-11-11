"""Manage this project's isolated PostgreSQL cluster, never the system service.

python scripts/postgres_local.py init|start|stop|status
Credentials are generated once and kept in ignored local files, not arguments.
"""
import argparse
import getpass
import os
from pathlib import Path
import secrets
import subprocess
import sys

from dotenv import dotenv_values, set_key
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
DATA = (ROOT / 'data' / 'postgres').resolve()
ADMIN_SECRET = ROOT / 'data' / 'postgres-admin.secret'
PORT = 55432


def private_file(path, value):
    path.write_text(value, encoding='utf-8')
    if os.name == 'nt':
        account = os.environ.get('USERDOMAIN', '') + '\\' + getpass.getuser()
        result = subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', account + ':(F)'], capture_output=True)
        if result.returncode:
            raise RuntimeError('Could not restrict access to the local credential file.')
    else:
        path.chmod(0o600)


def run(binary, arguments):
    # Background PostgreSQL may inherit handles; pipes would wait indefinitely for EOF.
    with (ROOT / 'data' / 'postgres-control.log').open('a', encoding='utf-8') as log:
        result = subprocess.run([str(binary), *arguments], stdout=log, stderr=log,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise RuntimeError(f'{binary.name} failed. Inspect the local PostgreSQL log; no credentials were printed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'start', 'stop', 'status'])
    parser.add_argument('--pg-bin', type=Path, default=Path('C:/Program Files/PostgreSQL/15/bin'))
    args = parser.parse_args()
    if not DATA.is_relative_to((ROOT / 'data').resolve()):
        raise RuntimeError('Cluster path escaped the project data directory.')
    ctl = args.pg_bin / ('pg_ctl.exe' if os.name == 'nt' else 'pg_ctl')
    initdb = args.pg_bin / ('initdb.exe' if os.name == 'nt' else 'initdb')
    if not ctl.is_file():
        parser.error('PostgreSQL binaries not found. Set --pg-bin to the installed bin directory.')
    if args.action == 'init':
        ROOT.joinpath('data').mkdir(exist_ok=True)
        if (DATA / 'PG_VERSION').exists():
            if not ADMIN_SECRET.is_file():
                raise RuntimeError('Existing cluster credentials are missing; refusing to overwrite the cluster.')
            password = ADMIN_SECRET.read_text(encoding='utf-8')
        else:
            password = secrets.token_urlsafe(32)
            private_file(ADMIN_SECRET, password)
            run(initdb, ['-D', str(DATA), '-U', 'postgres', '--pwfile=' + str(ADMIN_SECRET),
                         '--auth-host=scram-sha-256', '--auth-local=scram-sha-256', '--encoding=UTF8', '--locale=C'])
        # Only this loopback port is used. The installed PostgreSQL service is untouched.
        with (DATA / 'postgresql.conf').open('a', encoding='utf-8') as config:
            config.write(f"\nlisten_addresses = '127.0.0.1'\nport = {PORT}\npassword_encryption = 'scram-sha-256'\n")
        run(ctl, ['-D', str(DATA), '-l', str(ROOT / 'data' / 'postgres.log'), '-w', '-t', '30', 'start'])
        try:
            import psycopg
            from psycopg import sql
            app_secret = ROOT / 'data' / 'postgres-app.secret'
            if not app_secret.is_file(): private_file(app_secret, secrets.token_urlsafe(32))
            app_password = app_secret.read_text(encoding='utf-8')
            with psycopg.connect(user='postgres', password=password, host='127.0.0.1', port=PORT,
                                 dbname='postgres', autocommit=True, connect_timeout=5) as connection:
                if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", ('deepresearch',)).fetchone():
                    connection.execute(sql.SQL('CREATE ROLE deepresearch LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE').format(sql.Literal(app_password)))
                if not connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", ('deepresearch',)).fetchone():
                    connection.execute('CREATE DATABASE deepresearch OWNER deepresearch')
            url = URL.create('postgresql+psycopg', username='deepresearch', password=app_password,
                             host='127.0.0.1', port=PORT, database='deepresearch')
            env = ROOT / '.env'
            if not env.exists(): env.touch()
            old = dotenv_values(env).get('DATABASE_URL')
            if old and old.startswith('sqlite:'):
                set_key(env, 'LEGACY_SQLITE_URL', old)
            set_key(env, 'DATABASE_URL', url.render_as_string(hide_password=False))
            print('Created project PostgreSQL database on 127.0.0.1:55432; saved DATABASE_URL privately in .env.')
        finally:
            run(ctl, ['-D', str(DATA), '-m', 'fast', '-w', 'stop'])
            print('Project PostgreSQL stopped after setup. No application servers were started.')
    elif args.action == 'start':
        run(ctl, ['-D', str(DATA), '-l', str(ROOT / 'data' / 'postgres.log'), '-w', '-t', '30', 'start'])
        print('Project PostgreSQL started on 127.0.0.1:55432.')
    elif args.action == 'stop':
        run(ctl, ['-D', str(DATA), '-m', 'fast', '-w', 'stop'])
        print('Project PostgreSQL stopped.')
    else:
        result = subprocess.run([str(ctl), '-D', str(DATA), 'status'], capture_output=True)
        print('Project PostgreSQL is running.' if result.returncode == 0 else 'Project PostgreSQL is stopped.')


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(f'PostgreSQL setup failed ({type(exc).__name__}). No credentials were printed.', file=sys.stderr)
        raise SystemExit(1)

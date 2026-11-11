"""Explicit schema migrations: python -m deepresearch_ai.db upgrade."""
import argparse
from pathlib import Path
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]


def migration_config():
    config = Config(str(ROOT / 'alembic.ini'))
    config.set_main_option('script_location', str(ROOT / 'migrations'))
    return config


def current_revision():
    return ScriptDirectory.from_config(migration_config()).get_current_head()


def upgrade(connection=None):
    config = migration_config()
    if connection is not None:
        config.attributes['connection'] = connection
    command.upgrade(config, 'head')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('upgrade')
    importer = actions.add_parser('import-sqlite', help='Copy legacy history without modifying its SQLite file.')
    importer.add_argument('path', type=Path)
    assigner = actions.add_parser('assign-legacy', help='Assign unowned legacy history to an existing local account.')
    assigner.add_argument('--email', required=True)
    admin = actions.add_parser('set-admin',help='Explicit local operator action; registration never grants administrator access.')
    admin.add_argument('--email',required=True)
    admin.add_argument('--revoke',action='store_true')
    args = parser.parse_args()
    if args.action == 'upgrade':
        upgrade()
        print('Database migrations applied.')
        return
    from deepresearch_ai.config import Settings
    from deepresearch_ai.storage.sql import SQLRunRepository
    from deepresearch_ai.storage.legacy import import_sqlite, assign_legacy
    repository = SQLRunRepository(Settings().database_url.get_secret_value())
    try:
        repository.initialize()
        if args.action == 'import-sqlite':
            count = import_sqlite(repository, args.path)
            print(f'Imported {count} runs. Legacy history remains unassigned and private.')
        elif args.action == 'assign-legacy':
            count = assign_legacy(repository, args.email)
            print(f'Assigned {count} legacy runs to the specified account.')
        else:
            from sqlalchemy import update
            from deepresearch_ai.models.auth import LoginRequest
            from deepresearch_ai.storage.tables import UserRow
            email = str(LoginRequest(email=args.email,password='unused').email)
            with repository.sessions.begin() as session:
                result = session.execute(update(UserRow).where(UserRow.email == email,UserRow.is_active.is_(True))
                                         .values(is_admin=not args.revoke))
                if result.rowcount != 1:
                    raise ValueError('Register the account first.')
            print('Administrator role updated for the specified account.')
    except Exception:
        # SQLAlchemy errors may contain connection parameters or research text.
        parser.exit(1, 'Database operation failed. Check migrations, source file, and account details.\n')
    finally:
        repository.close()


if __name__ == '__main__':
    main()

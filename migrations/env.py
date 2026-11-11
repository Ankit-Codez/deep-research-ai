"""Connection details come from Settings, never a committed alembic.ini URL."""
from alembic import context
from sqlalchemy import create_engine
from deepresearch_ai.config import Settings
from deepresearch_ai.storage.tables import Base

config = context.config
target_metadata = Base.metadata
connection = config.attributes.get('connection')
if context.is_offline_mode():
    context.configure(url=Settings().database_url.get_secret_value(), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={'paramstyle': 'named'})
    with context.begin_transaction(): context.run_migrations()
elif connection is not None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction(): context.run_migrations()
else:
    engine = create_engine(Settings().database_url.get_secret_value())
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction(): context.run_migrations()
    finally: engine.dispose()

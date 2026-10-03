from alembic import context
from sqlalchemy import create_engine

from app import models  # noqa: F401  (registers tables)
from app.db import Base

config = context.config


def run_migrations_online() -> None:
    url = config.attributes.get("url") or config.get_main_option("sqlalchemy.url")
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()

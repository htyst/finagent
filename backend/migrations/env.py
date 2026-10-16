from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import database_path
from app.data.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    url = f"sqlite:///{database_path().as_posix()}"
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        # End the PRAGMA implicit transaction so Alembic owns and commits its revision transaction.
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()

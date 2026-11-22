"""Alembic environment (async template). The URL comes from `Settings` (`DATABASE_URL` or `.env`)."""

import asyncio
from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from sqlalchemy import make_url, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.repositories.sql.models import Base, UtcDateTime

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
url = get_settings().database_url
is_sqlite = make_url(url).get_backend_name() == "sqlite"


def render_item(kind: str, obj: Any, autogen_context: Any) -> str | Literal[False]:
    # A migration is history: it must not import app code that will change after it was written.
    if kind == "type" and isinstance(obj, UtcDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def configure(**options: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_item=render_item,
        render_as_batch=is_sqlite,  # SQLite can't ALTER most things: Alembic copies the table instead
        transactional_ddl=True,
        **options,
    )


def run_migrations_offline() -> None:
    configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    # Not the app's engine: here DDL should be transactional (modern sqlite3 transactions), and batch mode
    # needs foreign key enforcement off, which is SQLite's default.
    connect_args = {"autocommit": False} if is_sqlite else {}
    engine = create_async_engine(url, poolclass=pool.NullPool, connect_args=connect_args)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

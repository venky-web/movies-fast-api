"""The async engine, and how SQLite is made to behave like READ COMMITTED.

With "modern" sqlite3 transactions (`autocommit=False`) every SELECT is inside a snapshot: the re-read that
a service does after waiting for a lock would be stale, and the write after it fails with "database is
locked". Here reads outside a write each see the latest commit, and the first write starts the transaction
with BEGIN IMMEDIATE. That is also what PostgreSQL does by default.
"""

import sqlite3
from typing import Any

from sqlalchemy import event, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

BUSY_TIMEOUT_SECONDS = 10.0  # SQLite has one writer at a time; the others wait up to this long


def create_engine(url: str) -> AsyncEngine:
    if make_url(url).get_backend_name() != "sqlite":
        return create_async_engine(url)

    engine = create_async_engine(
        url,
        connect_args={
            # Spelled out, because Python 3.16 changes the default.
            "autocommit": sqlite3.LEGACY_TRANSACTION_CONTROL,
            "isolation_level": "IMMEDIATE",
            "timeout": BUSY_TIMEOUT_SECONDS,
        },
    )

    @event.listens_for(engine.sync_engine, "connect")
    def on_connect(connection: Any, record: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")  # off by default, per connection (pitfall 9)
        cursor.execute("PRAGMA journal_mode=WAL")  # readers and the writer don't block each other
        cursor.close()

    return engine

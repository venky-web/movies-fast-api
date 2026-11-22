"""Alembic and the seed CLI, run the way a person runs them: as commands, on a throwaway database."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app.repositories.sql.models import Base

pytestmark = [pytest.mark.sql, pytest.mark.slow]

ROOT = Path(__file__).parents[1]


@pytest.fixture
def database(tmp_path) -> Path:
    folder = tmp_path / "My Data"  # a path with a space (pitfall 10)
    folder.mkdir()
    return folder / "movies.db"


@pytest.fixture
def run(database, tmp_path):
    env = os.environ | {
        "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "UPLOADS_DIR": str(tmp_path / "uploads"),
        "SAMPLE_BOOKINGS_FOR": "u-ava",
    }

    def run(*args: str, expect: int = 0) -> str:
        result = subprocess.run(
            [sys.executable, "-m", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
        )
        assert result.returncode == expect, result.stdout + result.stderr
        return result.stdout + result.stderr

    return run


def count(database: Path, table: str) -> int:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def described(url: str) -> dict:
    """Everything the app relies on, including `ondelete`, which `alembic check` doesn't compare."""
    engine = create_engine(url)
    try:
        found = inspect(engine)
        return {
            table: {
                "columns": [(c["name"], str(c["type"]), c["nullable"]) for c in found.get_columns(table)],
                "pk": found.get_pk_constraint(table)["constrained_columns"],
                "indexes": sorted((i["name"], tuple(i["column_names"])) for i in found.get_indexes(table)),
                "unique": sorted(tuple(u["column_names"]) for u in found.get_unique_constraints(table)),
                "fks": sorted(
                    (tuple(f["constrained_columns"]), f["referred_table"], f["options"].get("ondelete", ""))
                    for f in found.get_foreign_keys(table)
                ),
            }
            for table in found.get_table_names()
            if table != "alembic_version"
        }
    finally:
        engine.dispose()


def test_upgrade_creates_a_database_that_matches_the_models(run, database):
    run("alembic", "upgrade", "head")

    assert "No new upgrade operations detected" in run("alembic", "check")


def test_migrated_schema_equals_create_all(run, database, tmp_path):
    """The other tests use `create_all`, so it has to be the same schema."""
    run("alembic", "upgrade", "head")
    other = f"sqlite:///{tmp_path / 'create_all.db'}"
    engine = create_engine(other)
    Base.metadata.create_all(engine)
    engine.dispose()

    assert described(f"sqlite:///{database}") == described(other)


def test_downgrade_empties_the_database(run, database):
    run("alembic", "upgrade", "head")

    run("alembic", "downgrade", "base")

    assert described(f"sqlite:///{database}") == {}


def test_seed_needs_the_schema(run):
    assert "Run: python -m alembic upgrade head" in run("app.seed", expect=1)


def test_seed_then_reset(run, database):
    run("alembic", "upgrade", "head")

    assert "Seeded" in run("app.seed")
    assert (count(database, "movies"), count(database, "bookings")) == (12, 6)

    assert "already has data" in run("app.seed")
    assert count(database, "bookings") == 6

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE movies SET title = 'Edited'")
    assert "Reset" in run("app.seed", "--reset")

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM movies WHERE title = 'Edited'").fetchone()[0] == 0
    assert (count(database, "movies"), count(database, "bookings")) == (12, 6)


def test_memory_mode_does_not_import_sqlalchemy(tmp_path):
    """So the app still runs in memory where SQLAlchemy is missing or blocked."""
    code = (
        "import sys\n"
        "sys.modules['sqlalchemy'] = None\n"  # any import of it now fails
        "from fastapi.testclient import TestClient\n"
        "from app.core.config import Settings\n"
        "from app.main import create_app\n"
        f"settings = Settings(_env_file=None, storage_backend='memory', uploads_dir={str(tmp_path)!r})\n"
        "app = create_app(settings)\n"
        "with TestClient(app) as client:\n"
        "    assert len(client.get('/api/movies').json()) == 12\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr

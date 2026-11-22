"""Seeds the database. The lifespan uses it, and so does the command line:

python -m app.seed            seeds DATABASE_URL if it is empty
python -m app.seed --reset    deletes ALL data in it, then seeds. Stop the server first: it would not notice.
"""

import argparse
import asyncio
import random
from collections.abc import Sequence
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.domain.seed import build_seed
from app.repositories.sql.repos import SqlRepos
from app.repositories.sql.storage import SchemaMissingError, SqlStorage
from app.services import admin
from app.services.locks import ShowtimeLocks
from app.services.posters import clear_uploads


async def seed_database(
    session: AsyncSession, clock: Clock, settings: Settings, rng: random.Random | None = None
) -> None:
    """Replaces everything with the seed (plus the configured sample bookings). The caller commits."""
    now, tz = clock.now(), ZoneInfo(settings.theater_tz)
    repos = SqlRepos(session)
    await repos.install(build_seed(now, tz))
    if settings.sample_bookings_for:
        await admin.add_startup_samples(
            repos, settings.sample_bookings_for, now=now, tz=tz, rng=rng or random.Random()
        )


async def seed_if_empty(
    storage: SqlStorage, clock: Clock, settings: Settings, rng: random.Random | None = None
) -> bool:
    async with storage.session() as session:
        if not await SqlRepos(session).is_empty():
            return False
        await seed_database(session, clock, settings, rng)
        await session.commit()
    return True


async def run(settings: Settings, clock: Clock, *, reset: bool) -> str:
    storage = await SqlStorage.connect(settings.database_url, ShowtimeLocks())
    try:
        if not reset:
            seeded = await seed_if_empty(storage, clock, settings)
            return (
                "Seeded the database."
                if seeded
                else "The database already has data. Use --reset to replace it."
            )
        async with storage.session() as session:
            await seed_database(session, clock, settings)
            await session.commit()
        await clear_uploads(settings.uploads_dir)  # only once the new data is committed
        return "Reset and seeded the database."
    finally:
        await storage.close()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.seed", description="Seed the movies database.")
    parser.add_argument("--reset", action="store_true", help="delete all data first (stop the server first)")
    args = parser.parse_args(argv)
    try:
        print(asyncio.run(run(get_settings(), SystemClock(), reset=args.reset)))
    except SchemaMissingError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()

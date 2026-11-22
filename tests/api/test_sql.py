"""What only the SQL backend must get right: transactions, the constraint, locks past the commit."""

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from app import seed
from app.main import create_app
from app.repositories.sql import engine
from app.repositories.sql.models import MovieRow, ShowtimeRow
from app.repositories.sql.repos import SqlBookingRepo, SqlOutboxRepo, SqlRepos
from app.repositories.sql.storage import SchemaMissingError
from app.schemas.events import BookingCreated
from app.schemas.movie import MOVIE_EXAMPLE
from app.services.locks import CATALOG_LOCK
from tests.conftest import ADMIN_KEY, TEST_NOW
from tests.helpers import (
    booking_request,
    first_future,
    free_seats,
    pause_before_write,
    showtimes,
    stored_showtime,
)
from tests.stored import stored

pytestmark = pytest.mark.backends("sql")

ADMIN = {"X-Admin-Key": ADMIN_KEY}
PNG = b"\x89PNG\r\n\x1a\n" + b"fake image data"


async def until(condition, seconds=8):
    async with asyncio.timeout(seconds):
        while not condition():  # noqa: ASYNC110 (no event to wait on)
            await asyncio.sleep(0.01)


async def queued(lock, count):
    await until(lambda: len(lock._waiters or ()) >= count)


def slow_commits(monkeypatch, seconds=0.2):
    commit = AsyncSession.commit

    async def slow(self):
        await asyncio.sleep(seconds)
        await commit(self)

    monkeypatch.setattr(AsyncSession, "commit", slow)


def failing_commits(monkeypatch):
    async def fail(self):
        raise RuntimeError("disk full")

    monkeypatch.setattr(AsyncSession, "commit", fail)


class RecordingPublisher:
    def __init__(self, on_created=lambda booking: None):
        self.calls = []
        self._on_created = on_created

    async def booking_created(self, booking):
        self.calls.append(("created", booking.id))
        self._on_created(booking)

    async def booking_cancelled(self, booking):
        self.calls.append(("cancelled", booking.id))

    async def showtime_changed(self, showtime):
        self.calls.append(("changed", showtime.id))

    async def showtime_deleted(self, showtime_id):
        self.calls.append(("deleted", showtime_id))

    async def demo_reset(self):
        self.calls.append(("reset", None))


@pytest.mark.anyio
class TestConstraint:
    async def test_prevents_double_booking_without_any_lock(self, app, aclient, monkeypatch):
        app.state.locks.enabled = False
        publisher = app.state.publisher = RecordingPublisher()
        both_checked = asyncio.Barrier(2)

        async def pause():  # both have seen the seats free before either one writes
            await asyncio.wait_for(both_checked.wait(), 5)

        pause_before_write(monkeypatch, app, pause)
        showtime = first_future(showtimes(app), TEST_NOW)
        seats = free_seats(showtime)[:2]
        body = booking_request(showtime, seats)

        responses = await asyncio.gather(*(aclient.post("/api/bookings", json=body) for _ in range(2)))

        won, lost = sorted(responses, key=lambda r: r.status_code)
        assert (won.status_code, lost.status_code) == (201, 409)
        assert (lost.json()["code"], lost.json()["details"]) == ("SEATS_TAKEN", {"takenSeats": seats})
        assert list(stored(app).bookings()) == [won.json()["id"]]  # the loser's booking row was rolled back
        assert stored_showtime(app, showtime.id).booked_seats == [*showtime.booked_seats, *seats]
        assert publisher.calls == [("created", won.json()["id"])]
        assert [email.booking_id for email in stored(app).outbox()] == [won.json()["id"]]

    async def test_reports_only_the_seats_that_were_really_taken(self, app, aclient, monkeypatch):
        app.state.locks.enabled = False
        showtime = first_future(showtimes(app), TEST_NOW)
        shared, mine, theirs = free_seats(showtime)[:3]
        both_checked = asyncio.Barrier(2)

        async def pause():
            await asyncio.wait_for(both_checked.wait(), 5)

        pause_before_write(monkeypatch, app, pause)

        responses = await asyncio.gather(
            aclient.post("/api/bookings", json=booking_request(showtime, [shared, mine])),
            aclient.post("/api/bookings", json=booking_request(showtime, [shared, theirs])),
        )

        [lost] = [r for r in responses if r.status_code == 409]
        assert lost.json()["details"]["takenSeats"] == [shared]

    async def test_covers_the_random_pre_booked_seats(self, app, aclient):
        showtime = first_future(showtimes(app), TEST_NOW)

        async with app.state.storage.session() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    text("INSERT INTO booked_seats (showtime_id, seat_id) VALUES (:showtime, :seat)"),
                    {"showtime": showtime.id, "seat": showtime.booked_seats[0]},
                )


@pytest.mark.anyio
class TestLocksAndCommit:
    async def test_cancelling_twice_at_once_is_one_200_and_one_409(self, app, aclient, monkeypatch):
        # The constraint can't help here: only holding the lock until after the commit does.
        showtime = first_future(showtimes(app), TEST_NOW)
        body = booking_request(showtime, free_seats(showtime)[:1])
        booking = (await aclient.post("/api/bookings", json=body)).json()
        publisher = app.state.publisher = RecordingPublisher()
        slow_commits(monkeypatch)

        url = f"/api/bookings/{booking['id']}/cancel"
        responses = await asyncio.gather(*(aclient.patch(url, json={"userId": "u-ava"}) for _ in range(2)))

        assert sorted(r.status_code for r in responses) == [200, 409]
        assert publisher.calls == [("cancelled", booking["id"])]

    async def test_a_layout_edit_waits_for_the_bookings_commit(self, app, aclient, monkeypatch):
        showtime = first_future(showtimes(app), TEST_NOW).model_copy(update={"booked_seats": []})
        stored(app).put_showtime(showtime)
        lock = app.state.locks.get(showtime.id)
        slow_commits(monkeypatch)

        async with lock:
            book = aclient.post("/api/bookings", json=booking_request(showtime, ["B1"]))
            booking = asyncio.create_task(book)
            await queued(lock, 1)
            url = f"/api/admin/showtimes/{showtime.id}"
            edit = asyncio.create_task(aclient.patch(url, json={"rows": 1}, headers=ADMIN))
            await queued(lock, 2)

        assert (await booking).status_code == 201
        assert (await edit).json()["code"] == "LAYOUT_CONFLICT"

    async def test_a_second_booking_is_refused_by_the_check(self, app, aclient, monkeypatch):
        claims = []
        add = SqlBookingRepo.add

        async def counting_add(self, booking):
            claims.append(booking.id)
            await add(self, booking)

        monkeypatch.setattr(SqlBookingRepo, "add", counting_add)
        slow_commits(monkeypatch)
        showtime = first_future(showtimes(app), TEST_NOW)
        body = booking_request(showtime, free_seats(showtime)[:1])

        responses = await asyncio.gather(*(aclient.post("/api/bookings", json=body) for _ in range(2)))

        assert sorted(r.status_code for r in responses) == [201, 409]
        assert len(claims) == 1  # the second never got as far as the constraint: it saw committed seats

    async def test_waiting_for_a_lock_holds_no_connection(self, app, aclient):
        showtime = first_future(showtimes(app), TEST_NOW)
        catalog = app.state.locks.get(CATALOG_LOCK)
        waiters = 20  # more than the pool's 15 connections

        async with catalog:
            # Takes the lock before its first query, so it needs a connection while it holds the lock.
            create = asyncio.create_task(aclient.post("/api/admin/movies", json=MOVIE_EXAMPLE, headers=ADMIN))
            await queued(catalog, 1)
            url = "/api/admin/movies/m-deep-field/poster"
            uploads = [
                asyncio.create_task(
                    aclient.post(url, files={"file": ("p.png", PNG, "image/png")}, headers=ADMIN)
                )
                for _ in range(waiters)
            ]  # each one queries before it takes the lock
            await queued(catalog, waiters + 1)
            assert app.state.storage.engine.sync_engine.pool.checkedout() == 0

        responses = await asyncio.wait_for(asyncio.gather(create, *uploads), 20)
        assert [r.status_code for r in responses] == [201] + [200] * waiters
        assert stored_showtime(app, showtime.id) == showtime

    async def test_a_lock_after_the_first_write_is_refused(self, app, aclient):
        with pytest.raises(RuntimeError, match="before the first write"):
            async with app.state.storage.unit_of_work(app.state.publisher) as uow:
                await uow.repos.tombstones.add("st-x")
                async with uow.locks.get("st-x"):
                    pass

    async def test_locks_are_released_after_an_error(self, app, aclient):
        showtime = first_future(showtimes(app), TEST_NOW)

        taken = booking_request(showtime, showtime.booked_seats[:1])
        assert (await aclient.post("/api/bookings", json=taken)).status_code == 409

        assert not app.state.locks.get(showtime.id).locked()

    async def test_many_writers_at_once_all_succeed_and_publish_in_commit_order(self, app, aclient):
        publisher = app.state.publisher = RecordingPublisher()
        chosen = [s for s in showtimes(app) if s.starts_at > TEST_NOW and free_seats(s)][:30]

        responses = await asyncio.gather(
            *(aclient.post("/api/bookings", json=booking_request(s, free_seats(s)[:1])) for s in chosen)
        )

        assert [r.status_code for r in responses] == [201] * 30
        async with app.state.storage.session() as session:
            commit_order = (
                (await session.execute(text("SELECT id FROM bookings ORDER BY seq"))).scalars().all()
            )
        assert [booking_id for _, booking_id in publisher.calls] == list(commit_order)


@pytest.mark.anyio
class TestUnitOfWork:
    async def test_the_response_is_sent_after_the_commit(self, app):
        async with app.router.lifespan_context(app):
            showtime = first_future(showtimes(app), TEST_NOW)
            body = json.dumps(booking_request(showtime, free_seats(showtime)[:1])).encode()
            committed_when_sent = []

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                if message["type"] == "http.response.start":
                    committed_when_sent.append((message["status"], len(stored(app).bookings())))

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/bookings",
                "raw_path": b"/api/bookings",
                "root_path": "",
                "query_string": b"",
                "headers": [(b"host", b"test"), (b"content-type", b"application/json")],
                "client": ("127.0.0.1", 1),
                "server": ("test", 80),
            }
            await app(scope, receive, send)

        assert committed_when_sent == [(201, 1)]

    async def test_events_are_published_after_the_commit_and_before_the_lock_is_released(self, app, aclient):
        showtime = first_future(showtimes(app), TEST_NOW)
        seen = []

        def on_created(booking):
            committed = booking.id in stored(app).bookings()
            seen.append((committed, app.state.locks.get(showtime.id).locked()))

        app.state.publisher = RecordingPublisher(on_created)

        await aclient.post("/api/bookings", json=booking_request(showtime, free_seats(showtime)[:1]))

        assert seen == [(True, True)]

    async def test_a_failed_commit_is_a_500_and_leaves_no_trace(self, app, aclient, monkeypatch):
        showtime = first_future(showtimes(app), TEST_NOW)
        body = booking_request(showtime, free_seats(showtime)[:1])
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

        async with app.state.bus.subscribe(BookingCreated) as events:
            with monkeypatch.context() as patched:
                failing_commits(patched)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post("/api/bookings", json=body)

            assert (response.status_code, response.json()["code"]) == (500, "INTERNAL")
            assert events._queue.empty()  # nothing was published
        assert stored(app).bookings() == {}
        assert stored(app).outbox() == []
        assert stored_showtime(app, showtime.id) == showtime
        assert (await aclient.post("/api/bookings", json=body)).status_code == 201  # and nothing stays locked

    async def test_an_exception_after_a_write_rolls_back(self, app, aclient, monkeypatch):
        showtime = first_future(showtimes(app), TEST_NOW)
        add = SqlBookingRepo.add

        async def write_then_fail(self, booking):
            await add(self, booking)
            raise RuntimeError("after the write")

        monkeypatch.setattr(SqlBookingRepo, "add", write_then_fail)

        with pytest.raises(RuntimeError, match="after the write"):
            await aclient.post("/api/bookings", json=booking_request(showtime, free_seats(showtime)[:1]))

        assert stored(app).bookings() == {}
        assert stored_showtime(app, showtime.id) == showtime

    async def test_the_email_uses_its_own_session(self, app, aclient, monkeypatch):
        sessions = {}
        for name, repo in (("booking", SqlBookingRepo), ("email", SqlOutboxRepo)):

            def record(self, *args, name=name, original=repo.add):
                sessions[name] = self._session
                if name == "email":
                    # Only the task's connection is out: the request's unit of work has already ended.
                    sessions["connections"] = app.state.storage.engine.sync_engine.pool.checkedout()
                return original(self, *args)

            monkeypatch.setattr(repo, "add", record)
        showtime = first_future(showtimes(app), TEST_NOW)

        response = await aclient.post(
            "/api/bookings", json=booking_request(showtime, free_seats(showtime)[:1])
        )

        assert [email.booking_id for email in stored(app).outbox()] == [response.json()["id"]]
        assert sessions["email"] is not sessions["booking"]
        assert sessions["connections"] == 1

    async def test_an_open_event_stream_holds_no_connection(self, app, aclient):
        before = app.state.bus.subscriber_count
        stream = asyncio.create_task(aclient.get("/api/events"))
        await until(lambda: app.state.bus.subscriber_count == before + 1)

        assert app.state.storage.engine.sync_engine.pool.checkedout() == 0

        app.state.shutdown_event.set()
        await asyncio.wait_for(stream, 2)

    async def test_an_open_socket_holds_no_connection(self, app):
        with TestClient(app) as client:
            showtime = first_future(showtimes(app), TEST_NOW)
            with client.websocket_connect(f"/ws/showtimes/{showtime.id}") as ws:
                ws.receive_json()
                client.portal.call(asyncio.sleep, 0.05)

                assert app.state.storage.engine.sync_engine.pool.checkedout() == 0


@pytest.mark.anyio
class TestReads:
    async def test_a_re_read_is_fresh_even_if_the_session_loaded_the_row_before(self, app, aclient):
        showtime = first_future(showtimes(app), TEST_NOW)
        storage = app.state.storage
        async with storage.session() as waiting:
            query = select(ShowtimeRow).where(ShowtimeRow.id == showtime.id)
            held = (await waiting.execute(query)).scalar_one()  # kept alive, so it stays in the identity map
            async with storage.session() as other:
                await other.execute(
                    update(ShowtimeRow).where(ShowtimeRow.id == showtime.id).values(screen="IMAX")
                )
                await other.commit()

            assert (await SqlRepos(waiting).showtimes.get(showtime.id)).screen == "IMAX"
            assert held.screen == "IMAX"

    async def test_saving_a_stale_showtime_cannot_touch_the_seats(self, app, aclient):
        stale = first_future(showtimes(app), TEST_NOW)
        seat = free_seats(stale)[0]
        await aclient.post("/api/bookings", json=booking_request(stale, [seat]))

        async with app.state.storage.session() as session:
            await SqlRepos(session).showtimes.save(stale)
            await session.commit()

        assert seat in stored_showtime(app, stale.id).booked_seats


@pytest.mark.anyio
class TestSchema:
    async def test_foreign_keys_and_wal_are_on(self, app, aclient):
        async with app.state.storage.session() as session:
            assert (await session.execute(text("PRAGMA foreign_keys"))).scalar() == 1
            assert (await session.execute(text("PRAGMA journal_mode"))).scalar() == "wal"

    async def test_deleting_a_showtime_that_bookings_reference_fails(self, app, aclient):
        showtime = first_future(showtimes(app), TEST_NOW)
        await aclient.post("/api/bookings", json=booking_request(showtime, free_seats(showtime)[:1]))

        async with app.state.storage.session() as session:
            with pytest.raises(IntegrityError):
                await session.execute(delete(ShowtimeRow).where(ShowtimeRow.id == showtime.id))

    async def test_datetimes_come_back_as_aware_utc(self, app, aclient):
        showtime = first_future(showtimes(app), TEST_NOW)
        async with app.state.storage.session() as session:
            query = select(ShowtimeRow.starts_at).where(ShowtimeRow.id == showtime.id)
            value = (await session.execute(query)).scalar_one()

        assert value.tzinfo is UTC
        assert value == showtime.starts_at
        body = (await aclient.get(f"/api/showtimes/{showtime.id}")).json()
        assert body["startsAt"].endswith(".000Z")

    async def test_naive_datetimes_cannot_be_stored(self, app, aclient):
        async with app.state.storage.session() as session:
            with pytest.raises(StatementError, match="aware"):
                await session.execute(update(MovieRow).values(deleted_at=datetime(2026, 1, 1)))

    async def test_money_round_trips_exactly(self, app, aclient):
        base = first_future(showtimes(app), TEST_NOW)
        prices = ["0.01", "0.10", "0.29", "13.33", "19.99", "999.99", "1000.00"]
        async with app.state.storage.session() as session:
            await session.execute(
                insert(ShowtimeRow),
                [
                    {
                        "id": f"st-price-{i}",
                        "movie_id": base.movie_id,
                        "starts_at": base.starts_at,
                        "screen": base.screen,
                        "price": Decimal(price),
                        "rows": 1,
                        "seats_per_row": 1,
                        "source": "manual",
                    }
                    for i, price in enumerate(prices)
                ],
            )
            await session.commit()

        assert [stored_showtime(app, f"st-price-{i}").price for i in range(len(prices))] == [
            Decimal(price) for price in prices
        ]


@pytest.mark.anyio
class TestReset:
    async def test_a_booking_that_waited_sees_the_new_data(self, app, aclient):
        base = first_future(showtimes(app), TEST_NOW)
        body = {
            "movieId": base.movie_id,
            "startsAt": (TEST_NOW + timedelta(days=30)).isoformat(),
            "screen": "Screen 1",
            "price": 10,
            "rows": 5,
            "seatsPerRow": 8,
        }
        created = (await aclient.post("/api/admin/showtimes", json=body, headers=ADMIN)).json()
        manual = stored_showtime(app, created["id"])
        lock = app.state.locks.get(manual.id)

        async with lock:
            reset = asyncio.create_task(aclient.post("/api/admin/reset", headers=ADMIN))
            await queued(lock, 1)
            booking = asyncio.create_task(aclient.post("/api/bookings", json=booking_request(manual, ["A1"])))
            await queued(lock, 2)

        assert (await reset).status_code == 204
        assert (await booking).status_code == 404

    async def test_a_cancel_that_waited_does_not_report_success(self, app, aclient):
        showtime = first_future(showtimes(app), TEST_NOW)
        body = booking_request(showtime, free_seats(showtime)[:1])
        booking = (await aclient.post("/api/bookings", json=body)).json()
        lock = app.state.locks.get(showtime.id)

        async with lock:
            reset = asyncio.create_task(aclient.post("/api/admin/reset", headers=ADMIN))
            await queued(lock, 1)
            url = f"/api/bookings/{booking['id']}/cancel"
            cancel = asyncio.create_task(aclient.patch(url, json={"userId": "u-ava"}))
            await queued(lock, 2)

        assert (await reset).status_code == 204
        assert (await cancel).status_code == 404

    @pytest.mark.parametrize("action", ["reset", "re-upload"])
    async def test_a_failed_commit_deletes_no_files(self, app, aclient, settings, monkeypatch, action):
        url = "/api/admin/movies/m-deep-field/poster"
        files = {"file": ("p.png", PNG, "image/png")}
        poster = (await aclient.post(url, files=files, headers=ADMIN)).json()["posterUrl"]
        failing_commits(monkeypatch)

        with pytest.raises(RuntimeError, match="disk full"):
            if action == "reset":
                await aclient.post("/api/admin/reset", headers=ADMIN)
            else:
                await aclient.post(url, files=files, headers=ADMIN)

        assert (settings.uploads_dir / "posters" / poster.rsplit("/", 1)[1]).exists()


class TestRestart:
    def test_data_survives_a_restart(self, settings, frozen_clock):
        with TestClient(create_app(settings, frozen_clock, random.Random(1))) as client:
            showtime = first_future(showtimes(client.app), TEST_NOW)
            body = booking_request(showtime, free_seats(showtime)[:2])
            booking = client.post("/api/bookings", json=body).json()

        with TestClient(create_app(settings, frozen_clock, random.Random(2))) as client:
            assert client.get("/api/bookings", params={"userId": "u-ava"}).json() == [booking]

    def test_startup_refreshes_data_that_is_days_old(self, settings, frozen_clock):
        with TestClient(create_app(settings, frozen_clock)) as client:
            showtime = first_future(showtimes(client.app), TEST_NOW)
            seat = free_seats(showtime)[0]
            client.post("/api/bookings", json=booking_request(showtime, [seat]))

        frozen_clock.advance(days=3)
        with TestClient(create_app(settings, frozen_clock)) as client:
            days = [int(s.id.rsplit("-", 2)[1]) for s in showtimes(client.app) if s.id != showtime.id]

            assert (min(days), max(days)) == (20261120, 20261128)
            assert seat in stored_showtime(client.app, showtime.id).booked_seats

    def test_sample_bookings_are_not_added_again_on_every_restart(self, settings, frozen_clock):
        configured = settings.model_copy(update={"sample_bookings_for": "u-sam"})
        for _ in range(3):
            with TestClient(create_app(configured, frozen_clock)) as client:
                assert len(client.get("/api/bookings", params={"userId": "u-sam"}).json()) == 6

    def test_a_database_without_the_schema_says_what_to_run(self, settings, frozen_clock, tmp_path):
        empty = settings.model_copy(update={"database_url": f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"})

        with pytest.raises(SchemaMissingError, match="alembic upgrade head"):
            with TestClient(create_app(empty, frozen_clock)):
                pass


@pytest.mark.anyio
async def test_writers_queue_in_process_instead_of_starving_on_sqlites_busy_timeout(app, monkeypatch):
    monkeypatch.setattr(engine, "BUSY_TIMEOUT_SECONDS", 0.15)
    async with app.router.lifespan_context(app):
        slow_commits(monkeypatch, 0.4)  # longer than the busy timeout
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        chosen = [s for s in showtimes(app) if s.starts_at > TEST_NOW and free_seats(s)][:3]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(
                *(client.post("/api/bookings", json=booking_request(s, free_seats(s)[:1])) for s in chosen)
            )

    assert [r.status_code for r in responses] == [201, 201, 201]


@pytest.mark.anyio
async def test_the_seed_command_seeds_once_and_resets(settings, frozen_clock):
    assert await seed.run(settings, frozen_clock, reset=False) == "Seeded the database."
    assert "already has data" in await seed.run(settings, frozen_clock, reset=False)
    assert await seed.run(settings, frozen_clock, reset=True) == "Reset and seeded the database."

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.background import refresh_showtimes
from app.main import create_app
from app.schemas.movie import MOVIE_EXAMPLE
from tests.conftest import ADMIN_KEY, TEST_NOW
from tests.helpers import booking_request, first_future, first_past, free_seats, showtimes, stored_showtime
from tests.stored import stored

LONDON = ZoneInfo("Europe/London")
ADMIN_ROUTES = [
    ("POST", "/api/admin/movies"),
    ("PUT", "/api/admin/movies/{movie_id}"),
    ("PATCH", "/api/admin/movies/{movie_id}"),
    ("DELETE", "/api/admin/movies/{movie_id}"),
    ("POST", "/api/admin/movies/{movie_id}/poster"),
    ("POST", "/api/admin/showtimes"),
    ("PATCH", "/api/admin/showtimes/{showtime_id}"),
    ("DELETE", "/api/admin/showtimes/{showtime_id}"),
    ("GET", "/api/admin/bookings"),
    ("GET", "/api/admin/outbox"),
    ("GET", "/api/admin/settings"),
    ("PUT", "/api/admin/settings"),
    ("POST", "/api/admin/reset"),
    ("POST", "/api/admin/sample-bookings"),
]


@pytest.fixture
def admin(client):
    client.headers["X-Admin-Key"] = ADMIN_KEY
    return client


def expect(response, status, code=None):
    assert response.status_code == status, response.text
    body = response.json() if response.content else None
    if code:
        assert body["code"] == code
    return body


def book(client, showtime, count=1, **overrides):
    body = booking_request(showtime, free_seats(showtime)[:count], **overrides)
    return expect(client.post("/api/bookings", json=body), 201)


def cancel(client, booking):
    expect(client.patch(f"/api/bookings/{booking['id']}/cancel", json={"userId": booking["userId"]}), 200)


def new_showtime(**overrides):
    return {
        "movieId": "m-deep-field",
        "startsAt": "2026-12-10T19:15:00Z",
        "screen": "Screen 2",
        "price": 12.5,
        "rows": 8,
        "seatsPerRow": 10,
    } | overrides


def refresh(app, client):
    client.portal.call(refresh_showtimes, app)
    return stored(app).showtimes()


class TestProtection:
    @pytest.mark.parametrize("key", [None, "", "wrong", ADMIN_KEY + "x", ADMIN_KEY[:-1]])
    @pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
    def test_every_route_needs_the_key(self, client, method, path, key):
        url = path.format(movie_id="m-deep-field", showtime_id="st-any")
        headers = {} if key is None else {"X-Admin-Key": key}

        response = client.request(method, url, headers=headers)

        expect(response, 401, "UNAUTHORIZED")
        assert response.headers["WWW-Authenticate"] == "APIKey"

    def test_a_non_ascii_key_is_401_not_500(self, client):
        expect(client.get("/api/admin/settings", headers={"X-Admin-Key": b"caf\xe9"}), 401, "UNAUTHORIZED")

    def test_the_route_list_above_is_complete_and_swagger_can_authorize(self, client):
        schema = client.get("/openapi.json").json()
        operations = {
            (method.upper(), path): operation
            for path, item in schema["paths"].items()
            if path.startswith("/api/admin")
            for method, operation in item.items()
        }

        assert set(operations) == set(ADMIN_ROUTES)
        assert schema["components"]["securitySchemes"]["APIKeyHeader"]["name"] == "X-Admin-Key"
        assert all(op["security"] == [{"APIKeyHeader": []}] for op in operations.values())

    def test_public_routes_have_no_security(self, client):
        assert "security" not in client.get("/openapi.json").json()["paths"]["/api/movies"]["get"]


class TestMovieCreate:
    def test_creates_with_defaults_and_a_location(self, admin):
        response = admin.post("/api/admin/movies", json=MOVIE_EXAMPLE)
        movie = expect(response, 201)

        assert movie["id"] == "m-the-glass-harbour"
        assert (movie["source"], movie["reviews"]) == ("admin", [])
        assert 0 <= movie["hue"] <= 359
        assert movie["posterUrl"] == "/api/posters/m-the-glass-harbour.svg"
        public = expect(admin.get(response.headers["Location"]), 200)
        assert public["title"] == MOVIE_EXAMPLE["title"]
        assert not {"hue", "source"} & public.keys()
        assert admin.get(movie["posterUrl"]).status_code == 200
        assert admin.get(f"/api/movies/{movie['id']}/showtimes").json() == []

    @pytest.mark.parametrize(
        ("title", "expected"),
        [("Amélie & the  Sea!", "m-amelie-the-sea"), ("  WALL 2  ", "m-wall-2")],
    )
    def test_slugs_the_title(self, admin, title, expected):
        assert (
            expect(admin.post("/api/admin/movies", json=MOVIE_EXAMPLE | {"title": title}), 201)["id"]
            == expected
        )

    def test_a_title_without_latin_letters_still_gets_an_id(self, admin):
        movie = expect(admin.post("/api/admin/movies", json=MOVIE_EXAMPLE | {"title": "東京"}), 201)

        assert movie["id"].startswith("m-") and len(movie["id"]) > 2

    def test_explicit_id_and_hue_zero_are_kept(self, admin):
        body = MOVIE_EXAMPLE | {"id": "m-custom", "hue": 0}

        movie = expect(admin.post("/api/admin/movies", json=body), 201)

        assert (movie["id"], movie["hue"]) == ("m-custom", 0)

    def test_duplicates_are_409_including_soft_deleted_ids(self, admin):
        body = MOVIE_EXAMPLE | {"id": "m-deep-field"}
        expect(admin.post("/api/admin/movies", json=body), 409, "ALREADY_EXISTS")
        expect(admin.delete("/api/admin/movies/m-deep-field"), 204)

        expect(admin.post("/api/admin/movies", json=body), 409, "ALREADY_EXISTS")

    @pytest.mark.parametrize(
        "overrides",
        [
            {"id": "../../evil"},
            {"id": "a/b"},
            {"id": "m-x\n"},
            {"title": "   "},
            {"genres": []},
            {"rating": 11},
            {"hue": 360},
        ],
    )
    def test_rejects_bad_bodies(self, admin, overrides):
        expect(admin.post("/api/admin/movies", json=MOVIE_EXAMPLE | overrides), 400, "VALIDATION")


class TestMovieUpdate:
    URL = "/api/admin/movies/m-deep-field"

    def test_patch_changes_only_the_given_fields(self, admin):
        before = expect(admin.patch(self.URL, json={}), 200)

        after = expect(admin.patch(self.URL, json={"title": "Deeper Field"}), 200)

        assert after == before | {"title": "Deeper Field"}

    def test_patch_takes_nested_models(self, admin):
        reviews = [{"author": "A", "stars": 2, "text": "Long"}]

        assert expect(admin.patch(self.URL, json={"reviews": reviews}), 200)["reviews"] == reviews

    @pytest.mark.parametrize("field", ["title", "durationMins", "cast", "hue"])
    def test_patch_rejects_explicit_nulls(self, admin, field):
        assert expect(admin.patch(self.URL, json={field: None}), 400)["message"] == f"{field} cannot be null"

    def test_put_replaces_everything_but_identity_and_hue(self, admin):
        before = expect(admin.patch(self.URL, json={}), 200)

        after = expect(admin.put(self.URL, json=MOVIE_EXAMPLE), 200)

        assert {key: after[key] for key in MOVIE_EXAMPLE} == MOVIE_EXAMPLE
        assert after["reviews"] == []  # left out, so replaced by the default
        for kept in ("id", "hue", "source", "posterUrl"):
            assert after[kept] == before[kept]

    def test_put_needs_every_field(self, admin):
        expect(admin.put(self.URL, json={"title": "Only a title"}), 400, "VALIDATION")

    def test_unknown_movie_is_404(self, admin):
        expect(admin.put("/api/admin/movies/nope", json=MOVIE_EXAMPLE), 404, "NOT_FOUND")
        expect(admin.patch("/api/admin/movies/nope", json={}), 404, "NOT_FOUND")


class TestMovieDelete:
    def test_hides_the_movie_and_its_showtimes_and_drops_upcoming_ones(self, app, admin):
        mine = [s for s in showtimes(app) if s.movie_id == "m-deep-field"]
        past = next(s for s in mine if s.starts_at < TEST_NOW)

        response = admin.delete("/api/admin/movies/m-deep-field")

        assert (response.status_code, response.content) == (204, b"")
        assert "m-deep-field" not in {m["id"] for m in admin.get("/api/movies").json()}
        expect(admin.get("/api/movies/m-deep-field"), 404)
        expect(admin.get("/api/movies/m-deep-field/showtimes"), 404)
        expect(admin.get(f"/api/showtimes/{past.id}"), 404)
        left = [s for s in showtimes(app) if s.movie_id == "m-deep-field"]
        assert left == [s for s in mine if s.starts_at <= TEST_NOW]
        expect(admin.delete("/api/admin/movies/m-deep-field"), 404)

    def test_an_upcoming_confirmed_booking_blocks_it(self, app, admin):
        showtime = first_future(showtimes(app), TEST_NOW)
        book(admin, showtime)

        expect(admin.delete(f"/api/admin/movies/{showtime.movie_id}"), 409, "MOVIE_HAS_BOOKINGS")

        expect(admin.get(f"/api/movies/{showtime.movie_id}"), 200)

    def test_past_bookings_keep_their_details(self, app, admin, frozen_clock):
        showtime = first_future(showtimes(app), TEST_NOW)
        booking = book(admin, showtime)
        frozen_clock.set(showtime.starts_at + timedelta(minutes=1))

        expect(admin.delete(f"/api/admin/movies/{showtime.movie_id}"), 204)

        after = admin.get(f"/api/bookings/{booking['id']}", params={"userId": "u-ava"}).json()
        assert after == booking

    def test_cancelled_bookings_keep_their_upcoming_showtime_but_it_cannot_be_booked(self, app, admin):
        showtime = first_future(showtimes(app), TEST_NOW)
        booking = book(admin, showtime)
        cancel(admin, booking)

        expect(admin.delete(f"/api/admin/movies/{showtime.movie_id}"), 204)

        after = admin.get(f"/api/bookings/{booking['id']}", params={"userId": "u-ava"}).json()
        assert (after["movie"], after["showtime"]) == (booking["movie"], booking["showtime"])
        body = booking_request(showtime, free_seats(showtime)[:1])
        expect(admin.post("/api/bookings", json=body), 404, "NOT_FOUND")


class TestShowtimeCreate:
    def test_creates_a_manual_bookable_showtime(self, app, admin):
        response = admin.post("/api/admin/showtimes", json=new_showtime())
        created = expect(response, 201)

        assert created["id"] == "st-m-deep-field-20261210-1915"
        assert (created["source"], created["bookedSeats"], created["seatsRemaining"]) == ("manual", [], 80)
        public = expect(admin.get(response.headers["Location"]), 200)
        assert "source" not in public
        assert book(admin, stored_showtime(app, created["id"]))["totalPrice"] == 12.5
        assert created["id"] in refresh(app, admin)

    def test_id_uses_the_theater_time_zone(self, admin):
        body = new_showtime(startsAt="2027-07-01T23:30:00Z")  # 00:30 the next day in London

        assert expect(admin.post("/api/admin/showtimes", json=body), 201)["id"].endswith("20270702-0030")

    @pytest.mark.parametrize("starts_at", ["2026-11-19T12:00:00Z", "2026-11-01T19:00:00Z"])
    def test_now_and_the_past_are_rejected(self, admin, starts_at):
        error = expect(
            admin.post("/api/admin/showtimes", json=new_showtime(startsAt=starts_at)), 400, "VALIDATION"
        )

        assert error["message"] == "startsAt must be in the future"

    def test_duplicate_is_409(self, admin):
        expect(admin.post("/api/admin/showtimes", json=new_showtime()), 201)
        expect(admin.post("/api/admin/showtimes", json=new_showtime()), 409, "ALREADY_EXISTS")

    def test_unknown_or_deleted_movie_is_404(self, admin):
        expect(admin.post("/api/admin/showtimes", json=new_showtime(movieId="nope")), 404)
        expect(admin.delete("/api/admin/movies/m-deep-field"), 204)
        expect(admin.post("/api/admin/showtimes", json=new_showtime()), 404)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"rows": 0},
            {"rows": 27},
            {"seatsPerRow": 0},
            {"seatsPerRow": 31},
            {"price": 0},
            {"price": -1},
            {"price": 1e30},
            {"price": "NaN"},
            {"startsAt": "2026-12-10T19:15:00"},
            {"startsAt": "9999-12-31T23:59:00-05:00"},
        ],
    )
    def test_rejects_bad_bodies(self, admin, overrides):
        expect(admin.post("/api/admin/showtimes", json=new_showtime(**overrides)), 400, "VALIDATION")


class TestShowtimeUpdate:
    def test_an_edit_detaches_it_from_the_refresh(self, app, admin):
        showtime = first_future(showtimes(app), TEST_NOW)

        edited = expect(admin.patch(f"/api/admin/showtimes/{showtime.id}", json={"price": 5}), 200)

        assert (edited["source"], edited["price"]) == ("manual", 5)
        assert float(refresh(app, admin)[showtime.id].price) == 5

    def test_an_empty_patch_edits_nothing(self, app, admin):
        showtime = first_future(showtimes(app), TEST_NOW)

        assert (
            expect(admin.patch(f"/api/admin/showtimes/{showtime.id}", json={}), 200)["source"] == "generated"
        )

    def test_layout_cannot_drop_booked_seats(self, app, admin):
        showtime = first_future(showtimes(app), TEST_NOW)
        outside = [s for s in showtime.booked_seats if s[0] != "A"]

        error = expect(
            admin.patch(f"/api/admin/showtimes/{showtime.id}", json={"rows": 1}), 409, "LAYOUT_CONFLICT"
        )

        assert error["details"]["outsideSeats"] == outside
        assert stored_showtime(app, showtime.id) == showtime

    def test_layout_can_grow(self, app, admin):
        showtime = first_future(showtimes(app), TEST_NOW)

        grown = expect(admin.patch(f"/api/admin/showtimes/{showtime.id}", json={"rows": 26}), 200)

        assert grown["seatsRemaining"] == 26 * showtime.seats_per_row - len(showtime.booked_seats)

    def test_past_start_and_started_showtimes_are_rejected(self, app, admin):
        future = first_future(showtimes(app), TEST_NOW)
        past = first_past(showtimes(app), TEST_NOW)

        body = {"startsAt": "2026-11-01T19:00:00Z"}
        expect(admin.patch(f"/api/admin/showtimes/{future.id}", json=body), 400, "VALIDATION")
        expect(admin.patch(f"/api/admin/showtimes/{past.id}", json={"price": 1}), 400, "VALIDATION")

    def test_movie_can_change_only_without_bookings(self, app, admin):
        free, booked = [s for s in showtimes(app) if s.starts_at > TEST_NOW and len(free_seats(s)) >= 6][:2]
        target = next(m for m in stored(app).movies() if m not in (free.movie_id, booked.movie_id))
        cancel(admin, book(admin, booked))  # a cancelled booking still references it

        moved = expect(admin.patch(f"/api/admin/showtimes/{free.id}", json={"movieId": target}), 200)

        assert moved["movieId"] == target
        url = f"/api/admin/showtimes/{booked.id}"
        expect(admin.patch(url, json={"movieId": target}), 409, "SHOWTIME_HAS_BOOKINGS")

    def test_unknown_showtime_or_movie_is_404(self, app, admin):
        showtime = first_future(showtimes(app), TEST_NOW)

        expect(admin.patch("/api/admin/showtimes/nope", json={"price": 5}), 404)
        expect(admin.patch(f"/api/admin/showtimes/{showtime.id}", json={"movieId": "nope"}), 404)

    def test_rejects_explicit_null(self, app, admin):
        showtime = first_future(showtimes(app), TEST_NOW)

        expect(admin.patch(f"/api/admin/showtimes/{showtime.id}", json={"startsAt": None}), 400, "VALIDATION")


class TestShowtimeDelete:
    def test_a_generated_showtime_does_not_come_back(self, app, admin):
        showtime = first_future(showtimes(app), TEST_NOW)

        expect(admin.delete(f"/api/admin/showtimes/{showtime.id}"), 204)

        expect(admin.get(f"/api/showtimes/{showtime.id}"), 404)
        assert showtime.id not in refresh(app, admin)

    def test_an_edited_generated_showtime_does_not_come_back_either(self, app, admin):
        showtime = first_future(showtimes(app), TEST_NOW)
        expect(admin.patch(f"/api/admin/showtimes/{showtime.id}", json={"price": 5}), 200)

        expect(admin.delete(f"/api/admin/showtimes/{showtime.id}"), 204)

        assert showtime.id not in refresh(app, admin)

    def test_recreating_a_deleted_id_survives_the_refresh(self, app, admin):
        showtime = first_future(showtimes(app), TEST_NOW)
        expect(admin.delete(f"/api/admin/showtimes/{showtime.id}"), 204)
        body = new_showtime(movieId=showtime.movie_id, startsAt=showtime.starts_at.isoformat())

        assert expect(admin.post("/api/admin/showtimes", json=body), 201)["id"] == showtime.id
        assert refresh(app, admin)[showtime.id].source == "manual"

        expect(admin.delete(f"/api/admin/showtimes/{showtime.id}"), 204)
        assert showtime.id not in refresh(app, admin)

    @pytest.mark.parametrize("cancelled", [False, True])
    def test_any_booking_blocks_it(self, app, admin, cancelled):
        showtime = first_future(showtimes(app), TEST_NOW)
        booking = book(admin, showtime)
        if cancelled:
            cancel(admin, booking)

        expect(admin.delete(f"/api/admin/showtimes/{showtime.id}"), 409, "SHOWTIME_HAS_BOOKINGS")

    def test_unknown_is_404(self, admin):
        expect(admin.delete("/api/admin/showtimes/nope"), 404)


class TestAdminBookings:
    URL = "/api/admin/bookings"

    @pytest.fixture
    def made(self, app, admin, frozen_clock):
        first, second = [s for s in showtimes(app) if s.starts_at > TEST_NOW and len(free_seats(s)) >= 6][:2]
        a = book(admin, first)
        frozen_clock.advance(minutes=1)
        b = book(admin, second, userId="u-leo")
        cancel(admin, b)
        frozen_clock.advance(minutes=1)
        c = book(admin, second)
        return a, b, c

    def ids(self, admin, **params):
        return [b["id"] for b in expect(admin.get(self.URL, params=params), 200)["items"]]

    def test_envelope_defaults_and_order(self, admin, made):
        page = expect(admin.get(self.URL), 200)

        assert (page["total"], page["limit"], page["offset"]) == (3, 20, 0)
        assert [b["id"] for b in page["items"]] == [b["id"] for b in reversed(made)]
        assert "cancelledAt" not in page["items"][0]

    def test_filters(self, admin, made):
        a, b, c = made

        assert self.ids(admin, status="CANCELLED") == [b["id"]]
        assert self.ids(admin, userId="u-ava") == [c["id"], a["id"]]
        assert self.ids(admin, movieId=a["movieId"]) == [
            x["id"] for x in (c, b, a) if x["movieId"] == a["movieId"]
        ]
        assert self.ids(admin, createdFrom=b["createdAt"], createdTo=b["createdAt"]) == [b["id"]]

    def test_paging_keeps_the_total(self, admin, made):
        page = expect(admin.get(self.URL, params={"limit": 1, "offset": 1}), 200)

        assert (page["total"], page["limit"], page["offset"]) == (3, 1, 1)
        assert [b["id"] for b in page["items"]] == [made[1]["id"]]

    @pytest.mark.parametrize(
        "params",
        [
            {"limit": 101},
            {"limit": 0},
            {"status": "NOPE"},
            {"createdFrom": "2026-11-20"},
            {"createdFrom": "2026-11-21T00:00:00Z", "createdTo": "2026-11-20T00:00:00Z"},
        ],
    )
    def test_rejects_bad_params(self, admin, params):
        expect(admin.get(self.URL, params=params), 400, "VALIDATION")

    def test_query_names_are_camel_case_in_openapi(self, admin):
        operation = admin.get("/openapi.json").json()["paths"][self.URL]["get"]

        assert {p["name"] for p in operation["parameters"] if p["in"] == "query"} == {
            "status",
            "movieId",
            "userId",
            "createdFrom",
            "createdTo",
            "limit",
            "offset",
        }


class TestDemoSettings:
    def test_round_trip(self, admin):
        assert admin.get("/api/admin/settings").json() == {"latencyMs": 0, "failureRate": 0}
        body = {"latencyMs": 250, "failureRate": 0.25}

        assert expect(admin.put("/api/admin/settings", json=body), 200) == body
        assert admin.get("/api/admin/settings").json() == body

    @pytest.mark.parametrize(
        "body",
        [
            {"latencyMs": 5001, "failureRate": 0},
            {"latencyMs": -1, "failureRate": 0},
            {"latencyMs": 0, "failureRate": 2},
        ],
    )
    def test_ranges(self, admin, body):
        expect(admin.put("/api/admin/settings", json=body), 400, "VALIDATION")

    def test_defaults_come_from_the_config(self, settings, frozen_clock):
        configured = settings.model_copy(update={"demo_latency_ms": 300, "demo_failure_rate": 0.1})
        with TestClient(create_app(configured, frozen_clock)) as client:
            response = client.get("/api/admin/settings", headers={"X-Admin-Key": ADMIN_KEY})

        assert response.json() == {"latencyMs": 300, "failureRate": 0.1}


class TestReset:
    def test_restores_the_seed_and_keeps_demo_settings(self, app, admin):
        original = showtimes(app)
        showtime = first_future(original, TEST_NOW)
        other = next(s for s in original if s.movie_id != showtime.movie_id and s.starts_at > TEST_NOW)
        book(admin, showtime)
        expect(admin.post("/api/admin/movies", json=MOVIE_EXAMPLE), 201)
        expect(admin.delete(f"/api/admin/showtimes/{other.id}"), 204)
        demo = {"latencyMs": 100, "failureRate": 0.5}
        expect(admin.put("/api/admin/settings", json=demo), 200)

        response = admin.post("/api/admin/reset")

        assert (response.status_code, response.content) == (204, b"")
        assert (stored(app).bookings(), stored(app).tombstones()) == ({}, set())
        assert len(stored(app).movies()) == 12
        assert showtimes(app) == original
        assert admin.get("/api/admin/settings").json() == demo

    def test_moves_the_window_to_the_current_day(self, admin, frozen_clock):
        frozen_clock.advance(days=30)

        expect(admin.post("/api/admin/reset"), 204)

        first = admin.get("/api/movies/m-deep-field/showtimes").json()[0]
        assert first["id"].split("-")[-2] == "20261217"


class TestSampleBookings:
    URL = "/api/admin/sample-bookings"

    def test_adds_the_six_bookings_of_the_angular_helper(self, app, admin):
        before = {s.id: s for s in showtimes(app)}

        assert expect(admin.post(self.URL, params={"userId": "u-mia"}), 200) == {"added": 6}

        mine = admin.get("/api/bookings", params={"userId": "u-mia"}).json()
        kinds = sorted(
            (b["status"], before[b["showtimeId"]].starts_at > TEST_NOW, len(b["seats"])) for b in mine
        )
        assert kinds == [
            ("CANCELLED", True, 2),
            ("CONFIRMED", False, 2),
            ("CONFIRMED", False, 2),
            ("CONFIRMED", True, 1),
            ("CONFIRMED", True, 2),
            ("CONFIRMED", True, 3),
        ]
        upcoming = [b for b in mine if before[b["showtimeId"]].starts_at > TEST_NOW]
        assert len({b["movieId"] for b in upcoming}) == 4
        assert len({before[b["showtimeId"]].starts_at.astimezone(LONDON).date() for b in upcoming}) == 4
        for booking in mine:
            seats = set(booking["seats"])
            assert not seats & set(before[booking["showtimeId"]].booked_seats)
            now_booked = seats <= set(stored_showtime(app, booking["showtimeId"]).booked_seats)
            assert now_booked == (booking["status"] == "CONFIRMED")

    def test_adding_twice_never_shares_a_confirmed_seat(self, app, admin):
        for user in ("u-ava", "u-leo"):
            expect(admin.post(self.URL, params={"userId": user}), 200)

        confirmed = [b for b in stored(app).bookings().values() if b.status == "CONFIRMED"]
        sold = [(b.showtime_id, seat) for b in confirmed for seat in b.seats]
        assert len(sold) == len(set(sold))

    def test_skips_deleted_movies(self, app, admin):
        deleted = first_future(showtimes(app), TEST_NOW).movie_id
        expect(admin.delete(f"/api/admin/movies/{deleted}"), 204)

        expect(admin.post(self.URL, params={"userId": "u-ava"}), 200)

        assert deleted not in {b.movie_id for b in stored(app).bookings().values()}

    def test_unknown_user_is_404(self, admin):
        expect(admin.post(self.URL, params={"userId": "nope"}), 404, "NOT_FOUND")

    def test_can_run_at_startup(self, settings, frozen_clock):
        configured = settings.model_copy(update={"sample_bookings_for": "u-sam"})
        with TestClient(create_app(configured, frozen_clock)) as client:
            assert len(client.get("/api/bookings", params={"userId": "u-sam"}).json()) == 6


class TestPublisherHooks:
    def test_admin_changes_call_the_publisher(self, app, admin):
        calls = []

        class Recorder:
            async def showtime_changed(self, showtime):
                calls.append(("changed", showtime.id))

            async def showtime_deleted(self, showtime_id):
                calls.append(("deleted", showtime_id))

            async def demo_reset(self):
                calls.append(("reset", None))

        app.state.publisher = Recorder()
        showtime = first_future(showtimes(app), TEST_NOW)

        expect(admin.patch(f"/api/admin/showtimes/{showtime.id}", json={"price": 5}), 200)
        expect(admin.delete(f"/api/admin/showtimes/{showtime.id}"), 204)
        expect(admin.post("/api/admin/reset"), 204)

        assert calls == [("changed", showtime.id), ("deleted", showtime.id), ("reset", None)]

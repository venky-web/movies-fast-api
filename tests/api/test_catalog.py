from zoneinfo import ZoneInfo

import pytest

from tests.conftest import TEST_NOW

LONDON = ZoneInfo("Europe/London")


def ids(response):
    return [m["id"] for m in response.json()]


class TestMovieList:
    def test_default_order_is_the_seed_order_with_a_total_header(self, client):
        response = client.get("/api/movies")

        assert response.status_code == 200
        assert len(response.json()) == 12
        assert response.json()[0]["id"] == "m-last-lighthouse"
        assert response.headers["X-Total-Count"] == "12"

    def test_search_matches_title_director_and_cast_case_insensitively(self, client):
        assert ids(client.get("/api/movies", params={"q": "LIGHTHOUSE"})) == ["m-last-lighthouse"]
        assert ids(client.get("/api/movies", params={"q": "imogen hale"})) == ["m-last-lighthouse"]
        assert ids(client.get("/api/movies", params={"q": "nadia ferro"})) == ["m-last-lighthouse"]

    def test_repeated_genre_matches_any(self, client):
        movies = client.get("/api/movies", params=[("genre", "Horror"), ("genre", "Animation")]).json()

        assert movies
        assert all({"Horror", "Animation"} & set(m["genres"]) for m in movies)

    @pytest.mark.parametrize(
        ("sort", "key", "reverse"),
        [
            ("title", "title", False),
            ("rating", "rating", True),
            ("release", "releaseDate", True),
            ("duration", "durationMins", False),
        ],
    )
    def test_sorts(self, client, sort, key, reverse):
        movies = client.get("/api/movies", params={"sort": sort}).json()
        values = [m[key].casefold() if isinstance(m[key], str) and key == "title" else m[key] for m in movies]

        assert values == sorted(values, reverse=reverse)

    def test_limit_and_offset_page_through_the_total(self, client):
        everything = ids(client.get("/api/movies"))
        page = client.get("/api/movies", params={"limit": 5, "offset": 10})

        assert ids(page) == everything[10:15]
        assert page.headers["X-Total-Count"] == "12"

    @pytest.mark.parametrize(
        "params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"sort": "x"}, {"genre": "Western"}]
    )
    def test_rejects_bad_params(self, client, params):
        assert client.get("/api/movies", params=params).status_code == 400


class TestMovieDetails:
    def test_returns_the_public_fields_only(self, client):
        movie = client.get("/api/movies/m-last-lighthouse").json()

        assert movie["title"] == "The Last Lighthouse"
        assert movie["posterUrl"] == "/api/posters/m-last-lighthouse.svg"
        assert not {"hue", "source", "deletedAt", "uploadedPoster"} & movie.keys()

    def test_unknown_movie_is_404(self, client):
        assert client.get("/api/movies/nope").status_code == 404


class TestShowtimes:
    def test_movie_showtimes_include_past_ones_sorted_by_start(self, client):
        showtimes = client.get("/api/movies/m-last-lighthouse/showtimes").json()

        assert all(s["movieId"] == "m-last-lighthouse" for s in showtimes)
        assert [s["startsAt"] for s in showtimes] == sorted(s["startsAt"] for s in showtimes)
        assert showtimes[0]["startsAt"] < TEST_NOW.isoformat()  # past showtimes are included (parity)

    def test_upcoming_only_uses_the_app_clock(self, client):
        showtimes = client.get(
            "/api/movies/m-last-lighthouse/showtimes", params={"upcomingOnly": "true"}
        ).json()

        assert showtimes
        assert all(s["startsAt"] > "2026-11-19T12:00:00.000Z" for s in showtimes)

    def test_date_filter_uses_the_theater_day(self, client):
        showtimes = client.get(
            "/api/movies/m-last-lighthouse/showtimes", params={"date": "2026-11-20"}
        ).json()

        assert 3 <= len(showtimes) <= 4
        assert all(s["id"].split("-")[-2] == "20261120" for s in showtimes)

    def test_seats_remaining_is_computed(self, client):
        showtime = client.get("/api/movies/m-last-lighthouse/showtimes").json()[0]

        assert showtime["seatsRemaining"] == showtime["rows"] * showtime["seatsPerRow"] - len(
            showtime["bookedSeats"]
        )

    def test_showtimes_of_unknown_movie_is_404(self, client):
        assert client.get("/api/movies/nope/showtimes").status_code == 404

    def test_get_showtime_by_id(self, client):
        first = client.get("/api/movies/m-last-lighthouse/showtimes").json()[0]

        assert client.get(f"/api/showtimes/{first['id']}").json() == first
        assert client.get("/api/showtimes/nope").status_code == 404


class TestUsersAndPosters:
    def test_users(self, client):
        users = client.get("/api/users").json()

        assert [u["id"] for u in users] == ["u-ava", "u-leo", "u-mia", "u-sam"]
        assert client.get("/api/users/u-mia").json()["avatarColor"] == "#c2410c"
        assert client.get("/api/users/nope").status_code == 404

    def test_poster_is_svg_with_cache_header(self, client):
        response = client.get("/api/posters/m-last-lighthouse.svg")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/svg+xml")
        assert response.headers["cache-control"] == "public, max-age=86400"
        assert "THE LAST" in response.text

    def test_poster_of_unknown_movie_is_404(self, client):
        assert client.get("/api/posters/nope.svg").status_code == 404


def test_lifespan_seeds_around_the_frozen_clock_not_the_real_date(client):
    first = client.get("/api/movies/m-last-lighthouse/showtimes").json()[0]

    assert first["id"].split("-")[-2] == "20261117"

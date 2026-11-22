"""Compares our JSON with reference output captured from the Angular seed code (tests/fixtures/README.md)."""

import json
from pathlib import Path
from urllib.parse import unquote

import pytest

from app.domain.posters import create_poster

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "mfe-seed.json").read_text(encoding="utf-8"))
MFE_MOVIES = {m["id"]: m for m in FIXTURE["movies"]}


def json_type(value):
    return {bool: "boolean", int: "number", float: "number", str: "string", list: "array", dict: "object"}[
        type(value)
    ]


def assert_same_shape(ours: dict, theirs: dict, extra: set[str] = frozenset()):
    assert set(ours) == set(theirs) | extra
    for key, value in theirs.items():
        assert json_type(ours[key]) == json_type(value), key


def test_movies_match_the_angular_seed_except_generated_fields(client):
    ours = client.get("/api/movies").json()

    assert [m["id"] for m in ours] == list(MFE_MOVIES)  # same records, same order
    for movie in ours:
        theirs = MFE_MOVIES[movie["id"]]
        assert_same_shape(movie, theirs)
        assert_same_shape(movie["reviews"][0], theirs["reviews"][0])
        # posterUrl (relative URL instead of a data: URI) and reviews (PRNG) are known deviations.
        for key in theirs.keys() - {"posterUrl", "reviews"}:
            assert movie[key] == theirs[key], key


def test_users_match_exactly(client):
    assert client.get("/api/users").json() == FIXTURE["users"]


def test_showtimes_have_the_same_shape_and_window(client):
    ours = [s for m in MFE_MOVIES for s in client.get(f"/api/movies/{m}/showtimes").json()]
    theirs = FIXTURE["showtimes"]

    for showtime in ours:
        assert_same_shape(showtime, theirs[0], extra={"seatsRemaining"})
    day_of = lambda s: s["id"].rsplit("-", 2)[1]  # noqa: E731
    assert {day_of(s) for s in ours} == {day_of(s) for s in theirs}
    assert {s["price"] for s in ours} == {s["price"] for s in theirs}
    assert {s["screen"] for s in ours} == {s["screen"] for s in theirs}
    assert all(s["startsAt"].endswith(".000Z") for s in ours)


@pytest.mark.parametrize("movie_id", list(MFE_MOVIES))
def test_posters_are_identical_to_the_typescript_generator(movie_id):
    theirs = unquote(MFE_MOVIES[movie_id]["posterUrl"].removeprefix("data:image/svg+xml;charset=utf-8,"))
    hue = next(
        m["hue"] for m in json.loads((Path("app/domain/data/movies.json")).read_text()) if m["id"] == movie_id
    )

    assert create_poster(MFE_MOVIES[movie_id]["title"], hue) == theirs

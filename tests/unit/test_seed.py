"""Ports of `<mfe>/data/showtimes.seed.spec.ts`, plus the Python-only merge rules (plan 5.7)."""

import random
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.domain.seed import (
    BookingRef,
    apply_bookings_to_showtimes,
    generate_showtimes,
    load_movies,
    merge_showtimes,
    pick_distinct,
)
from tests.conftest import TEST_NOW

LONDON = ZoneInfo("Europe/London")
MOVIES = load_movies()


def local_day(showtime):
    return showtime.starts_at.astimezone(LONDON).date()


def test_generates_3_to_4_showtimes_per_movie_per_day_over_9_days():
    showtimes = generate_showtimes(MOVIES, TEST_NOW, LONDON)

    assert local_day(showtimes[0]).day == 17  # 2 past days
    assert local_day(showtimes[-1]).day == 25  # today + 6
    for movie in MOVIES:
        per_day = Counter(local_day(s) for s in showtimes if s.movie_id == movie.id)
        assert len(per_day) == 9
        assert all(3 <= count <= 4 for count in per_day.values())


def test_is_deterministic_for_the_same_day():
    assert generate_showtimes(MOVIES, TEST_NOW, LONDON) == generate_showtimes(MOVIES, TEST_NOW, LONDON)


def test_moves_with_the_clock():
    later = datetime(2027, 1, 10, 9, 0, tzinfo=LONDON)
    showtimes = generate_showtimes(MOVIES, later, LONDON)

    assert any(s.starts_at > later for s in showtimes)
    assert local_day(showtimes[0]).month == 1


def test_slots_prices_screens_and_ids():
    showtimes = generate_showtimes(MOVIES, TEST_NOW, LONDON)

    for s in showtimes:
        local = s.starts_at.astimezone(LONDON)
        assert (local.hour, local.minute) in {(10, 30), (13, 15), (16, 0), (18, 45), (21, 30)}
        assert float(s.price) == (13.5 if local.hour >= 17 else 9.5)
        assert s.screen in {f"Screen {n}" for n in range(1, 7)}
        assert s.id == f"st-{s.movie_id}-{local:%Y%m%d}-{local:%H%M}"
        assert 6 <= s.rows <= 8 and 8 <= s.seats_per_row <= 12
        assert s.source == "generated"
        assert s.starts_at.utcoffset() == timedelta(0)


def test_skip_ids_leave_out_tombstoned_showtimes_without_changing_the_rest():
    full = generate_showtimes(MOVIES, TEST_NOW, LONDON)
    skipped = generate_showtimes(MOVIES, TEST_NOW, LONDON, skip_ids={full[0].id})

    assert skipped == full[1:]


def test_merges_a_new_window_with_stored_seats_and_keeps_referenced_showtimes():
    stored = generate_showtimes(MOVIES, TEST_NOW, LONDON)
    kept = stored[0].model_copy(update={"booked_seats": [*stored[0].booked_seats, "Z99"]})
    old = stored[1].model_copy(update={"id": "st-old-referenced"})
    dropped = stored[2].model_copy(update={"id": "st-old-unreferenced"})

    merged = merge_showtimes(
        generate_showtimes(MOVIES, TEST_NOW, LONDON),
        [kept, old, dropped, *stored[3:]],
        [BookingRef(showtime_id=old.id)],
    )
    by_id = {s.id: s for s in merged}

    assert "Z99" in by_id[kept.id].booked_seats
    assert old.id in by_id
    assert dropped.id not in by_id


def test_merge_keeps_manual_showtimes_and_skips_tombstones():
    stored = generate_showtimes(MOVIES, TEST_NOW, LONDON)
    edited = stored[0].model_copy(update={"price": 5, "source": "manual"})
    created = stored[1].model_copy(update={"id": "st-admin-made", "source": "manual"})
    deleted_id = stored[2].id

    merged = merge_showtimes(
        generate_showtimes(MOVIES, TEST_NOW, LONDON),
        [edited, created, *stored[3:]],
        [],
        tombstones={deleted_id},
    )
    by_id = {s.id: s for s in merged}

    assert by_id[edited.id].price == 5  # the refresh does not undo admin edits
    assert "st-admin-made" in by_id  # admin-created showtimes survive without bookings
    assert deleted_id not in by_id  # deleted showtimes do not come back


def test_applies_confirmed_bookings_to_rebuilt_showtimes():
    showtime = generate_showtimes(MOVIES, TEST_NOW, LONDON)[0]
    bookings = [
        BookingRef(showtime_id=showtime.id, seats=("Z1",), status="CONFIRMED"),
        BookingRef(showtime_id=showtime.id, seats=("Z2",), status="CANCELLED"),
    ]

    [result] = apply_bookings_to_showtimes([showtime], bookings)

    assert "Z1" in result.booked_seats
    assert "Z2" not in result.booked_seats


def test_pick_distinct_keeps_the_original_order_and_is_deterministic():
    items = list("abcdefgh")
    first = pick_distinct(items, 4, random.Random("seed"))

    assert first == pick_distinct(items, 4, random.Random("seed"))
    assert len(set(first)) == 4
    assert first == sorted(first, key=items.index)


def test_reviews_are_deterministic_and_in_range():
    assert [m.reviews for m in load_movies()] == [m.reviews for m in MOVIES]
    for movie in MOVIES:
        assert len(movie.reviews) == 3
        assert len({r.author for r in movie.reviews}) == 3
        assert all(1 <= r.stars <= 5 for r in movie.reviews)

"""ORM models, kept separate from the Pydantic schemas (the API contract).

The relationships are `lazy="raise"`: with async sessions a lazy load can't work, so every query has to name
what it loads (`selectinload`).
"""

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Dialect,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

# Named constraints, because SQLite migrations ("batch mode") can only change a constraint that has a name.
NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UtcDateTime(TypeDecorator[dt.datetime]):
    """Stores UTC and always returns an aware UTC datetime. SQLite itself forgets time zones (pitfall 23)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Only aware datetimes can be stored")
        return value.astimezone(dt.UTC)

    def process_result_value(self, value: dt.datetime | None, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map: dict[Any, Any] = {
        dt.datetime: UtcDateTime(),
        Decimal: Numeric(10, 2),
        list[str]: JSON,
    }


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(primary_key=True)
    position: Mapped[int]  # seed order
    name: Mapped[str]
    email: Mapped[str]
    avatar_color: Mapped[str]


class MovieRow(Base):
    __tablename__ = "movies"

    id: Mapped[str] = mapped_column(primary_key=True)
    position: Mapped[int]  # seed order: the default list order, and generated screen numbers depend on it
    title: Mapped[str]
    genres: Mapped[list[str]]
    duration_mins: Mapped[int]
    rating: Mapped[float]
    language: Mapped[str]
    poster_url: Mapped[str]
    synopsis: Mapped[str]
    release_date: Mapped[dt.date]
    director: Mapped[str]
    cast: Mapped[list[str]]
    hue: Mapped[int]
    source: Mapped[str]
    deleted_at: Mapped[dt.datetime | None]
    uploaded_poster: Mapped[str | None]

    reviews: Mapped[list["ReviewRow"]] = relationship(order_by="ReviewRow.id", lazy="raise")


class ReviewRow(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)  # also the order
    movie_id: Mapped[str] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), index=True)
    author: Mapped[str]
    stars: Mapped[int]
    text: Mapped[str]


class ShowtimeRow(Base):
    __tablename__ = "showtimes"
    __table_args__ = (Index(None, "movie_id", "starts_at"),)

    id: Mapped[str] = mapped_column(primary_key=True)
    movie_id: Mapped[str] = mapped_column(ForeignKey("movies.id"))
    starts_at: Mapped[dt.datetime]
    screen: Mapped[str]
    price: Mapped[Decimal]
    rows: Mapped[int]
    seats_per_row: Mapped[int]
    source: Mapped[str]

    seat_rows: Mapped[list["BookedSeatRow"]] = relationship(order_by="BookedSeatRow.id", lazy="raise")

    @property
    def booked_seats(self) -> list[str]:  # read by `ShowtimeRecord.model_validate(row, from_attributes=True)`
        return [seat.seat_id for seat in self.seat_rows]


class ShowtimeTombstoneRow(Base):
    __tablename__ = "showtime_tombstones"

    # No foreign key: the showtime is gone, that is the point.
    showtime_id: Mapped[str] = mapped_column(primary_key=True)


class BookingRow(Base):
    __tablename__ = "bookings"
    __table_args__ = (Index(None, "user_id", "created_at"),)

    seq: Mapped[int] = mapped_column(primary_key=True)  # insertion order, breaks ties in "newest first"
    id: Mapped[str] = mapped_column(unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    movie_id: Mapped[str] = mapped_column(ForeignKey("movies.id"))
    showtime_id: Mapped[str] = mapped_column(ForeignKey("showtimes.id"), index=True)
    total_price: Mapped[Decimal]
    customer_name: Mapped[str]
    email: Mapped[str]
    created_at: Mapped[dt.datetime]
    status: Mapped[str]
    cancelled_at: Mapped[dt.datetime | None]

    seat_rows: Mapped[list["BookingSeatRow"]] = relationship(order_by="BookingSeatRow.id", lazy="raise")

    @property
    def seats(self) -> list[str]:
        return [seat.seat_id for seat in self.seat_rows]


class BookingSeatRow(Base):
    """The seats a booking was made for. They stay when it is cancelled (history)."""

    __tablename__ = "booking_seats"
    __table_args__ = (UniqueConstraint("booking_id", "seat_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"))
    seat_id: Mapped[str]


class BookedSeatRow(Base):
    """The seats that are taken right now. The unique constraint is what prevents double booking (5.4)."""

    __tablename__ = "booked_seats"
    __table_args__ = (UniqueConstraint("showtime_id", "seat_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)  # also the order
    showtime_id: Mapped[str] = mapped_column(ForeignKey("showtimes.id", ondelete="CASCADE"))
    seat_id: Mapped[str]
    # NULL for the random pre-booked seats of generated showtimes.
    booking_id: Mapped[str | None] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), index=True)


class OutboxRow(Base):
    __tablename__ = "outbox"

    seq: Mapped[int] = mapped_column(primary_key=True)
    id: Mapped[str] = mapped_column(unique=True)
    to: Mapped[str]
    subject: Mapped[str]
    body: Mapped[str]
    # A foreign key, so an email can never outlive a reset of the booking it is about.
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str]
    sent_at: Mapped[dt.datetime]

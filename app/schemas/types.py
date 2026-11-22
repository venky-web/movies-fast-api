"""Annotated types shared by the schemas."""

from datetime import MAXYEAR, MINYEAR, UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, Field, PlainSerializer, StringConstraints, WithJsonSchema

CENT = Decimal("0.01")


def to_utc(value: datetime) -> datetime:
    try:
        utc = value.astimezone(UTC)
    except OverflowError as exc:  # only ValueError becomes a validation error; anything else is a 500
        raise ValueError("Date is out of range") from exc
    # A year of slack at both ends, so that converting to the theater's time zone can't overflow either.
    if not MINYEAR < utc.year < MAXYEAR:
        raise ValueError("Date is out of range")
    return utc


def to_js_iso(value: datetime) -> str:
    """Same format as JavaScript's `Date.prototype.toISOString()`: `2026-11-19T12:00:00.000Z`."""
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def round_money(value: Decimal) -> Decimal:
    """Half-up rounding to cents, like `Math.round(x * 100) / 100`.

    Python's `round()` uses banker's rounding on inexact floats, so it can differ.
    """
    try:
        return value.quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("Amount is out of range") from exc


UtcDatetime = Annotated[
    AwareDatetime,  # naive datetimes are rejected
    AfterValidator(to_utc),
    PlainSerializer(to_js_iso, return_type=str, when_used="json"),
    # The serializer's `str` would lose the format in the response schemas.
    WithJsonSchema({"type": "string", "format": "date-time"}),
]

# Pydantic serialises Decimal as a JSON *string* by default; the contract needs a number.
Money = Annotated[
    Decimal,
    AfterValidator(round_money),
    PlainSerializer(float, return_type=float, when_used="json"),
]

# A price sent by a client. The range is checked before rounding.
Price = Annotated[
    Decimal,
    Field(ge=CENT, le=Decimal("1000"), allow_inf_nan=False),
    AfterValidator(round_money),
    PlainSerializer(float, return_type=float, when_used="json"),
]

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel, ValidationError

from app.schemas.types import Money, UtcDatetime


class Sample(BaseModel):
    at: UtcDatetime
    price: Money


def test_utc_datetime_serialises_like_to_iso_string():
    sample = Sample(at=datetime(2026, 11, 19, 12, 0, tzinfo=ZoneInfo("Europe/London")), price=1)

    assert sample.model_dump(mode="json")["at"] == "2026-11-19T12:00:00.000Z"


def test_utc_datetime_converts_to_utc():
    summer = Sample(at="2026-07-01T10:30:00+01:00", price=1)

    assert summer.model_dump(mode="json")["at"] == "2026-07-01T09:30:00.000Z"


def test_utc_datetime_rejects_naive_values():
    with pytest.raises(ValidationError):
        Sample(at=datetime(2026, 11, 19, 12, 0), price=1)


def test_money_is_a_json_number_rounded_half_up():
    sample = Sample(at="2026-11-19T12:00:00Z", price=Decimal("1.005"))

    assert sample.price == Decimal("1.01")  # round(1.005, 2) would give 1.0 with floats
    assert sample.model_dump(mode="json")["price"] == 1.01
    assert sample.model_dump_json().endswith('"price":1.01}')

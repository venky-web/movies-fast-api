"""App-level clock (the Python version of the Angular `NOW` token).

Everything reads time from `app.state.clock`, so tests can freeze it for requests, lifespan seeding,
middleware and background tasks alike. `dependency_overrides` would only reach request handling.
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current time as an aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    def __init__(self, at: datetime) -> None:
        self.set(at)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock needs an aware datetime")
        self._at = at.astimezone(UTC)

    def advance(self, **delta: float) -> None:
        self._at += timedelta(**delta)

from enum import StrEnum

from app.schemas.base import CamelModel
from app.schemas.types import UtcDatetime


class EmailKind(StrEnum):
    CONFIRMATION = "confirmation"
    CANCELLATION = "cancellation"


class OutboxEmail(CamelModel):
    """A "sent" email. Nothing is really sent."""

    id: str
    to: str
    subject: str
    body: str
    booking_id: str
    kind: EmailKind
    sent_at: UtcDatetime

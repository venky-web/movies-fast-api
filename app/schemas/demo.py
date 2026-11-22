from pydantic import Field

from app.schemas.base import CamelModel


class DemoSettings(CamelModel):
    """Runtime demo settings, not the env-based `Settings`. The Phase 5 simulation middleware reads them."""

    latency_ms: int = Field(ge=0, le=5000)
    failure_rate: float = Field(ge=0, le=1)


class SampleBookingsResult(CamelModel):
    added: int

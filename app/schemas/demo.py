from pydantic import ConfigDict, Field

from app.schemas.base import CamelModel


class DemoSettings(CamelModel):
    """Runtime demo settings. They go back to the configured defaults when the server restarts."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"latencyMs": 300, "failureRate": 0.1}]})

    latency_ms: int = Field(ge=0, le=5000, description="Delay of each simulated request")
    failure_rate: float = Field(ge=0, le=1, description="0 never fails, 1 always fails")


class SampleBookingsResult(CamelModel):
    added: int

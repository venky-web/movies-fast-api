from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration from environment variables and `.env`.

    Not to be confused with `DemoSettings` (runtime latency / failure rate, changed through the admin API).
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["dev", "test", "prod"] = "dev"
    admin_api_key: str = "dev-admin-key"
    storage_backend: Literal["memory", "sql"] = "memory"
    theater_tz: str = "Europe/London"
    database_url: str = "sqlite+aiosqlite:///./movies.db"
    demo_latency_ms: int = 0
    demo_failure_rate: float = 0.0
    showtime_refresh_seconds: float = Field(3600, gt=0)  # 0 would make the refresh loop spin
    sse_heartbeat_seconds: float = Field(15, gt=0)  # above 15 has no effect: FastAPI pings then anyway
    sample_bookings_for: str | None = None
    use_showtime_locks: bool = True
    uploads_dir: Path = Path("uploads")
    cors_origins: list[str] = ["http://localhost:4200"]


@lru_cache
def get_settings() -> Settings:
    return Settings()

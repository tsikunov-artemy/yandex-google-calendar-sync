from __future__ import annotations

from enum import Enum

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SyncDirection(str, Enum):
    BIDIRECTIONAL = "bidirectional"
    YANDEX_TO_GOOGLE = "yandex_to_google"
    GOOGLE_TO_YANDEX = "google_to_yandex"


class ConflictStrategy(str, Enum):
    LAST_WRITE_WINS = "last_write_wins"
    YANDEX_WINS = "yandex_wins"
    GOOGLE_WINS = "google_wins"
    SKIP = "skip"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
    )

    # Yandex Calendar (CalDAV)
    yandex_caldav_url: str = "https://caldav.yandex.ru"
    yandex_username: str = ""
    yandex_password: str = ""
    yandex_calendar_name: str = ""

    # Google Calendar
    google_credentials_path: str = "credentials/google_credentials.json"
    google_token_path: str = "credentials/token.json"
    google_calendar_id: str = "primary"
    google_calendar_name: str = ""  # If set, find or create a sub-calendar with this name

    # Sync
    sync_interval_minutes: int = 10
    sync_direction: SyncDirection = SyncDirection.YANDEX_TO_GOOGLE
    conflict_strategy: ConflictStrategy = ConflictStrategy.LAST_WRITE_WINS
    sync_window_days_past: int = 30
    sync_window_days_future: int = 180

    # Adaptive scheduler: working hours (UTC)
    # Default: 05:00-18:00 UTC = 08:00-21:00 Moscow
    work_hours_start: int = 5
    work_hours_end: int = 18

    # Rate limiting
    yandex_request_delay_seconds: float = 2.0

    # Database
    db_path: str = "data/sync.db"

    # Logging
    log_level: str = "INFO"
    log_file: str = "data/sync.log"

    @model_validator(mode="after")
    def validate_credentials(self):
        if not self.yandex_username or not self.yandex_password:
            raise ValueError("YANDEX_USERNAME and YANDEX_PASSWORD are required")
        return self


def get_settings() -> Settings:
    return Settings()

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    zoom_client_id: str
    zoom_client_secret: str
    zoom_webhook_secret_token: str

    moveris_api_key: str
    moveris_mode: Literal["fast", "live"] = "fast"

    frame_sample_rate: int = 5
    # Score threshold per Moveris API docs: score >= 65 = live
    # https://documentation.moveris.com/api-reference/fast-check
    liveness_threshold: int = 65
    max_concurrent_sessions: int = 50

    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

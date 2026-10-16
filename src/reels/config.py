"""Process-wide configuration, read from the environment and `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # `ADMIN_CHAT_ID=` in .env means "not set", not "invalid int".
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://reels:reels@localhost:5432/reels"
    test_database_url: str = "postgresql+psycopg://reels:reels@localhost:5432/reels_test"

    telegram_bot_token: SecretStr | None = None
    telegram_api_id: int | None = None
    telegram_api_hash: SecretStr | None = None
    telegram_api_url: str = "http://localhost:8081"
    telegram_files_container_path: str = "/var/lib/telegram-bot-api"
    telegram_files_host_path: Path = Path("data/telegram-bot-api")
    admin_chat_id: int | None = None

    openai_api_key: SecretStr | None = None
    openai_model_text: str | None = None
    openai_model_vision: str | None = None
    openai_model_embed: str | None = None

    pexels_api_key: SecretStr | None = None

    data_dir: Path = Path("data")
    fonts_dir: Path = Path("assets/fonts")
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    transcribe_engine: Literal["whisperx", "mlx"] = "whisperx"
    whisperx_model: str = "large-v3-turbo"
    whisperx_batch_size: int = 8
    mlx_whisper_model: str = "mlx-community/whisper-large-v3-turbo"
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "text"


@lru_cache
def get_settings() -> Settings:
    return Settings()

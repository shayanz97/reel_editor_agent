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

    pixabay_api_key: SecretStr | None = None
    # Dimension of OPENAI_MODEL_EMBED vectors; must match the broll_assets.embedding column.
    openai_embed_dim: int = 1536

    data_dir: Path = Path("data")
    fonts_dir: Path = Path("assets/fonts")
    templates_dir: Path = Path("assets/templates")
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    transcribe_engine: Literal["whisperx", "mlx"] = "whisperx"
    whisperx_model: str = "large-v3-turbo"
    whisperx_batch_size: int = 8
    mlx_whisper_model: str = "mlx-community/whisper-large-v3-turbo"
    # x264 preset for exports (None = RenderOptions default "medium").
    render_preset: str | None = None

    # Worker
    worker_poll_interval_s: float = 1.0
    worker_max_attempts: int = 3
    # Seconds between claim and the step taking its row lock before a job counts as orphaned.
    worker_orphan_grace_s: float = 60.0
    # Hard limit per step; the step's process is killed afterwards. Upper bounds, not estimates.
    worker_step_timeout_s: float = 1800.0
    worker_cleanup_interval_h: float = 24.0
    # Where steps get engines/VAD/LLM from ('module:function'). Only changed by tests.
    worker_providers_factory: str = "reels.pipeline.runner:default_providers"
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "text"


@lru_cache
def get_settings() -> Settings:
    return Settings()

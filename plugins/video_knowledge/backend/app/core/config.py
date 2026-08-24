from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from VKC_* environment variables and .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="VKC_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Video Knowledge Collector"
    version: str = "0.6.0"
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    storage_root: Path = Path("storage")
    worker_poll_interval_seconds: float = 1.0
    worker_lease_seconds: float = 15.0
    demo_stage_delay_seconds: float = 0.35
    yt_dlp_cookies_file: Path | None = None
    download_proxy: str | None = None
    download_max_video_height: int = 1080
    ffprobe_path: str = "ffprobe"
    ffmpeg_path: str = "ffmpeg"
    asr_enabled: bool = True
    asr_model: str = "small"
    asr_device: str = "auto"
    asr_compute_type: str = "auto"
    asr_language: str | None = None
    asr_vad_filter: bool = True
    asr_word_timestamps: bool = False
    asr_chunk_seconds: int = 120
    asr_overlap_seconds: float = 1.5
    auto_analyze: bool = True
    hermes_base_url: str = "http://127.0.0.1:8642/v1"
    hermes_api_mode: str = "chat_completions"
    hermes_model: str = "hermes-agent"
    hermes_api_key: SecretStr | None = None
    # Local Hermes models can need several minutes for the final reduce over
    # all mapped transcript chunks. A short client timeout does not reliably
    # cancel llama-server generation and can create a queue of orphan retries.
    hermes_timeout_seconds: float = 600.0
    hermes_max_retries: int = 3
    hermes_max_output_tokens: int = 4096
    analysis_chunk_characters: int = 12000
    analysis_prompt_version: str = "1.1.0"
    analysis_structured_attempts: int = 2
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

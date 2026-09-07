from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
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
    # Messaging admission policy only; Desktop ingest remains independent.
    # Stage 0 defines policy, while workflow handlers enforce it in later stages.
    messaging_ingest_enabled: bool = False
    messaging_allowed_platforms: list[Literal["feishu"]] = Field(
        default_factory=lambda: ["feishu"]
    )
    messaging_max_active_per_user: int = Field(default=1, ge=1, le=10)
    messaging_max_active_per_chat: int = Field(default=3, ge=1, le=50)
    messaging_max_submissions_per_user_per_day: int = Field(default=10, ge=1, le=1000)
    messaging_max_submissions_per_chat_per_day: int = Field(default=30, ge=1, le=5000)
    messaging_max_video_duration_seconds: int = Field(default=1800, ge=1, le=86400)
    messaging_max_video_height: Literal[360, 480, 720, 1080] = 720
    messaging_min_free_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024,
        ge=128 * 1024 * 1024,
        le=1024 * 1024 * 1024 * 1024,
    )
    messaging_personal_data_retention_days: int = Field(default=90, ge=1, le=3650)
    messaging_retention_cleanup_interval_seconds: float = Field(
        default=86400.0, ge=60.0, le=604800.0
    )
    notification_lease_seconds: float = Field(default=30.0, ge=1.0, le=600.0)
    notification_max_attempts: int = Field(default=8, ge=1, le=100)
    notification_retry_base_seconds: float = Field(default=5.0, ge=0.1, le=3600.0)
    notification_retry_max_seconds: float = Field(default=900.0, ge=0.1, le=86400.0)
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
    # Adaptive analysis uses 24..96 segments according to transcript duration;
    # this remains the operator-controlled safety ceiling.
    analysis_max_chunk_segments: int = 96
    analysis_prompt_version: str = "1.2.2"
    analysis_structured_attempts: int = 2
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    def messaging_ingest_allowed(self, platform: str) -> bool:
        return (
            self.messaging_ingest_enabled
            and platform in self.messaging_allowed_platforms
        )

    @field_validator("messaging_max_video_height", mode="before")
    @classmethod
    def parse_messaging_height(cls, value: object) -> object:
        # Environment values are strings; integer Literal does not coerce them.
        return int(value) if isinstance(value, str) and value.isdecimal() else value


@lru_cache
def get_settings() -> Settings:
    return Settings()

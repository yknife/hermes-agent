from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ComponentHealth(BaseModel):
    status: Literal["ok", "error"]
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    version: str
    environment: str
    timestamp: datetime
    request_id: str
    components: dict[str, ComponentHealth]


class ASRModelStatus(BaseModel):
    name: str
    size: str
    description: str
    downloaded: bool
    downloading: bool = False


class ASRSettingsUpdate(BaseModel):
    enabled: bool = True
    model: Literal["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"] = (
        "small"
    )
    configured_device: Literal["auto", "cpu", "cuda"] = "auto"
    configured_compute_type: Literal["auto", "int8", "float16", "float32"] = "auto"
    language: str | None = Field(default=None, max_length=32)
    vad_filter: bool = True
    word_timestamps: bool = False
    chunk_seconds: int = Field(default=120, ge=30, le=3600)
    overlap_seconds: float = Field(default=1.5, ge=0, le=30)
    auto_analyze: bool = True

    @model_validator(mode="after")
    def validate_chunk_overlap(self) -> "ASRSettingsUpdate":
        if self.overlap_seconds >= self.chunk_seconds:
            raise ValueError("ASR 重叠时长必须小于分片时长")
        return self


class ASRStatusResponse(BaseModel):
    enabled: bool
    model: str
    configured_device: str
    effective_device: str
    configured_compute_type: str
    effective_compute_type: str
    cuda_available: bool
    language: str | None
    vad_filter: bool
    word_timestamps: bool
    chunk_seconds: int
    overlap_seconds: float
    auto_analyze: bool
    models: list[ASRModelStatus]


class RuntimeToolStatus(BaseModel):
    name: str
    available: bool
    version: str | None = None
    detail: str | None = None


class RuntimeStatusResponse(BaseModel):
    ready: bool
    tools: list[RuntimeToolStatus]


class StorageMigrationRequest(BaseModel):
    target_path: str = Field(min_length=1, max_length=32767)


class StorageMigrationStatusResponse(BaseModel):
    id: str | None = None
    phase: Literal[
        "IDLE",
        "COPYING",
        "VERIFYING",
        "SWITCHING",
        "CLEANING",
        "COMPLETED",
        "FAILED",
    ] = "IDLE"
    source_path: str
    target_path: str | None = None
    total_bytes: int = 0
    processed_bytes: int = 0
    total_files: int = 0
    processed_files: int = 0
    progress: float = Field(default=0, ge=0, le=100)
    error: str | None = None
    warning: str | None = None


class StorageSettingsResponse(BaseModel):
    storage_root: str
    migration: StorageMigrationStatusResponse

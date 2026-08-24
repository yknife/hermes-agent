from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("type", "canonical_url", name="uq_sources_canonical_url"),
        UniqueConstraint(
            "type", "platform", "external_id", name="uq_sources_external_id"
        ),
        Index("ix_sources_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class LiveSession(Base):
    __tablename__ = "live_sessions"
    __table_args__ = (
        UniqueConstraint("source_id", "session_key", name="uq_live_session_key"),
        Index("ix_live_sessions_source_created", "source_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_key: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    anchor: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    media_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class MediaItem(Base):
    __tablename__ = "media_items"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "external_id", name="uq_media_source_external_id"
        ),
        Index("ix_media_items_published_at", "published_at"),
        Index("ix_media_items_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    webpage_url: Mapped[str] = mapped_column(Text, nullable=False)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class MediaAsset(Base):
    __tablename__ = "media_assets"
    __table_args__ = (Index("ix_media_assets_media_kind", "media_id", "kind"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    media_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("media_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    container: Mapped[str | None] = mapped_column(String(64), nullable=True)
    codec: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="READY", nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Transcript(Base):
    __tablename__ = "transcripts"
    __table_args__ = (
        UniqueConstraint("media_id", "version", name="uq_transcript_media_version"),
        Index("ix_transcripts_media_created", "media_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    media_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("media_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    plain_text_path: Mapped[str] = mapped_column(Text, nullable=False)
    segments_path: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (
        UniqueConstraint("transcript_id", "segment_index", name="uq_segment_index"),
        Index("ix_transcript_segments_time", "transcript_id", "start_ms"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    transcript_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("transcripts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker: Mapped[str | None] = mapped_column(String(255), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint(
            "media_id",
            "document_type",
            "version",
            name="uq_knowledge_media_type_version",
        ),
        Index(
            "ix_knowledge_media_type_created", "media_id", "document_type", "created_at"
        ),
        Index("ix_knowledge_fingerprint", "fingerprint"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    media_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("media_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    transcript_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("transcripts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_type: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("progress >= 0 AND progress <= 100", name="ck_jobs_progress"),
        Index("ix_jobs_schedule", "status", "next_run_at", "priority"),
        Index("ix_jobs_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    media_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    input_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_no", name="uq_job_attempt_no"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_path: Mapped[str | None] = mapped_column(Text, nullable=True)


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (
        UniqueConstraint("job_id", "sequence", name="uq_job_event_sequence"),
        Index("ix_job_events_job_id_id", "job_id", "id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    progress: Mapped[float] = mapped_column(Float, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    data_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

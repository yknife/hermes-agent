from enum import StrEnum


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_LIVE = "WAITING_LIVE"
    RETRY_WAIT = "RETRY_WAIT"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {
            JobStatus.SUCCEEDED,
            JobStatus.PARTIAL,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }


class JobStage(StrEnum):
    CREATED = "CREATED"
    PROBING = "PROBING"
    ACQUIRING_MEDIA = "ACQUIRING_MEDIA"
    MONITORING_LIVE = "MONITORING_LIVE"
    RECORDING = "RECORDING"
    VERIFYING_MEDIA = "VERIFYING_MEDIA"
    ACQUIRING_SUBTITLE = "ACQUIRING_SUBTITLE"
    TRANSCRIBING = "TRANSCRIBING"
    NORMALIZING_TRANSCRIPT = "NORMALIZING_TRANSCRIPT"
    ANALYZING = "ANALYZING"
    INDEXING = "INDEXING"
    FINALIZING = "FINALIZING"
    DONE = "DONE"


class JobType(StrEnum):
    INGEST_VIDEO = "INGEST_VIDEO"
    RECORD_LIVE = "RECORD_LIVE"
    ANALYZE = "ANALYZE"
    EXPORT = "EXPORT"
    DEMO = "DEMO"


class JobEventType(StrEnum):
    CREATED = "job.created"
    STATE_CHANGED = "job.state_changed"
    PROGRESS = "job.progress"
    CANCEL_REQUESTED = "job.cancel_requested"
    LEASE_RENEWED = "job.lease_renewed"


class SourceType(StrEnum):
    VIDEO = "VIDEO"
    LIVE = "LIVE"


class MediaAssetKind(StrEnum):
    VIDEO = "VIDEO"
    THUMBNAIL = "THUMBNAIL"
    AUDIO = "AUDIO"
    INFO_JSON = "INFO_JSON"
    SUBTITLE_ORIGINAL = "SUBTITLE_ORIGINAL"
    TRANSCRIPT_JSON = "TRANSCRIPT_JSON"
    TRANSCRIPT_TEXT = "TRANSCRIPT_TEXT"
    LIVE_SEGMENT = "LIVE_SEGMENT"


TERMINAL_STATUSES = {
    JobStatus.SUCCEEDED,
    JobStatus.PARTIAL,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
}

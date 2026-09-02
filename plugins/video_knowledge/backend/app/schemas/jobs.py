import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from plugins.video_knowledge.backend.app.domain.enums import JobType
from plugins.video_knowledge.backend.app.infrastructure.db.base import Job, JobEvent


class JobCreate(BaseModel):
    type: JobType = JobType.DEMO
    priority: int = Field(default=100, ge=0, le=1000)
    max_attempts: int = Field(default=3, ge=1, le=20)
    input: dict[str, Any] = Field(default_factory=dict)


class JobRead(BaseModel):
    id: str
    source_id: str | None
    media_id: str | None
    type: str
    status: str
    stage: str
    priority: int
    progress: float
    attempt_count: int
    max_attempts: int
    next_run_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    cancel_requested_at: datetime | None
    input: dict[str, Any]
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_job(cls, job: Job) -> "JobRead":
        input_value = json.loads(job.input_json)
        # A request-scoped cookie path is an implementation credential detail.
        # Workers read it from the persisted job directly; API consumers must
        # never receive it through task-center/job responses.
        input_value.pop("cookies_file", None)
        return cls(
            id=job.id,
            source_id=job.source_id,
            media_id=job.media_id,
            type=job.type,
            status=job.status,
            stage=job.stage,
            priority=job.priority,
            progress=job.progress,
            attempt_count=job.attempt_count,
            max_attempts=job.max_attempts,
            next_run_at=job.next_run_at,
            lease_owner=job.lease_owner,
            lease_expires_at=job.lease_expires_at,
            cancel_requested_at=job.cancel_requested_at,
            input=input_value,
            result=json.loads(job.result_json) if job.result_json else None,
            error_code=job.error_code,
            error_message=job.error_message,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )


class JobList(BaseModel):
    items: list[JobRead]
    next_cursor: str | None


class JobEventRead(BaseModel):
    event_id: str
    type: str
    occurred_at: datetime
    data: dict[str, Any]

    @classmethod
    def from_orm_event(cls, event: JobEvent) -> "JobEventRead":
        return cls(
            event_id=event.id,
            type=event.event_type,
            occurred_at=event.created_at,
            data={
                "job_id": event.job_id,
                "sequence": event.sequence,
                "status": event.to_status,
                "from_status": event.from_status,
                "stage": event.stage,
                "overall_progress": event.progress,
                "message": event.message,
                "actor": event.actor,
                **json.loads(event.data_json),
            },
        )

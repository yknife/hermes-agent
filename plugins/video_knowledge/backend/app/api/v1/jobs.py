from datetime import UTC, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request, status

from plugins.video_knowledge.backend.app.api.deps import get_database
from plugins.video_knowledge.backend.app.domain.enums import JobStatus
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.jobs import (
    JobCreate,
    JobEventRead,
    JobList,
    JobRead,
)
from plugins.video_knowledge.backend.app.services.job_service import (
    JobQueryService,
    JobStateMachine,
)
from plugins.video_knowledge.backend.app.services.live_service import LiveSourceService

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", response_model=JobRead, status_code=status.HTTP_201_CREATED)
async def create_job(
    payload: JobCreate,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> JobRead:
    job = await JobStateMachine(database).create(
        job_type=payload.type,
        priority=payload.priority,
        max_attempts=payload.max_attempts,
        input_data=payload.input,
        actor=f"api:{request.state.request_id}",
    )
    return JobRead.from_orm_job(job)


@router.get("", response_model=JobList)
async def list_jobs(
    database: Annotated[Database, Depends(get_database)],
    statuses: Annotated[list[JobStatus] | None, Query(alias="status")] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    scope: Literal["all", "today"] = "all",
) -> JobList:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    jobs = await JobQueryService(database).list_jobs(
        statuses=statuses,
        created_since=today_start if scope == "today" else None,
        include_active_live=scope == "today",
        cursor=cursor,
        limit=limit + 1,
    )
    has_more = len(jobs) > limit
    visible = jobs[:limit]
    return JobList(
        items=[JobRead.from_orm_job(job) for job in visible],
        next_cursor=visible[-1].id if has_more and visible else None,
    )


@router.get("/{job_id}", response_model=JobRead)
async def get_job(
    job_id: str, database: Annotated[Database, Depends(get_database)]
) -> JobRead:
    return JobRead.from_orm_job(await JobQueryService(database).get(job_id))


@router.get("/{job_id}/events", response_model=list[JobEventRead])
async def get_job_events(
    job_id: str,
    database: Annotated[Database, Depends(get_database)],
    after_id: str | None = None,
) -> list[JobEventRead]:
    events = await JobQueryService(database).events(job_id, after_id=after_id)
    return [JobEventRead.from_orm_event(event) for event in events]


@router.post("/{job_id}/cancel", response_model=JobRead)
async def cancel_job(
    job_id: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> JobRead:
    current = await JobQueryService(database).get(job_id)
    if current.type == "RECORD_LIVE":
        return JobRead.from_orm_job(
            await LiveSourceService(database).cancel_monitor(
                job_id, actor=f"api:{request.state.request_id}"
            )
        )
    job = await JobStateMachine(database).request_cancel(
        job_id, actor=f"api:{request.state.request_id}"
    )
    return JobRead.from_orm_job(job)


@router.post("/{job_id}/retry", response_model=JobRead)
async def retry_job(
    job_id: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> JobRead:
    current = await JobQueryService(database).get(job_id)
    if current.type == "RECORD_LIVE":
        return JobRead.from_orm_job(
            await LiveSourceService(database).retry_monitor(
                job_id, actor=f"api:{request.state.request_id}"
            )
        )
    job = await JobStateMachine(database).retry(
        job_id, actor=f"api:{request.state.request_id}"
    )
    return JobRead.from_orm_job(job)


@router.post("/{job_id}/pause", response_model=JobRead)
async def pause_job(
    job_id: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> JobRead:
    job = await JobStateMachine(database).pause(
        job_id, actor=f"api:{request.state.request_id}"
    )
    return JobRead.from_orm_job(job)


@router.post("/{job_id}/resume", response_model=JobRead)
async def resume_job(
    job_id: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> JobRead:
    job = await JobStateMachine(database).resume(
        job_id, actor=f"api:{request.state.request_id}"
    )
    return JobRead.from_orm_job(job)

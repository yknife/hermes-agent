import asyncio

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.jobs import JobEventRead
from plugins.video_knowledge.backend.app.services.job_service import JobQueryService

router = APIRouter(tags=["events"])


@router.websocket("/ws")
async def event_stream(
    websocket: WebSocket,
    client_id: str = Query(min_length=1, max_length=128),
    last_event_id: str | None = None,
) -> None:
    del client_id
    await websocket.accept()
    database: Database = websocket.app.state.database
    query_service = JobQueryService(database)
    cursor = last_event_id
    idle_ticks = 0
    try:
        while True:
            events = await query_service.global_events(after_id=cursor)
            for event in events:
                payload = JobEventRead.from_orm_event(event)
                await websocket.send_json(payload.model_dump(mode="json"))
                cursor = event.id
            if events:
                idle_ticks = 0
            else:
                idle_ticks += 1
                if idle_ticks >= 20:
                    await websocket.send_json({"type": "system.heartbeat"})
                    idle_ticks = 0
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return

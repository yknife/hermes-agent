"""Hermes Dashboard transport for the bundled Video Knowledge runtime."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from hermes_constants import get_hermes_home
from plugins.video_knowledge.backend.app.domain.errors import DomainError
from plugins.video_knowledge.backend.app.integration.controller import (
    VideoKnowledgeController,
)
from plugins.video_knowledge.backend.app.integration.runtime import runtime_registry
from plugins.video_knowledge.backend.app.schemas.jobs import JobEventRead
from plugins.video_knowledge.backend.app.services.job_service import JobQueryService
from plugins.video_knowledge.backend.media_adapters.errors import MediaToolError
from pydantic import ValidationError

router = APIRouter()


def _gateway_base_url() -> str:
    """Return the local OpenAI-compatible gateway owned by Hermes."""
    port = os.environ.get("API_SERVER_PORT", "8642").strip()
    return f"http://127.0.0.1:{port if port.isdigit() else '8642'}"


def _gateway_api_key() -> str | None:
    # Desktop creates a process-local key for the embedded API server.  Prefer
    # that exact value over any persisted secret with the same name so the
    # supervised VKC worker authenticates against the listener started by this
    # process.  A stale value in the user's secret store must not shadow it.
    environment_value = os.environ.get("API_SERVER_KEY", "")
    if environment_value:
        return environment_value
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret

        try:
            value = get_secret("API_SERVER_KEY", "")
        except UnscopedSecretError:
            value = ""
    except ImportError:
        value = ""
    return value or None


async def _dispatch(request: Request, tail: str):
    runtime = await runtime_registry.get(
        get_hermes_home(),
        gateway_base_url=_gateway_base_url(),
        gateway_api_key=_gateway_api_key(),
    )
    payload: dict[str, Any] = {}
    if request.method in {"POST", "PUT", "PATCH"}:
        try:
            value = await request.json()
            if isinstance(value, dict):
                payload = value
        except ValueError:
            payload = {}
    try:
        result = await VideoKnowledgeController(runtime).dispatch(
            request.method,
            tail,
            query=dict(request.query_params),
            payload=payload,
            actor="hermes-desktop",
        )
    except DomainError as exc:
        raise HTTPException(
            status_code=(
                404
                if exc.code.endswith("NOT_FOUND")
                else 422
                if exc.code
                in {
                    "INVALID_LOCAL_MEDIA",
                    "INVALID_COOKIE_FILE",
                    "INVALID_STORAGE_PATH",
                }
                else 409
            ),
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc
    except MediaToolError as exc:
        raise HTTPException(
            status_code=503 if exc.retryable else 422,
            detail={"code": exc.code, "message": str(exc), "details": {}},
        ) from exc
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_ERROR", "message": str(exc)},
        ) from exc
    return JSONResponse(content=result.body, status_code=result.status)


@router.websocket("/events")
async def video_knowledge_events(websocket: WebSocket) -> None:
    """Stream persisted job events; clients retain polling as a disconnect fallback."""
    await websocket.accept()
    runtime = await runtime_registry.get(
        get_hermes_home(),
        gateway_base_url=_gateway_base_url(),
        gateway_api_key=_gateway_api_key(),
    )
    database, _client = await runtime.resources()
    query_service = JobQueryService(database)
    cursor = websocket.query_params.get("last_event_id")
    if cursor is None:
        # This socket only nudges the Desktop to refetch current state. Replaying
        # the complete persisted history after every Desktop restart can starve
        # the HTTP API for minutes on long-running live monitors.
        cursor = await query_service.latest_event_id()
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


@router.api_route("/{tail:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def video_knowledge_route(request: Request, tail: str):
    return await _dispatch(request, tail)


@router.api_route("", methods=["GET", "POST"])
async def video_knowledge_root(request: Request):
    return await _dispatch(request, "")

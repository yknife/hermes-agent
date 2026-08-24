from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from plugins.video_knowledge.backend.app.api.deps import get_database
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.jobs import JobRead
from plugins.video_knowledge.backend.app.schemas.knowledge import (
    AnalyzeRequest,
    KnowledgeDocumentRead,
)
from plugins.video_knowledge.backend.app.services.knowledge_service import (
    KnowledgeService,
)

router = APIRouter(prefix="/media", tags=["knowledge"])


def _service(request: Request, database: Database) -> KnowledgeService:
    settings = request.app.state.settings
    return KnowledgeService(
        database,
        request.app.state.hermes_client,
        prompt_version=settings.analysis_prompt_version,
        chunk_characters=settings.analysis_chunk_characters,
    )


@router.get("/{media_id}/knowledge", response_model=list[KnowledgeDocumentRead])
async def get_knowledge(
    media_id: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> list[KnowledgeDocumentRead]:
    documents = await _service(request, database).latest_documents(media_id)
    return [KnowledgeDocumentRead.from_orm_document(item) for item in documents]


@router.post(
    "/{media_id}/analyze", response_model=JobRead, status_code=status.HTTP_201_CREATED
)
async def analyze_media(
    media_id: str,
    payload: AnalyzeRequest,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> JobRead:
    job = await _service(request, database).queue_analysis(
        media_id,
        force=payload.force,
        actor=f"api:{request.state.request_id}",
    )
    return JobRead.from_orm_job(job)

from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.schemas.system import (
    RuntimeStatusResponse,
    RuntimeToolStatus,
)
from plugins.video_knowledge.backend.media_adapters import MediaToolInspector


class RuntimeReadinessService:
    def __init__(
        self, settings: Settings, inspector: MediaToolInspector | None = None
    ) -> None:
        self.inspector = inspector or MediaToolInspector(
            ffmpeg_command=settings.ffmpeg_path,
            ffprobe_command=settings.ffprobe_path,
        )

    async def status(self) -> RuntimeStatusResponse:
        tools = [
            RuntimeToolStatus(
                name=item.name,
                available=item.available,
                version=item.version,
                detail=item.detail,
            )
            for item in await self.inspector.inspect()
        ]
        return RuntimeStatusResponse(
            ready=all(item.available for item in tools),
            tools=tools,
        )

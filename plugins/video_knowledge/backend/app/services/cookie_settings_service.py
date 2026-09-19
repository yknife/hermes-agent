import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.domain.errors import InvalidCookieFileError
from plugins.video_knowledge.backend.app.infrastructure.db.base import AppSetting
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.system import (
    CookiePlatform,
    CookieSettingsResponse,
    PlatformCookieSetting,
)
from plugins.video_knowledge.backend.app.services.media_service import (
    resolve_cookie_file_path,
)

COOKIE_SETTINGS_KEY = "media.cookies"
COOKIE_PLATFORMS: dict[CookiePlatform, str] = {
    "bilibili": "B\u7ad9",
    "douyin": "\u6296\u97f3",
    "xiaohongshu": "\u5c0f\u7ea2\u4e66",
    "weibo": "\u5fae\u535a",
    "youtube": "YouTube",
    "vimeo": "Vimeo",
    "twitch": "Twitch",
}


class CookieSettingsService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    async def status(self) -> CookieSettingsResponse:
        paths = await self._paths()
        return CookieSettingsResponse(
            platforms=[
                self._platform_status(platform, label, paths.get(platform))
                for platform, label in COOKIE_PLATFORMS.items()
            ]
        )

    async def update(
        self, platform: str, raw_path: str | None
    ) -> CookieSettingsResponse:
        self._validate_platform(platform)
        resolved = (
            resolve_cookie_file_path(raw_path)
            if raw_path is not None and raw_path.strip()
            else None
        )
        async with self.database.session() as session, session.begin():
            paths = await self._paths(session)
            if resolved is None:
                paths.pop(platform, None)
            else:
                paths[platform] = str(resolved)
            encoded = json.dumps(paths, ensure_ascii=False, sort_keys=True)
            row = await session.get(AppSetting, COOKIE_SETTINGS_KEY)
            if row is None:
                session.add(AppSetting(key=COOKIE_SETTINGS_KEY, value_json=encoded))
            else:
                row.value_json = encoded
        return await self.status()

    async def resolve(
        self, platform: str, *, session: AsyncSession | None = None
    ) -> Path | None:
        paths = await self._paths(session)
        raw_path = paths.get(platform)
        if raw_path:
            return resolve_cookie_file_path(raw_path)
        if self.settings.yt_dlp_cookies_file is not None:
            return resolve_cookie_file_path(str(self.settings.yt_dlp_cookies_file))
        return None

    async def _paths(self, session: AsyncSession | None = None) -> dict[str, str]:
        if session is not None:
            row = await session.get(AppSetting, COOKIE_SETTINGS_KEY)
            return self._decode(row.value_json if row is not None else None)
        async with self.database.session() as owned_session:
            row = await owned_session.get(AppSetting, COOKIE_SETTINGS_KEY)
            return self._decode(row.value_json if row is not None else None)

    @staticmethod
    def _decode(value: str | None) -> dict[str, str]:
        if not value:
            return {}
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        if not isinstance(decoded, dict):
            return {}
        return {
            str(platform): str(path)
            for platform, path in decoded.items()
            if isinstance(platform, str) and isinstance(path, str) and path
        }

    @staticmethod
    def _platform_status(
        platform: CookiePlatform, label: str, raw_path: str | None
    ) -> PlatformCookieSetting:
        path = Path(raw_path) if raw_path else None
        return PlatformCookieSetting(
            platform=platform,
            label=label,
            cookies_file=str(path) if path is not None else None,
            file_name=path.name if path is not None else None,
            configured=path is not None,
            available=bool(path is not None and path.is_file()),
        )

    @staticmethod
    def _validate_platform(platform: str) -> None:
        if platform not in COOKIE_PLATFORMS:
            raise InvalidCookieFileError("Unsupported cookie platform")

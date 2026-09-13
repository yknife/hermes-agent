from sqlalchemy.ext.asyncio import AsyncSession

from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.infrastructure.db.base import AppSetting
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.system import MessagingQuotaSettings

MESSAGING_QUOTA_SETTINGS_KEY = "messaging.quotas"


class MessagingQuotaSettingsService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    def defaults(self) -> MessagingQuotaSettings:
        return MessagingQuotaSettings(
            enabled=True,
            max_active_per_user=self.settings.messaging_max_active_per_user,
            max_active_per_chat=self.settings.messaging_max_active_per_chat,
            max_submissions_per_user_per_day=(
                self.settings.messaging_max_submissions_per_user_per_day
            ),
            max_submissions_per_chat_per_day=(
                self.settings.messaging_max_submissions_per_chat_per_day
            ),
        )

    async def status(
        self, *, session: AsyncSession | None = None
    ) -> MessagingQuotaSettings:
        if session is not None:
            row = await session.get(AppSetting, MESSAGING_QUOTA_SETTINGS_KEY)
            return self._decode(row.value_json if row is not None else None)
        async with self.database.session() as owned_session:
            row = await owned_session.get(AppSetting, MESSAGING_QUOTA_SETTINGS_KEY)
            return self._decode(row.value_json if row is not None else None)

    async def update(self, value: MessagingQuotaSettings) -> MessagingQuotaSettings:
        encoded = value.model_dump_json()
        async with self.database.session() as session, session.begin():
            row = await session.get(AppSetting, MESSAGING_QUOTA_SETTINGS_KEY)
            if row is None:
                session.add(
                    AppSetting(key=MESSAGING_QUOTA_SETTINGS_KEY, value_json=encoded)
                )
            else:
                row.value_json = encoded
        return value

    def _decode(self, value: str | None) -> MessagingQuotaSettings:
        if not value:
            return self.defaults()
        try:
            return MessagingQuotaSettings.model_validate_json(value)
        except ValueError:
            return self.defaults()

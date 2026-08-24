from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", self._configure_sqlite)

    @staticmethod
    def _configure_sqlite(dbapi_connection: object, connection_record: object) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    def session(self) -> AsyncSession:
        return self.session_factory()

    async def dispose(self) -> None:
        await self.engine.dispose()

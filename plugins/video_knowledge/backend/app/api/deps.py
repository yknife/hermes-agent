from typing import cast

from fastapi import Request

from plugins.video_knowledge.backend.app.infrastructure.db.session import Database


def get_database(request: Request) -> Database:
    return cast(Database, request.app.state.database)

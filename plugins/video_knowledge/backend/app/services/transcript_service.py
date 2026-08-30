import asyncio
import hashlib
import json
import os
import re
from pathlib import Path

from sqlalchemy import bindparam, func, select, text

from plugins.video_knowledge.backend.app.domain.enums import MediaAssetKind
from plugins.video_knowledge.backend.app.domain.errors import MediaNotFoundError
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    MediaAsset,
    MediaItem,
    Transcript,
    TranscriptSegment,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.job_service import new_id
from plugins.video_knowledge.backend.media_adapters import SubtitleDownloadResult
from plugins.video_knowledge.backend.transcript import NormalizedTranscript

SAFE_LANGUAGE = re.compile(r"[^A-Za-z0-9_-]+")


class TranscriptService:
    def __init__(self, database: Database, storage_root: Path) -> None:
        self.database = database
        self.storage_root = storage_root.resolve()

    async def register(
        self,
        media_id: str,
        subtitle: SubtitleDownloadResult | None,
        normalized: NormalizedTranscript,
        *,
        model_name: str | None = None,
        model_config: dict[str, object] | None = None,
    ) -> Transcript:
        async with self.database.session() as session:
            media_exists = await session.get(MediaItem, media_id)
            if media_exists is None:
                raise MediaNotFoundError("媒体不存在", details={"media_id": media_id})
            current_version = await session.scalar(
                select(func.coalesce(func.max(Transcript.version), 0)).where(
                    Transcript.media_id == media_id
                )
            )
        version = int(current_version or 0) + 1
        root = (self.storage_root / "media" / media_id).resolve()
        if self.storage_root not in root.parents:
            raise ValueError("Transcript 目标路径越界")
        language = SAFE_LANGUAGE.sub("-", normalized.language).strip("-") or "und"
        subtitle_dir = root / "subtitles"
        transcript_dir = root / "transcript"
        await asyncio.to_thread(subtitle_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(transcript_dir.mkdir, parents=True, exist_ok=True)
        original_target: Path | None = None
        if subtitle is not None:
            original_target = (
                subtitle_dir / f"original.{language}{subtitle.path.suffix.lower()}"
            )
            await asyncio.to_thread(os.replace, subtitle.path, original_target)
        segments_target = transcript_dir / f"v{version}.segments.json"
        text_target = transcript_dir / f"v{version}.txt"
        serialized = {
            "language": normalized.language,
            "source_type": normalized.source_type,
            "segments": [
                {
                    "index": item.index,
                    "start_ms": item.start_ms,
                    "end_ms": item.end_ms,
                    "speaker": item.speaker,
                    "text": item.text,
                    "confidence": item.confidence,
                }
                for item in normalized.segments
            ],
        }
        await asyncio.to_thread(
            segments_target.write_text,
            json.dumps(serialized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        await asyncio.to_thread(
            text_target.write_text, normalized.plain_text, encoding="utf-8"
        )

        transcript = Transcript(
            id=new_id("transcript"),
            media_id=media_id,
            version=version,
            language=normalized.language,
            source_type=normalized.source_type,
            status="READY",
            plain_text_path=text_target.relative_to(self.storage_root).as_posix(),
            segments_path=segments_target.relative_to(self.storage_root).as_posix(),
            model_name=model_name,
            model_config_json=json.dumps(model_config or {}, ensure_ascii=False),
        )
        segment_rows = [
            TranscriptSegment(
                id=new_id("segment"),
                transcript_id=transcript.id,
                segment_index=item.index,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                speaker=item.speaker,
                text=item.text,
                confidence=item.confidence,
                search_text=item.text.casefold(),
            )
            for item in normalized.segments
        ]
        assets = [
            await asyncio.to_thread(
                self._asset, media_id, MediaAssetKind.TRANSCRIPT_JSON, segments_target
            ),
            await asyncio.to_thread(
                self._asset, media_id, MediaAssetKind.TRANSCRIPT_TEXT, text_target
            ),
        ]
        if original_target is not None:
            assets.append(
                await asyncio.to_thread(
                    self._asset,
                    media_id,
                    MediaAssetKind.SUBTITLE_ORIGINAL,
                    original_target,
                )
            )
        async with self.database.session() as session, session.begin():
            await session.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS transcript_segments_fts "
                    "USING fts5(segment_id UNINDEXED, transcript_id UNINDEXED, text, "
                    "tokenize='trigram')"
                )
            )
            session.add(transcript)
            await session.flush()
            session.add_all(segment_rows)
            session.add_all(assets)
            await session.flush()
            for segment in segment_rows:
                await session.execute(
                    text(
                        "INSERT INTO transcript_segments_fts(segment_id, transcript_id, text) "
                        "VALUES (:segment_id, :transcript_id, :text)"
                    ),
                    {
                        "segment_id": segment.id,
                        "transcript_id": transcript.id,
                        "text": segment.text,
                    },
                )
        return transcript

    async def latest(
        self, media_id: str
    ) -> tuple[Transcript, list[TranscriptSegment]] | None:
        async with self.database.session() as session:
            transcript = await session.scalar(
                select(Transcript)
                .where(Transcript.media_id == media_id, Transcript.status == "READY")
                .order_by(Transcript.version.desc())
                .limit(1)
            )
            if transcript is None:
                return None
            segments = list(
                (
                    await session.scalars(
                        select(TranscriptSegment)
                        .where(TranscriptSegment.transcript_id == transcript.id)
                        .order_by(TranscriptSegment.segment_index)
                    )
                ).all()
            )
            return transcript, segments

    async def search(
        self,
        query: str,
        *,
        media_id: str | None = None,
        media_ids: list[str] | None = None,
        limit: int = 50,
    ) -> list[tuple[TranscriptSegment, str]]:
        query = query.strip()
        if not query:
            return []
        scope_media_ids = [media_id] if media_id else media_ids
        if scope_media_ids is not None:
            scope_media_ids = list(dict.fromkeys(scope_media_ids))[:50]
            if not scope_media_ids:
                return []
        async with self.database.session() as session:
            if len(query) >= 3:
                phrase = f'"{query.replace(chr(34), chr(34) * 2)}"'
                scope_clause = (
                    "AND t.media_id IN :media_ids " if scope_media_ids else ""
                )
                statement = text(
                    "SELECT s.id FROM transcript_segments_fts f "
                    "JOIN transcript_segments s ON s.id = f.segment_id "
                    "JOIN transcripts t ON t.id = s.transcript_id "
                    "WHERE transcript_segments_fts MATCH :query "
                    + scope_clause
                    + "ORDER BY bm25(transcript_segments_fts), s.start_ms LIMIT :limit"
                )
                parameters: dict[str, object] = {"query": phrase, "limit": limit}
                if scope_media_ids:
                    statement = statement.bindparams(
                        bindparam("media_ids", expanding=True)
                    )
                    parameters["media_ids"] = scope_media_ids
                ids = list((await session.execute(statement, parameters)).scalars())
            else:
                ids = list(
                    (
                        await session.scalars(
                            select(TranscriptSegment.id)
                            .join(
                                Transcript,
                                Transcript.id == TranscriptSegment.transcript_id,
                            )
                            .where(
                                TranscriptSegment.search_text.contains(
                                    query.casefold()
                                ),
                                *(
                                    [Transcript.media_id.in_(scope_media_ids)]
                                    if scope_media_ids
                                    else []
                                ),
                            )
                            .order_by(TranscriptSegment.start_ms)
                            .limit(limit)
                        )
                    ).all()
                )
            if not ids:
                return []
            rows = list(
                (
                    await session.scalars(
                        select(TranscriptSegment).where(TranscriptSegment.id.in_(ids))
                    )
                ).all()
            )
            by_id = {row.id: row for row in rows}
            transcript_ids = {row.transcript_id for row in rows}
            transcript_media_rows = (
                await session.execute(
                    select(Transcript.id, Transcript.media_id).where(
                        Transcript.id.in_(transcript_ids)
                    )
                )
            ).all()
            media_by_transcript: dict[str, str] = {
                row[0]: row[1] for row in transcript_media_rows
            }
            return [
                (
                    by_id[segment_id],
                    media_by_transcript[by_id[segment_id].transcript_id],
                )
                for segment_id in ids
                if segment_id in by_id
            ]

    def resolve_media_video(self, media_id: str, assets: list[MediaAsset]) -> Path:
        asset = next(
            (item for item in assets if item.kind == MediaAssetKind.VIDEO.value), None
        )
        if asset is None:
            raise MediaNotFoundError(
                "媒体视频资产不存在", details={"media_id": media_id}
            )
        path = (self.storage_root / asset.relative_path).resolve()
        if self.storage_root not in path.parents or not path.is_file():
            raise MediaNotFoundError("媒体文件不存在", details={"media_id": media_id})
        return path

    def _asset(self, media_id: str, kind: MediaAssetKind, path: Path) -> MediaAsset:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        mime = {
            MediaAssetKind.SUBTITLE_ORIGINAL: "text/plain",
            MediaAssetKind.TRANSCRIPT_JSON: "application/json",
            MediaAssetKind.TRANSCRIPT_TEXT: "text/plain",
        }[kind]
        return MediaAsset(
            id=new_id("asset"),
            media_id=media_id,
            kind=kind.value,
            relative_path=path.relative_to(self.storage_root).as_posix(),
            mime_type=mime,
            size_bytes=path.stat().st_size,
            sha256=digest.hexdigest(),
            status="READY",
            metadata_json="{}",
        )

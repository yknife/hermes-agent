"""Add transcripts, segments, and FTS search.

Revision ID: 20260817_0004
Revises: 20260816_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0004"
down_revision: str | None = "20260816_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transcripts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "media_id",
            sa.String(64),
            sa.ForeignKey("media_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(32), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("plain_text_path", sa.Text(), nullable=False),
        sa.Column("segments_path", sa.Text(), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("model_config_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("media_id", "version", name="uq_transcript_media_version"),
    )
    op.create_index("ix_transcripts_media_id", "transcripts", ["media_id"])
    op.create_index(
        "ix_transcripts_media_created", "transcripts", ["media_id", "created_at"]
    )
    op.create_table(
        "transcript_segments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "transcript_id",
            sa.String(64),
            sa.ForeignKey("transcripts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("speaker", sa.String(255), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.UniqueConstraint("transcript_id", "segment_index", name="uq_segment_index"),
    )
    op.create_index(
        "ix_transcript_segments_transcript_id", "transcript_segments", ["transcript_id"]
    )
    op.create_index(
        "ix_transcript_segments_time",
        "transcript_segments",
        ["transcript_id", "start_ms"],
    )
    op.execute(
        "CREATE VIRTUAL TABLE transcript_segments_fts "
        "USING fts5(segment_id UNINDEXED, transcript_id UNINDEXED, text, tokenize='trigram')"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS transcript_segments_fts")
    op.drop_table("transcript_segments")
    op.drop_table("transcripts")

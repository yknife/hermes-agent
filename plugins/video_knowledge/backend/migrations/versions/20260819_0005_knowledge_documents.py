"""Add versioned Hermes knowledge documents.

Revision ID: 20260819_0005
Revises: 20260817_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0005"
down_revision: str | None = "20260817_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "media_id",
            sa.String(64),
            sa.ForeignKey("media_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "transcript_id",
            sa.String(64),
            sa.ForeignKey("transcripts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_type", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "media_id",
            "document_type",
            "version",
            name="uq_knowledge_media_type_version",
        ),
    )
    op.create_index(
        "ix_knowledge_documents_media_id", "knowledge_documents", ["media_id"]
    )
    op.create_index(
        "ix_knowledge_documents_transcript_id", "knowledge_documents", ["transcript_id"]
    )
    op.create_index(
        "ix_knowledge_media_type_created",
        "knowledge_documents",
        ["media_id", "document_type", "created_at"],
    )
    op.create_index("ix_knowledge_fingerprint", "knowledge_documents", ["fingerprint"])


def downgrade() -> None:
    op.drop_table("knowledge_documents")

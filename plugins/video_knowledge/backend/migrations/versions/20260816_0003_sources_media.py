"""Add sources, media items, and media assets.

Revision ID: 20260816_0003
Revises: 20260816_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260816_0003"
down_revision: str | None = "20260816_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("type", sa.String(24), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("type", "canonical_url", name="uq_sources_canonical_url"),
        sa.UniqueConstraint(
            "type", "platform", "external_id", name="uq_sources_external_id"
        ),
    )
    op.create_index("ix_sources_created_at", "sources", ["created_at"])
    op.create_table(
        "media_items",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "source_id",
            sa.String(64),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("webpage_url", sa.Text(), nullable=False),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "source_id", "external_id", name="uq_media_source_external_id"
        ),
    )
    op.create_index("ix_media_items_source_id", "media_items", ["source_id"])
    op.create_index("ix_media_items_published_at", "media_items", ["published_at"])
    op.create_index("ix_media_items_created_at", "media_items", ["created_at"])
    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "media_id",
            sa.String(64),
            sa.ForeignKey("media_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("container", sa.String(64), nullable=True),
        sa.Column("codec", sa.String(128), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_media_assets_media_id", "media_assets", ["media_id"])
    op.create_index("ix_media_assets_media_kind", "media_assets", ["media_id", "kind"])


def downgrade() -> None:
    op.drop_table("media_assets")
    op.drop_table("media_items")
    op.drop_table("sources")

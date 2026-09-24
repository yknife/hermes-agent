"""Durable Wiki ingestion requests tied to independent jobs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0012"
down_revision: str | None = "20260924_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wiki_ingestions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("wiki_id", sa.String(64), nullable=False),
        sa.Column("bundle_key", sa.String(64), nullable=False),
        sa.Column("media_id", sa.String(64), nullable=False),
        sa.Column("document_ids_json", sa.Text(), nullable=False),
        sa.Column("job_id", sa.String(64), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("batch_id", sa.String(64)),
        sa.Column("source_revision", sa.String(68)),
        sa.Column("commit_id", sa.String(64)),
        sa.Column("needs_review", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("wiki_id", "bundle_key", name="uq_wiki_ingestion_bundle"),
    )
    op.create_index(
        "ix_wiki_ingestion_media", "wiki_ingestions", ["media_id", "created_at"]
    )
    op.create_index("ix_wiki_ingestion_batch", "wiki_ingestions", ["batch_id"])


def downgrade() -> None:
    op.drop_table("wiki_ingestions")

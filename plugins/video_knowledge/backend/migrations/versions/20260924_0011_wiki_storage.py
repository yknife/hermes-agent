"""Add Wiki catalog, commit journal and recoverable page projection."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0011"
down_revision: str | None = "20260907_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wiki_catalogs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("relative_root", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("commit_id", sa.String(64)),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", sa.Float()),
    )
    op.create_table(
        "wiki_commits",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("wiki_id", sa.String(64), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("wiki_id", "base_revision", name="uq_wiki_commit_base"),
    )
    op.create_index("ix_wiki_commits_status", "wiki_commits", ["wiki_id", "status"])
    op.create_table(
        "wiki_page_projections",
        sa.Column("wiki_id", sa.String(64), primary_key=True),
        sa.Column("page_id", sa.String(128), primary_key=True),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("page_type", sa.String(32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("commit_id", sa.String(64), nullable=False),
        sa.UniqueConstraint("wiki_id", "relative_path", name="uq_wiki_page_path"),
    )


def downgrade() -> None:
    op.drop_table("wiki_page_projections")
    op.drop_table("wiki_commits")
    op.drop_table("wiki_catalogs")

"""Create rebuildable Wiki FTS index and revision marker."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0013"
down_revision: str | None = "20260924_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wiki_search_state",
        sa.Column("wiki_id", sa.String(64), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
    op.execute(
        "CREATE VIRTUAL TABLE wiki_page_fts USING fts5("
        "wiki_id UNINDEXED, page_id UNINDEXED, tokens)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE wiki_page_fts")
    op.drop_table("wiki_search_state")

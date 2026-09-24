"""Track independent Wiki fusion jobs and their committed result."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0014"
down_revision: str | None = "20260924_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("wiki_ingestions") as batch:
        batch.add_column(sa.Column("fusion_job_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("fusion_run_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("fusion_commit_id", sa.String(64), nullable=True))
        batch.create_foreign_key(
            "fk_wiki_fusion_job", "jobs", ["fusion_job_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("wiki_ingestions") as batch:
        batch.drop_constraint("fk_wiki_fusion_job", type_="foreignkey")
        batch.drop_column("fusion_commit_id")
        batch.drop_column("fusion_run_id")
        batch.drop_column("fusion_job_id")

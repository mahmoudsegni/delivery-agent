"""Dated member absences for capacity proration.

Capacity was a flat hours-per-sprint number, so anyone away for part of a
sprint counted as fully available and the board bars and the agent's sprint
drafts over-allocated them. Absences are inclusive date ranges; 8h per absent
workday (Mon-Fri) comes off the sprint's capacity, clamped at zero.

Revision ID: 020
Revises: 019
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "member_absences",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "member_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("team_members.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("starts_on <= ends_on", name="ck_absence_dates"),
    )
    op.create_index("ix_member_absences_member_id", "member_absences", ["member_id"])


def downgrade() -> None:
    op.drop_table("member_absences")

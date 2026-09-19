"""Initial ShadowTalk schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_sessions",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("genre", sa.String(length=32), nullable=True),
        sa.Column("draft_content_type", sa.String(length=24), nullable=True),
        sa.Column("draft_text", sa.Text(), nullable=True),
        sa.Column("draft_entities", sa.JSON(), nullable=True),
        sa.Column("draft_file_id", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "moderator_sessions",
        sa.Column("moderator_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("submission_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("moderator_id"),
    )

    op.create_table(
        "submissions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("genre", sa.String(length=32), nullable=False),
        sa.Column("content_type", sa.String(length=24), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("entities", sa.JSON(), nullable=True),
        sa.Column("file_id", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("moderated_by", sa.BigInteger(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("internal_error", sa.Text(), nullable=True),
        sa.Column("channel_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_submissions_status_created_at",
        "submissions",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_submissions_author_id_created_at",
        "submissions",
        ["author_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_submissions_author_id_created_at", table_name="submissions")
    op.drop_index("ix_submissions_status_created_at", table_name="submissions")
    op.drop_table("submissions")
    op.drop_table("moderator_sessions")
    op.drop_table("user_sessions")

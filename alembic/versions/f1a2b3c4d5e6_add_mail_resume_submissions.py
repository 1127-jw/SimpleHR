"""add mail_resume_submissions

Revision ID: f1a2b3c4d5e6
Revises: d4e5f6a7b8c9
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mail_resume_submissions",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("email_uid", sa.String(64), nullable=False),
        sa.Column("message_id", sa.String(255), nullable=False),
        sa.Column("from_address", sa.String(255), nullable=False),
        sa.Column("from_name", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("body_excerpt", sa.Text(), nullable=False),
        sa.Column("attachment_paths", sa.JSON(), nullable=False),
        sa.Column("parsed_info", sa.JSON(), nullable=False),
        sa.Column("suggested_position_id", sa.String(100), nullable=True),
        sa.Column("position_id", sa.String(100), nullable=True),
        sa.Column("resume_id", sa.String(100), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reviewer_id", sa.String(100), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_mail_submission_message_id", "mail_resume_submissions", ["message_id"], unique=True
    )
    op.create_index("ix_mail_submission_from_address", "mail_resume_submissions", ["from_address"])
    op.create_index(
        "ix_mail_submission_status_created", "mail_resume_submissions", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_mail_submission_status_created", table_name="mail_resume_submissions")
    op.drop_index("ix_mail_submission_from_address", table_name="mail_resume_submissions")
    op.drop_index("ix_mail_submission_message_id", table_name="mail_resume_submissions")
    op.drop_table("mail_resume_submissions")

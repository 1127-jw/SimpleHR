"""mail review

Revision ID: 8951e4b8b2a0
Revises: ccbe36322325
Create Date: 2026-11-19 16:08:26.943299

意图识别「人工复核」待办表。落地顺序：先建模型（``MailReviewModel``）
→ ``alembic revision --autogenerate`` → 手工裁剪。

> ⚠️ 本迁移**只包含** ``mail_reviews`` 建表与索引。autogenerate 另外报的
> LangGraph checkpoint 内部表、``mail_resume_submissions`` 索引漂移，
> 与本次无关，已剔除（同 ``ccbe36322325`` 的处理方式）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '8951e4b8b2a0'
down_revision: Union[str, Sequence[str], None] = 'ccbe36322325'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'mail_reviews',
        sa.Column('email_uid', sa.String(length=64), nullable=False),
        sa.Column('message_id', sa.String(length=255), nullable=False),
        sa.Column('from_address', sa.String(length=255), nullable=False),
        sa.Column('from_name', sa.String(length=255), nullable=False),
        sa.Column('subject', sa.String(length=500), nullable=False),
        sa.Column('body_excerpt', sa.Text(), nullable=False),
        sa.Column('attachment_paths', sa.JSON(), nullable=False),
        sa.Column('intent_guess', sa.String(length=32), nullable=False),
        sa.Column('sub_intent', sa.String(length=32), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('evidence', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('resolver_id', sa.String(length=100), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('resolution', sa.Text(), nullable=False),
        sa.Column('id', sa.String(length=100), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_mail_reviews')),
    )
    op.create_index('ix_mail_review_status_created', 'mail_reviews',
                    ['status', 'created_at'], unique=False)
    op.create_index(op.f('ix_mail_reviews_from_address'), 'mail_reviews',
                    ['from_address'], unique=False)
    op.create_index(op.f('ix_mail_reviews_message_id'), 'mail_reviews',
                    ['message_id'], unique=True)
    op.create_index(op.f('ix_mail_reviews_status'), 'mail_reviews',
                    ['status'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_mail_reviews_status'), table_name='mail_reviews')
    op.drop_index(op.f('ix_mail_reviews_message_id'), table_name='mail_reviews')
    op.drop_index(op.f('ix_mail_reviews_from_address'), table_name='mail_reviews')
    op.drop_index('ix_mail_review_status_created', table_name='mail_reviews')
    op.drop_table('mail_reviews')

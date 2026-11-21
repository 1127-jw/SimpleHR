"""allow null resume_id on candidates

Revision ID: f2b3c4d5e6a7
Revises: 8951e4b8b2a0
Create Date: 2026-11-21 00:00:00.000000

背景
----
线下投递 / 手工录入的候选人可能没有电子简历，强制 `resume_id NOT NULL`
会让创建接口只能靠外键违约返回 500，或者逼调用方先造一份假简历。

本迁移放开 NOT NULL；`ix_candidates_resume_id` 仍然是唯一索引，
PostgreSQL 的唯一索引允许多个 NULL，因此「一份简历只挂一个候选人」
的约束不受影响。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2b3c4d5e6a7'
down_revision: Union[str, Sequence[str], None] = '8951e4b8b2a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        'candidates',
        'resume_id',
        existing_type=sa.String(length=100),
        nullable=True,
    )


def downgrade() -> None:
    """Downgrade schema.

    注意：若已存在 resume_id 为 NULL 的行，回滚会失败——需先补齐数据。
    """
    op.alter_column(
        'candidates',
        'resume_id',
        existing_type=sa.String(length=100),
        nullable=False,
    )

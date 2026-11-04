"""add PENDING to interviewresultenum

Revision ID: 75dc90da28a7
Revises: 892bef1a3fe7
Create Date: 2026-11-04 12:06:55.034195

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '75dc90da28a7'
down_revision: Union[str, Sequence[str], None] = '892bef1a3fe7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 添加缺失的枚举值到 interviewresultenum
    # LangGraph 的 checkpoint_* 表由 LangGraph 自行管理，不纳入 Alembic 迁移
    op.execute("ALTER TYPE interviewresultenum ADD VALUE IF NOT EXISTS 'PENDING'")


def downgrade() -> None:
    """Downgrade schema."""
    # PostgreSQL 不支持直接删除枚举值，需要重建类型
    pass

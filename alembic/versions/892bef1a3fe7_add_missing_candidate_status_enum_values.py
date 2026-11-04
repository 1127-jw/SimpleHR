"""add missing candidate status enum values

Revision ID: 892bef1a3fe7
Revises: 76f1be893ce0
Create Date: 2026-11-04 11:39:02.381091

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '892bef1a3fe7'
down_revision: Union[str, Sequence[str], None] = '76f1be893ce0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 添加缺失的枚举值到 candidatestatusenum
    # LangGraph 的 checkpoint_* 表由 LangGraph 自行管理，不纳入 Alembic 迁移
    op.execute("ALTER TYPE candidatestatusenum ADD VALUE IF NOT EXISTS '协商超时'")
    op.execute("ALTER TYPE candidatestatusenum ADD VALUE IF NOT EXISTS '处理异常'")


def downgrade() -> None:
    """Downgrade schema."""
    # PostgreSQL 不支持直接删除枚举值，需要重建类型
    # 这里仅作占位，实际 downgrade 需要更复杂的操作
    pass

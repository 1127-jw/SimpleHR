"""add manual_passed to candidatestatusenum

Revision ID: a1b2c3d4e5f6
Revises: ed3f50bb8cea
Create Date: 2026-11-11 23:15:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'ed3f50bb8cea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 添加人工通过/重试 枚举值到 candidatestatusenum
    op.execute("ALTER TYPE candidatestatusenum ADD VALUE IF NOT EXISTS '人工通过/重试'")


def downgrade() -> None:
    """Downgrade schema."""
    # PostgreSQL 不支持直接删除枚举值，需要重建类型
    # 这里仅作占位，实际 downgrade 需要更复杂的操作
    pass

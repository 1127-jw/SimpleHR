"""add work_year to candidates

Revision ID: 76f1be893ce0
Revises: 662c8b0eba7d
Create Date: 2026-11-04 11:30:52.600651

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '76f1be893ce0'
down_revision: Union[str, Sequence[str], None] = '662c8b0eba7d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 只添加 candidates.work_year 列
    # LangGraph 的 checkpoint_* 表由 LangGraph 自行管理，不纳入 Alembic 迁移
    op.add_column('candidates', sa.Column('work_year', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('candidates', 'work_year')

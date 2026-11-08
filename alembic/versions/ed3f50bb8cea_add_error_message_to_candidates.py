"""add error_message to candidates

Revision ID: ed3f50bb8cea
Revises: b0b9b9bcf268
Create Date: 2026-11-08 17:31:13.889855

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ed3f50bb8cea'
down_revision: Union[str, Sequence[str], None] = 'b0b9b9bcf268'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 只添加 candidates.error_message 列
    # LangGraph 的 checkpoint_* 表由 LangGraph 自行管理，不纳入 Alembic 迁移
    op.add_column('candidates', sa.Column('error_message', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('candidates', 'error_message')

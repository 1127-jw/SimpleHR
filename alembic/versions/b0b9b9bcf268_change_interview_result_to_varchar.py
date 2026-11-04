"""change interview result to varchar

Revision ID: b0b9b9bcf268
Revises: 75dc90da28a7
Create Date: 2026-11-04 12:15:59.610098

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b0b9b9bcf268'
down_revision: Union[str, Sequence[str], None] = '75dc90da28a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 将 interviews.result 从 PostgreSQL ENUM 改为 VARCHAR
    # 避免枚举值同步问题
    op.alter_column(
        'interviews', 'result',
        existing_type=postgresql.ENUM('PASSED', 'FAILED', 'PENDING', name='interviewresultenum'),
        type_=sa.String(length=20),
        existing_nullable=True,
        postgresql_using='result::text',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'interviews', 'result',
        existing_type=sa.String(length=20),
        type_=postgresql.ENUM('PASSED', 'FAILED', 'PENDING', name='interviewresultenum'),
        existing_nullable=True,
        postgresql_using='result::interviewresultenum',
    )
